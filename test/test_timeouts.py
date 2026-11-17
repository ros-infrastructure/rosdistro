from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from rosdistro import logger
from rosdistro.distribution import Distribution
from rosdistro.distribution_cache import DistributionCache
from rosdistro.freeze_source import _worker
from rosdistro.loader import load_url
from rosdistro.manifest_provider import (
    DEFAULT_HTTP_TIMEOUT,
    PermanentManifestError,
    get_http_timeout,
    is_unauthenticated_http_url,
)
from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedManifestProvider, CachedSourceManifestProvider
from rosdistro.manifest_provider.git import git_source_manifest_provider
import rosdistro.manifest_provider.github as github_mp
import rosdistro.manifest_provider.gitlab as gitlab_mp
from rosdistro.manifest_provider.tar import tar_manifest_provider, tar_source_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.repository_specification import RepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification
from rosdistro.vcs import (
    DEFAULT_GIT_LS_REMOTE_TIMEOUT,
    DEFAULT_GIT_TIMEOUT,
    Git,
    _run_command,
    get_git_timeout,
)


@contextmanager
def _stalled_http_server(stall_event):
    """Local HTTP server that accepts connections and blocks until *stall_event* is set."""
    class _StallHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            stall_event.wait(timeout=10.0)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')
            except OSError:
                pass

        def log_message(self, format, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), _StallHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    env_overrides = {
        'no_proxy': '127.0.0.1,localhost',
        'NO_PROXY': '127.0.0.1,localhost',
    }
    try:
        with patch.dict(os.environ, env_overrides, clear=False):
            yield port
    finally:
        stall_event.set()
        server.shutdown()
        server.server_close()


class _ListLogHandler(logging.Handler):
    def __init__(self, level=logging.WARNING):
        super(_ListLogHandler, self).__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _capture_logs(level=logging.WARNING):
    handler = _ListLogHandler(level=level)
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _make_mock_http_response(body, headers=None):
    headers = headers or {}
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.getheader.side_effect = lambda k, default=None: headers.get(k, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_get_http_timeout_defaults_and_env():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('ROSDISTRO_HTTP_TIMEOUT', None)
        assert get_http_timeout() == DEFAULT_HTTP_TIMEOUT

    with patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '12.5'}, clear=False):
        assert get_http_timeout() == 12.5

    with patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '0'}, clear=False):
        assert get_http_timeout() is None


def test_get_git_timeout_defaults_and_env():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('ROSDISTRO_GIT_TIMEOUT', None)
        os.environ.pop('ROSDISTRO_GIT_LS_REMOTE_TIMEOUT', None)
        assert get_git_timeout('clone') == DEFAULT_GIT_TIMEOUT
        assert get_git_timeout('ls-remote') == DEFAULT_GIT_LS_REMOTE_TIMEOUT

    with patch.dict(os.environ, {'ROSDISTRO_GIT_TIMEOUT': '45'}, clear=False):
        os.environ.pop('ROSDISTRO_GIT_LS_REMOTE_TIMEOUT', None)
        assert get_git_timeout('clone') == 45.0
        assert get_git_timeout('ls-remote') == 45.0

    with patch.dict(os.environ, {
        'ROSDISTRO_GIT_TIMEOUT': '45',
        'ROSDISTRO_GIT_LS_REMOTE_TIMEOUT': '15',
    }, clear=False):
        assert get_git_timeout('clone') == 45.0
        assert get_git_timeout('ls-remote') == 15.0

    with patch.dict(os.environ, {'ROSDISTRO_GIT_TIMEOUT': '0'}, clear=False):
        os.environ.pop('ROSDISTRO_GIT_LS_REMOTE_TIMEOUT', None)
        assert get_git_timeout('clone') is None


def test_run_command_timeout_kills_process_tree():
    # Spawn a python process that creates a child sleeping process inheriting stdout/stderr.
    # Verifies _run_command times out promptly and kills child processes too.
    script = (
        'import subprocess, sys, time; '
        'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
        'time.sleep(60)'
    )
    t0 = time.monotonic()
    result = _run_command((sys.executable, '-c', script), timeout=0.5)
    elapsed = time.monotonic() - t0

    assert result['returncode'] == 124
    assert result.get('timed_out') is True
    assert 'timed out after 0.5 seconds' in result['error']
    assert elapsed < 5.0


def test_git_ls_remote_and_clone_timeout_on_stalled_server():
    stall_event = threading.Event()
    with _stalled_http_server(stall_event) as port:
        url = 'http://127.0.0.1:%d/owner/stalled-repo.git' % port
        with patch.dict(os.environ, {
            'ROSDISTRO_GIT_TIMEOUT': '1',
            'ROSDISTRO_GIT_LS_REMOTE_TIMEOUT': '1',
        }, clear=False):
            t0 = time.monotonic()
            result = Git().command('ls-remote', url)
            elapsed = time.monotonic() - t0
            assert result['returncode'] == 124
            assert result.get('timed_out') is True
            assert 'timed out' in result['error']
            assert elapsed < 5.0

            # RepositorySpecification.remote_refs surfaces the timeout clearly
            repo_spec = RepositorySpecification('stalled', {'url': url})
            try:
                _ = repo_spec.remote_refs
                assert False, 'Expected RuntimeError on stalled ls-remote'
            except RuntimeError as e:
                assert 'timed out' in str(e)

            # git_source_manifest_provider times out cleanly and cleans up temp dir
            src_repo = SourceRepositorySpecification('stalled', {
                'type': 'git',
                'url': url,
                'version': 'main',
            })
            t1 = time.monotonic()
            try:
                git_source_manifest_provider(src_repo)
                assert False, 'Expected RuntimeError on stalled git clone'
            except RuntimeError as e:
                assert 'timed out' in str(e)
            assert time.monotonic() - t1 < 5.0


