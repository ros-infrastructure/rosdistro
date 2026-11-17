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
from rosdistro.manifest_provider import DEFAULT_HTTP_TIMEOUT, get_http_timeout
from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedSourceManifestProvider
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