def test_cached_source_manifest_provider_logs_timeout():
    stall_event = threading.Event()
    with _stalled_http_server(stall_event) as port:
        url = 'http://127.0.0.1:%d/owner/stalled-repo.git' % port
        dist_file_data = {
            'type': 'distribution',
            'version': 2,
            'repositories': {
                'stalled': {
                    'source': {
                        'type': 'git',
                        'url': url,
                        'version': 'main',
                    }
                }
            }
        }
        cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
        provider = CachedSourceManifestProvider(cache, Distribution.default_source_manifest_providers)
        repo = SourceRepositorySpecification('stalled', {
            'type': 'git',
            'url': url,
            'version': 'main',
        })
        with patch.dict(os.environ, {'ROSDISTRO_GIT_TIMEOUT': '1'}, clear=False), \
                _capture_logs(level=logging.ERROR) as records:
            repo_cache = provider(repo)

        assert repo_cache is None
        error_msgs = [r.getMessage() for r in records if r.levelno == logging.ERROR]
        assert len(error_msgs) == 1
        assert 'timed out' in error_msgs[0]
        assert 'git_source_manifest_provider()' in error_msgs[0]


def test_github_primary_rate_limit_fails_fast_when_reset_exceeds_budget():
    url = 'https://api.github.com/repos/ros/genmsg/git/trees/main?recursive=1'
    reset_ts = str(int(time.time()) + 1800)  # 30 minutes in the future
    err = HTTPError(
        url, 403, 'rate limit exceeded',
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset_ts},
        io.BytesIO(b'{"message": "API rate limit exceeded"}'),
    )
    sleeps = []
    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch.object(github_mp, '_rate_limit_warned', False), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=err) as mock_open, \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append), \
            _capture_logs(level=logging.WARNING) as records:
        try:
            github_mp._get_url_contents(url)
            assert False, 'Expected HTTPError'
        except HTTPError as raised:
            assert raised.code == 403

    assert mock_open.call_count == 1
    assert sleeps == []
    assert any('GITHUB_TOKEN' in r.getMessage() for r in records)


def test_github_primary_rate_limit_retries_when_reset_within_budget():
    url = 'https://api.github.com/repos/ros/genmsg/git/trees/main?recursive=1'
    reset_ts = str(int(time.time()) + 3)
    err = HTTPError(
        url, 403, 'rate limit exceeded',
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset_ts},
        io.BytesIO(b'{"message": "API rate limit exceeded"}'),
    )
    sleeps = []
    responses = [err, _make_mock_http_response('{"ok": true}')]

    def fake_urlopen(req, timeout=None):
        assert timeout is not None and timeout > 0
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        body = github_mp._get_url_contents(url)

    assert body == '{"ok": true}'
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 30


def test_github_retry_after_header_exceeds_vs_within_budget():
    url = 'https://api.github.com/repos/ros/genmsg/git/trees/main?recursive=1'
    # Retry-After=120 exceeds budget=30 -> fail fast with 0 sleeps
    err_long = HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '120'}, None)
    sleeps = []
    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=err_long) as mock_open, \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        try:
            github_mp._get_url_contents(url)
            assert False, 'Expected HTTPError'
        except HTTPError as raised:
            assert raised.code == 429
    assert mock_open.call_count == 1
    assert sleeps == []

    # Retry-After=4 within budget=30 -> sleep 4s and succeed
    err_short = HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '4'}, None)
    responses = [err_short, _make_mock_http_response('recovered')]
    sleeps.clear()

    def fake_urlopen(req, timeout=None):
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        assert github_mp._get_url_contents(url) == 'recovered'
    assert sleeps == [4.0]


def test_github_permanent_403_fails_fast():
    url = 'https://api.github.com/repos/ros/blocked/git/trees/main?recursive=1'
    err = HTTPError(
        url, 403, 'Forbidden',
        {'X-RateLimit-Remaining': '4999'},
        io.BytesIO(b'{"message": "Repository access blocked"}'),
    )
    sleeps = []
    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=err) as mock_open, \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        try:
            github_mp._get_url_contents(url)
            assert False, 'Expected HTTPError'
        except HTTPError as raised:
            assert raised.code == 403
    assert mock_open.call_count == 1
    assert sleeps == []


def test_github_headerless_403_total_sleep_bounded_by_retry_max():
    url = 'https://raw.githubusercontent.com/ros/genmsg/master/package.xml'
    sleeps = []

    def always_403(req, timeout=None):
        raise HTTPError(url, 403, 'Forbidden', {}, None)

    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 10), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=always_403), \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        try:
            github_mp._get_url_contents(url)
            assert False, 'Expected HTTPError'
        except HTTPError as raised:
            assert raised.code == 403

    assert sum(sleeps) <= 10.0 + 1e-9
    assert sum(sleeps) == 10.0

    # ROSDISTRO_RETRY_MAX = 0 disables retries completely
    sleeps.clear()
    with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 0), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=always_403) as mock_open, \
            patch('rosdistro.manifest_provider.github.time.sleep', side_effect=sleeps.append):
        try:
            github_mp._get_url_contents(url)
            assert False, 'Expected HTTPError'
        except HTTPError:
            pass
    assert mock_open.call_count == 1
    assert sleeps == []


def test_github_http_timeout_on_stalled_server():
    stall_event = threading.Event()
    with _stalled_http_server(stall_event) as port:
        url = 'http://127.0.0.1:%d/stalled' % port
        with patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '0.5'}, clear=False):
            t0 = time.monotonic()
            try:
                github_mp._get_url_contents(url)
                assert False, 'Expected timeout'
            except (URLError, TimeoutError, socket.timeout):
                pass
            assert time.monotonic() - t0 < 4.0


def test_gitlab_http_timeout_and_pagination_guards():
    seen_timeouts = []
    # Circular Link header must stop instead of looping forever
    def circular_urlopen(req, timeout=None):
        seen_timeouts.append(timeout)
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        return _make_mock_http_response(
            json.dumps([{'path': 'pkg/package.xml'}]),
            headers={'Link': '<%s>; rel="next"' % url},
        )

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=circular_urlopen):
        items = list(gitlab_mp._gitlab_paged_api_query('gitlab.com', 'org/repo', 'repository/tree', {}))
    assert len(items) == 1
    assert all(t == DEFAULT_HTTP_TIMEOUT for t in seen_timeouts)

    # Pagination total deadline aborts slow multi-page fetches
    page_counter = {'n': 0}

    def slow_paged_urlopen(req, timeout=None):
        page_counter['n'] += 1
        time.sleep(0.06)
        next_url = 'https://gitlab.com/api/v4/projects/org%2Frepo/repository/tree?page=' + str(page_counter['n'] + 1)
        return _make_mock_http_response(
            json.dumps([{'path': 'p%d/package.xml' % page_counter['n']}]),
            headers={'Link': '<%s>; rel="next"' % next_url},
        )

    with patch.dict(os.environ, {'ROSDISTRO_GITLAB_PAGINATION_TIMEOUT': '0.1'}, clear=False), \
            patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=slow_paged_urlopen):
        try:
            list(gitlab_mp._gitlab_paged_api_query('gitlab.com', 'org/repo', 'repository/tree', {}))
            assert False, 'Expected pagination timeout RuntimeError'
        except RuntimeError as e:
            assert 'timed out' in str(e)


def test_bitbucket_and_tar_pass_http_timeout():
    bb_repo = ReleaseRepositorySpecification('rospeex', {
        'packages': ['rospeex_msgs'],
        'tags': {'release': 'release/indigo/{package}/{version}'},
        'url': 'https://bitbucket.org/rospeex/rospeex-release.git',
        'version': '2.14.7-0',
    })
    recorded_timeouts = []

    def timeout_urlopen(req, timeout=None):
        recorded_timeouts.append(timeout)
        raise socket.timeout('timed out')

    with patch.object(bb_repo, 'has_remote_tag', return_value=True), \
            patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '7'}, clear=False), \
            patch('rosdistro.manifest_provider.bitbucket.urlopen', side_effect=timeout_urlopen):
        try:
            bitbucket_manifest_provider('indigo', bb_repo, 'rospeex_msgs')
            assert False, 'Expected RuntimeError on bitbucket timeout'
        except RuntimeError as e:
            assert 'timed out' in str(e)
    assert recorded_timeouts == [7.0]

    recorded_timeouts.clear()
    tar_rel_repo = ReleaseRepositorySpecification('genmsg', {
        'url': 'https://example.com/genmsg.tar.gz',
        'tags': {'release': 'genmsg-0.5.16'},
        'version': '0.5.16-1',
        'type': 'tar',
    })
    with patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '9'}, clear=False), \
            patch('rosdistro.manifest_provider.tar.urlopen', side_effect=timeout_urlopen):
        try:
            tar_manifest_provider('melodic', tar_rel_repo, 'genmsg')
            assert False, 'Expected timeout'
        except (socket.timeout, TimeoutError):
            pass
    assert recorded_timeouts == [9.0]

    recorded_timeouts.clear()
    tar_src_repo = SourceRepositorySpecification('genmsg', {
        'url': 'https://example.com/genmsg.tar.gz',
        'type': 'tar',
    })
    with patch.dict(os.environ, {'ROSDISTRO_HTTP_TIMEOUT': '11'}, clear=False), \
            patch('rosdistro.manifest_provider.tar.urlopen', side_effect=timeout_urlopen):
        try:
            tar_source_manifest_provider(tar_src_repo)
            assert False, 'Expected RuntimeError on tar source timeout'
        except RuntimeError as e:
            assert 'timed out' in str(e)
    assert recorded_timeouts == [11.0]


def test_loader_load_url_retries_on_read_timeout():
    attempts = {'n': 0}

    def flaky_urlopen(url, timeout=None):
        attempts['n'] += 1
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        if attempts['n'] == 1:
            resp.read.side_effect = socket.timeout('read timed out')
        else:
            resp.read.return_value = b'hello'
        return resp

    with patch('rosdistro.loader.urlopen', side_effect=flaky_urlopen), \
            patch('rosdistro.loader.time.sleep'):
        assert load_url('https://example.com/index.yaml', retry=1, retry_period=0, timeout=5) == 'hello'
    assert attempts['n'] == 2


def test_freeze_source_worker_handles_timeout_without_deadlock():
    work_queue = queue.Queue()
    src_repo = SourceRepositorySpecification('slow_repo', {
        'type': 'git',
        'url': 'https://example.com/slow.git',
        'version': 'main',
    })
    work_queue.put((src_repo, 'main', False))

    with patch('rosdistro.freeze_source._get_repo_info',
               side_effect=subprocess.TimeoutExpired(['git', 'ls-remote'], 60)):
        _worker(work_queue)

    # Queue must be marked done so work_queue.join() will not deadlock
    assert work_queue.unfinished_tasks == 0
    assert src_repo.version == 'main'


def test_is_unauthenticated_http_url():
    assert is_unauthenticated_http_url('https://github.com/ros/genmsg.git') is True
    assert is_unauthenticated_http_url('http://gitlab.com/org/repo.git') is True
    assert is_unauthenticated_http_url('git@github.com:org/private.git') is False
    assert is_unauthenticated_http_url('ssh://git@github.com/org/private.git') is False
    assert is_unauthenticated_http_url('https://token@github.com/org/private.git') is False
    assert is_unauthenticated_http_url('https://user:pass@github.com/org/private.git') is False
    assert is_unauthenticated_http_url('git://github.com/ros/genmsg.git') is False


def test_github_source_permanent_errors_skip_git_fallback():
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'andino': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/Ekumen-OS/andino.git',
                    'version': 'humble',
                }
            }
        }
    }
    repo = SourceRepositorySpecification('andino', {
        'type': 'git',
        'url': 'https://github.com/Ekumen-OS/andino.git',
        'version': 'humble',
    })

    permanent_cases = [
        lambda url: HTTPError(url, 404, 'Not Found', {}, None),
        lambda url: HTTPError(url, 410, 'Gone', {}, None),
        lambda url: HTTPError(url, 451, 'Unavailable For Legal Reasons', {}, None),
        lambda url: HTTPError(
            url, 403, 'Forbidden',
            {'X-RateLimit-Remaining': '4999'},
            io.BytesIO(b'{"message": "Repository access blocked"}'),
        ),
    ]

    for make_err in permanent_cases:
        cache = DistributionCache('humble', distribution_file_data=dist_file_data)
        git_fallback = MagicMock(side_effect=AssertionError('git fallback must not run'))
        provider = CachedSourceManifestProvider(
            cache, [github_mp.github_source_manifest_provider, git_fallback]
        )

        def fail_urlopen(req, timeout=None, _make_err=make_err):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            raise _make_err(url)

        with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fail_urlopen), \
                patch('rosdistro.manifest_provider.github.time.sleep') as mock_sleep, \
                _capture_logs(level=logging.ERROR) as records:
            result = provider(repo)

        assert result is None
        git_fallback.assert_not_called()
        mock_sleep.assert_not_called()
        error_msgs = [r.getMessage() for r in records if r.levelno == logging.ERROR]
        assert len(error_msgs) == 1
        assert 'github_source_manifest_provider()' in error_msgs[0]


def test_github_source_transient_and_ssh_errors_still_fall_back_to_git():
    from rosdistro.source_repository_cache import SourceRepositoryCache

    dist_file_data = {'type': 'distribution', 'version': 2, 'repositories': {}}
    https_repo = SourceRepositorySpecification('andino', {
        'type': 'git',
        'url': 'https://github.com/Ekumen-OS/andino.git',
        'version': 'humble',
    })
    ssh_repo = SourceRepositorySpecification('priv', {
        'type': 'git',
        'url': 'git@github.com:org/priv.git',
        'version': 'main',
    })
    token_url_repo = SourceRepositorySpecification('priv2', {
        'type': 'git',
        'url': 'https://abcdef0123456789abcdef0123456789abcdef01@github.com/org/priv2.git',
        'version': 'main',
    })

    reset_ts = str(int(time.time()) + 1800)
    fallback_cases = [
        # Primary rate limit 403
        (https_repo, lambda url: HTTPError(
            url, 403, 'rate limit exceeded',
            {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset_ts},
            io.BytesIO(b'{"message": "API rate limit exceeded"}'),
        )),
        # Secondary rate limit 403
        (https_repo, lambda url: HTTPError(
            url, 403, 'Forbidden',
            {'X-RateLimit-Remaining': '4999'},
            io.BytesIO(b'{"message": "You have exceeded a secondary rate limit"}'),
        )),
        # Headerless 403
        (https_repo, lambda url: HTTPError(url, 403, 'Forbidden', {}, None)),
        # 429 Too Many Requests
        (https_repo, lambda url: HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '120'}, None)),
        # 401 Unauthorized (e.g. bad token on public repo)
        (https_repo, lambda url: HTTPError(url, 401, 'Unauthorized', {}, None)),
        # 503 Service Unavailable
        (https_repo, lambda url: HTTPError(url, 503, 'Service Unavailable', {}, None)),
        # 404 on SSH URL (may succeed via SSH keys)
        (ssh_repo, lambda url: HTTPError(url, 404, 'Not Found', {}, None)),
        # 404 on URL with embedded credentials
        (token_url_repo, lambda url: HTTPError(url, 404, 'Not Found', {}, None)),
    ]

    for repo, make_err in fallback_cases:
        cache = DistributionCache('humble', distribution_file_data=dist_file_data)
        expected_cache = SourceRepositoryCache.from_ref('deadbeef')
        git_fallback = MagicMock(return_value=expected_cache)
        provider = CachedSourceManifestProvider(
            cache, [github_mp.github_source_manifest_provider, git_fallback]
        )

        def fail_urlopen(req, timeout=None, _make_err=make_err):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            raise _make_err(url)

        with patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 0), \
                patch('rosdistro.manifest_provider.github.urlopen', side_effect=fail_urlopen):
            result = provider(repo)

        assert result is expected_cache
        assert git_fallback.call_count == 1

    # Truncated JSON tree also still falls back to git
    cache = DistributionCache('humble', distribution_file_data=dist_file_data)
    expected_cache = SourceRepositoryCache.from_ref('deadbeef')
    git_fallback = MagicMock(return_value=expected_cache)
    provider = CachedSourceManifestProvider(
        cache, [github_mp.github_source_manifest_provider, git_fallback]
    )
    with patch('rosdistro.manifest_provider.github.urlopen',
               return_value=_make_mock_http_response(json.dumps({'sha': 'abc', 'truncated': True, 'tree': []}))):
        result = provider(https_repo)
    assert result is expected_cache
    assert git_fallback.call_count == 1


def test_github_source_quotes_version_in_tree_url():
    repo = SourceRepositorySpecification('special', {
        'type': 'git',
        'url': 'https://github.com/org/special.git',
        'version': 'release/v1+beta#2',
    })
    seen_urls = []

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        seen_urls.append(url)
        raise HTTPError(url, 404, 'Not Found', {}, None)

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen):
        try:
            github_mp.github_source_manifest_provider(repo)
            assert False, 'Expected PermanentManifestError'
        except PermanentManifestError:
            pass

    assert seen_urls == [
        'https://api.github.com/repos/org/special/git/trees/release/v1%2Bbeta%232?recursive=1'
    ]


def test_gitlab_source_permanent_vs_transient_fallback():
    from rosdistro.source_repository_cache import SourceRepositoryCache

    dist_file_data = {'type': 'distribution', 'version': 2, 'repositories': {}}
    https_repo = SourceRepositorySpecification('gl_repo', {
        'type': 'git',
        'url': 'https://gitlab.com/org/gl_repo.git',
        'version': 'humble',
    })
    ssh_repo = SourceRepositorySpecification('gl_ssh', {
        'type': 'git',
        'url': 'git@gitlab.com:org/gl_ssh.git',
        'version': 'humble',
    })

    # Permanent failures (404, non-rate-limit 403, empty commits list) must NOT fall back to git
    permanent_responses = [
        lambda url: HTTPError(url, 404, 'Not Found', {}, None),
        lambda url: HTTPError(
            url, 403, 'Forbidden',
            {'RateLimit-Remaining': '99'},
            io.BytesIO(b'{"message": "403 Forbidden"}'),
        ),
        lambda url: _make_mock_http_response('[]'),
    ]
    for make_resp in permanent_responses:
        cache = DistributionCache('humble', distribution_file_data=dist_file_data)
        git_fallback = MagicMock(side_effect=AssertionError('git fallback must not run'))
        provider = CachedSourceManifestProvider(
            cache, [gitlab_mp.gitlab_source_manifest_provider, git_fallback]
        )

        def fake_urlopen(req, timeout=None, _make_resp=make_resp):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            out = _make_resp(url)
            if isinstance(out, Exception):
                raise out
            return out

        with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_urlopen), \
                _capture_logs(level=logging.ERROR):
            assert provider(https_repo) is None
        git_fallback.assert_not_called()

    # Transient / rate-limit / SSH errors MUST still fall back to git
    transient_cases = [
        (https_repo, lambda url: HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '60'}, None)),
        (https_repo, lambda url: HTTPError(
            url, 403, 'Forbidden', {'RateLimit-Remaining': '0'}, io.BytesIO(b'rate limit')
        )),
        (https_repo, lambda url: HTTPError(url, 502, 'Bad Gateway', {}, None)),
        (ssh_repo, lambda url: HTTPError(url, 404, 'Not Found', {}, None)),
    ]
    for repo, make_err in transient_cases:
        cache = DistributionCache('humble', distribution_file_data=dist_file_data)
        expected_cache = SourceRepositoryCache.from_ref('deadbeef')
        git_fallback = MagicMock(return_value=expected_cache)
        provider = CachedSourceManifestProvider(
            cache, [gitlab_mp.gitlab_source_manifest_provider, git_fallback]
        )

        def fail_urlopen(req, timeout=None, _make_err=make_err):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            raise _make_err(url)

        with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fail_urlopen):
            assert provider(repo) is expected_cache
        assert git_fallback.call_count == 1


def test_release_manifest_providers_skip_git_fallback_on_permanent_errors():
    dist_file_data = {'type': 'distribution', 'version': 2, 'repositories': {}}
    gh_repo = ReleaseRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros-gbp/genmsg-release.git',
        'tags': {'release': 'release/melodic/{package}/{version}'},
        'version': '0.5.11-0',
    })
    gl_repo = ReleaseRepositorySpecification('tracetools', {
        'url': 'https://gitlab.com/ros-tracing/tracetools-release.git',
        'tags': {'release': 'release/foxy/{package}/{version}'},
        'version': '1.0.0-1',
    })
    bb_repo = ReleaseRepositorySpecification('rospeex', {
        'url': 'https://bitbucket.org/rospeex/rospeex-release.git',
        'tags': {'release': 'release/indigo/{package}/{version}'},
        'version': '2.14.7-0',
    })

    # 1. Missing remote tag on GitHub / Bitbucket skips git fallback
    for mp, repo, pkg in (
        (github_mp.github_manifest_provider, gh_repo, 'genmsg'),
        (bitbucket_manifest_provider, bb_repo, 'rospeex'),
    ):
        cache = DistributionCache('melodic', distribution_file_data=dist_file_data)
        git_fallback = MagicMock(side_effect=AssertionError('git fallback must not run'))
        provider = CachedManifestProvider(cache, [mp, git_fallback])
        with patch.object(repo, 'has_remote_tag', return_value=False), \
                _capture_logs(level=logging.ERROR):
            assert provider('melodic', repo, pkg) is None
        git_fallback.assert_not_called()

    # 2. HTTP 404 on package.xml from GitHub / GitLab / Bitbucket skips git fallback
    for mp, target, repo, pkg, patch_tag in (
        (github_mp.github_manifest_provider, 'rosdistro.manifest_provider.github.urlopen', gh_repo, 'genmsg', True),
        (gitlab_mp.gitlab_manifest_provider, 'rosdistro.manifest_provider.gitlab.urlopen', gl_repo, 'tracetools', False),
        (bitbucket_manifest_provider, 'rosdistro.manifest_provider.bitbucket.urlopen', bb_repo, 'rospeex', True),
    ):
        cache = DistributionCache('melodic', distribution_file_data=dist_file_data)
        git_fallback = MagicMock(side_effect=AssertionError('git fallback must not run'))
        provider = CachedManifestProvider(cache, [mp, git_fallback])

        def err_404(req, timeout=None):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            raise HTTPError(url, 404, 'Not Found', {}, None)

        with patch(target, side_effect=err_404), \
                _capture_logs(level=logging.ERROR):
            if patch_tag:
                with patch.object(repo, 'has_remote_tag', return_value=True):
                    assert provider('melodic', repo, pkg) is None
            else:
                assert provider('melodic', repo, pkg) is None
        git_fallback.assert_not_called()

    # 3. Transient 500 on GitHub release package.xml still falls back to git
    cache = DistributionCache('melodic', distribution_file_data=dist_file_data)
    git_fallback = MagicMock(return_value='<package><name>genmsg</name></package>')
    provider = CachedManifestProvider(cache, [github_mp.github_manifest_provider, git_fallback])
    with patch.object(gh_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.github.urlopen',
                  side_effect=lambda req, timeout=None: (_ for _ in ()).throw(HTTPError(str(req), 500, 'Server Error', {}, None))):
        assert '<name>genmsg</name>' in provider('melodic', gh_repo, 'genmsg')
    assert git_fallback.call_count == 1


def test_within_run_ls_remote_failure_skips_source_fetch_retry():
    import datetime
    from rosdistro.source_repository_cache import SourceRepositoryCache

    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'broken_repo': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/example/broken_repo.git',
                    'version': 'humble',
                }
            }
        }
    }
    # Even when cross-run failure_retry_delta is 0, within-run deduplication must skip retry
    cache = DistributionCache(
        'humble',
        distribution_file_data=dist_file_data,
        config={'source_package_resources': {'package.xml': True}, 'failure_retry_delta': 0},
    )
    stale_entry = SourceRepositoryCache.from_ref('deadbeef')
    stale_entry.add('broken_pkg', '', '<package><name>broken_pkg</name></package>', 'package.xml')
    stale_entry.set_last_update_time(datetime.datetime.now() - datetime.timedelta(hours=2))
    cache.source_repo_resources['broken_repo'] = stale_entry

    # Simulate git ls-remote returning empty output (branch removed upstream)
    with patch('rosdistro.distribution_cache.Git') as mock_git, \
            _capture_logs(level=logging.WARNING):
        mock_git.return_value.command.return_value = {'returncode': 0, 'output': '', 'error': ''}
        cache.update_distribution([dist_file_data])

    assert 'broken_repo' not in cache.source_repo_resources
    assert 'broken_repo' in cache.failed_source_repos
    assert cache.failed_source_repos['broken_repo']['permanent'] is True

    # Phase 3 source manifest provider must NOT hit the network again in the same run
    mock_source_provider = MagicMock(side_effect=AssertionError('Should not retry failed ls-remote repo'))
    cached_src_mp = CachedSourceManifestProvider(cache, [mock_source_provider])
    src_repo = cache.distribution_file.repositories['broken_repo'].source_repository
    assert cached_src_mp(src_repo) is None
    mock_source_provider.assert_not_called()

    # Special case: non-timeout git ls-remote failure on github.com WITH GITHUB_TOKEN still lets API try
    cache_with_token = DistributionCache(
        'humble',
        distribution_file_data=dist_file_data,
        config={'source_package_resources': {'package.xml': True}},
    )
    stale_entry2 = SourceRepositoryCache.from_ref('deadbeef')
    stale_entry2.add('broken_pkg', '', '<package><name>broken_pkg</name></package>', 'package.xml')
    stale_entry2.set_last_update_time(datetime.datetime.now() - datetime.timedelta(hours=2))
    cache_with_token.source_repo_resources['broken_repo'] = stale_entry2

    with patch.dict(os.environ, {'GITHUB_TOKEN': 'ghp_test_token'}, clear=False), \
            patch('rosdistro.distribution_cache.Git') as mock_git, \
            _capture_logs(level=logging.WARNING):
        mock_git.return_value.command.return_value = {
            'returncode': 128, 'output': '', 'error': 'could not read Username'
        }
        cache_with_token.update_distribution([dist_file_data])

    assert 'broken_repo' not in cache_with_token.source_repo_resources
    assert 'broken_repo' not in cache_with_token.failed_source_repos
    expected_cache = SourceRepositoryCache.from_ref('cafebabe')
    api_provider = MagicMock(return_value=expected_cache)
    cached_src_mp2 = CachedSourceManifestProvider(cache_with_token, [api_provider])
    assert cached_src_mp2(src_repo) is expected_cache
    assert api_provider.call_count == 1


def test_within_run_source_and_release_failure_deduplication():
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'repo_a': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/example/shared_broken.git',
                    'version': 'humble',
                }
            },
            'repo_b': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/example/shared_broken.git',
                    'version': 'humble',
                }
            },
        }
    }
    cache = DistributionCache(
        'humble',
        distribution_file_data=dist_file_data,
        config={'source_package_resources': {'package.xml': True}},
    )
    failing_src_mp = MagicMock(side_effect=PermanentManifestError('404 Not Found'))
    cached_src_mp = CachedSourceManifestProvider(cache, [failing_src_mp])
    dist = Distribution(cache.distribution_file, source_manifest_providers=[cached_src_mp])

    with _capture_logs(level=logging.ERROR) as records:
        assert dist.get_source_repo_resources('repo_a') is None
        # Second call for repo_a hits Distribution in-memory cache
        assert dist.get_source_repo_resources('repo_a') is None
        # repo_b shares the exact same broken (url, version) and skips provider
        assert dist.get_source_repo_resources('repo_b') is None

    assert failing_src_mp.call_count == 1
    assert len([r for r in records if r.levelno == logging.ERROR]) == 1

    # Release provider deduplicates failed (url, tag) across resources without affecting sibling tags
    rel_repo = ReleaseRepositorySpecification('multi', {
        'packages': ['pkg_a', 'pkg_b'],
        'tags': {'release': 'release/humble/{package}/{version}'},
        'url': 'https://github.com/example/multi-release.git',
        'version': '1.0.0-1',
    })
    rel_calls = []

    def fake_rel_mp(_dist_name, _repo, pkg_name, filepath='package.xml'):
        rel_calls.append((pkg_name, filepath))
        if pkg_name == 'pkg_a':
            raise PermanentManifestError('tag missing')
        return '<package><name>pkg_b</name></package>'

    cached_rel_mp = CachedManifestProvider(cache, [fake_rel_mp])
    with _capture_logs(level=logging.ERROR):
        assert cached_rel_mp('humble', rel_repo, 'pkg_a', 'package.xml') is None
        # Repeated fetch of package.xml and optional docs for the same broken tag must skip
        assert cached_rel_mp('humble', rel_repo, 'pkg_a', 'package.xml') is None
        assert cached_rel_mp('humble', rel_repo, 'pkg_a', 'README.md') is None
        assert cached_rel_mp('humble', rel_repo, 'pkg_a', 'CHANGELOG.rst') is None
        # Sibling package with its own tag still fetches normally
        assert '<name>pkg_b</name>' in cached_rel_mp('humble', rel_repo, 'pkg_b', 'package.xml')

    assert rel_calls == [('pkg_a', 'package.xml'), ('pkg_b', 'package.xml')]


def test_cross_run_negative_cache_ttl_and_invalidation():
    import datetime
    import yaml
    from rosdistro.source_repository_cache import SourceRepositoryCache

    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'andino': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/Ekumen-OS/andino.git',
                    'version': 'humble',
                }
            }
        }
    }
    # Run 1: andino fails and gets recorded in failed_source_repos
    cache1 = DistributionCache(
        'humble',
        distribution_file_data=dist_file_data,
        config={'source_package_resources': {'package.xml': True}, 'failure_retry_delta': 3600},
    )
    failing_mp = MagicMock(side_effect=PermanentManifestError('404 Not Found'))
    with _capture_logs(level=logging.ERROR):
        assert CachedSourceManifestProvider(cache1, [failing_mp])(
            cache1.distribution_file.repositories['andino'].source_repository
        ) is None
    assert 'andino' in cache1.failed_source_repos
    assert cache1.failed_source_repos['andino']['permanent'] is True

    # Persist to YAML and reload in Run 2
    serialized = yaml.safe_dump(cache1.get_data(version=3))
    loaded = yaml.safe_load(serialized)
    cache2 = DistributionCache('humble', data=loaded)
    cache2.update_distribution([dist_file_data])
    assert 'andino' in cache2.failed_source_repos

    # Run 2 within TTL: skips network fetch completely
    never_called = MagicMock(side_effect=AssertionError('Must skip repo within failure_retry_delta'))
    assert CachedSourceManifestProvider(cache2, [never_called])(
        cache2.distribution_file.repositories['andino'].source_repository
    ) is None
    never_called.assert_not_called()

    # Run 3: distribution.yaml updates branch from humble -> main -> immediately invalidated and retried
    fixed_dist_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'andino': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/Ekumen-OS/andino.git',
                    'version': 'main',
                }
            }
        }
    }
    cache3 = DistributionCache('humble', data=loaded)
    cache3.update_distribution([fixed_dist_data])
    assert 'andino' not in cache3.failed_source_repos
    ok_repo_cache = SourceRepositoryCache.from_ref('1234567890')
    ok_mp = MagicMock(return_value=ok_repo_cache)
    assert CachedSourceManifestProvider(cache3, [ok_mp])(
        cache3.distribution_file.repositories['andino'].source_repository
    ) is ok_repo_cache
    assert ok_mp.call_count == 1
    assert 'andino' in cache3.source_repo_resources
    assert 'andino' not in cache3.failed_source_repos

    # Run 4: TTL expired -> pruned during update_distribution and retried
    cache4 = DistributionCache('humble', data=loaded)
    cache4.failed_source_repos['andino']['failed_at'] = (
        datetime.datetime.now() - datetime.timedelta(hours=2)
    ).isoformat()
    cache4.update_distribution([dist_file_data])
    assert 'andino' not in cache4.failed_source_repos

    # apply_config disabling source clears failed_source_repos
    cache5 = DistributionCache('humble', data=loaded)
    assert 'andino' in cache5.failed_source_repos
    cache5.apply_config({})
    assert cache5.failed_source_repos == {}


def test_zero_package_repo_is_cache_hit_and_preserves_metadata():
    import datetime
    from rosdistro.source_repository_cache import SourceRepositoryCache

    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'docs_only': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/example/docs_only.git',
                    'version': 'main',
                }
            }
        }
    }
    cache = DistributionCache(
        'humble',
        distribution_file_data=dist_file_data,
        config={
            'source_package_resources': {'package.xml': True},
            'source_repo_metadata': {'readme': {'enabled': True, 'max_lines': 3}},
        },
    )
    empty_pkg_cache = SourceRepositoryCache.from_ref('abcdef1234')
    empty_pkg_cache.set_readme('line1\nline2\nline3\nline4\nline5')
    assert len(empty_pkg_cache) == 0

    provider_fn = MagicMock(return_value=empty_pkg_cache)
    cached_mp = CachedSourceManifestProvider(cache, [provider_fn])
    repo = cache.distribution_file.repositories['docs_only'].source_repository

    first = cached_mp(repo)
    assert first is empty_pkg_cache
    assert provider_fn.call_count == 1
    assert isinstance(first.last_update_time(), datetime.datetime)
    assert 'Truncated content at 3 of 5 lines' in first.readme()

    # Second call with a fresh CachedSourceManifestProvider must hit the cache (0 provider calls)
    second_provider_fn = MagicMock(side_effect=AssertionError('0-package repo must be a cache hit'))
    second = CachedSourceManifestProvider(cache, [second_provider_fn])(repo)
    assert second is empty_pkg_cache
    second_provider_fn.assert_not_called()

    # Distribution metadata crawl query also recognizes 0-package repo
    dist = Distribution(cache.distribution_file, source_manifest_providers=[])
    dist._source_repo_resources['docs_only'] = empty_pkg_cache
    assert dist.is_source_metadata_crawled('readme', repo_name='docs_only') is True
