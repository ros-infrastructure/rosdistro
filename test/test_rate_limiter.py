import io
import json
import logging
import os
import threading
import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from rosdistro import logger
from rosdistro.distribution_cache import DistributionCache, _resolve_git_remote_ref_details
from rosdistro.distribution_cache_generator import generate_distribution_cache
from rosdistro.index import Index
from rosdistro.manifest_provider import git as git_mp
from rosdistro.manifest_provider import github as github_mp
from rosdistro.manifest_provider import gitlab as gitlab_mp
from rosdistro.manifest_provider.cache import CachedSourceManifestProvider
from rosdistro.rate_limiter import (
    HostRateLimitError,
    RateLimiter,
    extract_host,
    is_git_rate_limit_error,
    parse_rate_limit_reset,
    parse_retry_after,
    use_rate_limiter,
)
from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro.source_repository_specification import SourceRepositorySpecification


def _mock_http_response(body, headers=None):
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.headers = headers or {}
    resp.getheader.side_effect = lambda name, default=None: (headers or {}).get(name, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_rate_limiter_url_and_header_helpers():
    assert extract_host('https://API.GitHub.com/repos/ros/genmsg') == 'api.github.com'
    assert extract_host(Request('https://gitlab.com/api/v4/projects')) == 'gitlab.com'
    assert extract_host('git@github.com:ros/genmsg.git') == 'github.com'
    assert extract_host('ssh://git@gitlab.com/org/repo.git') == 'gitlab.com'
    assert extract_host('/tmp/local/repo') is None
    assert extract_host('./relative/repo') is None
    assert extract_host('file:///tmp/repo') is None
    assert extract_host('http://127.0.0.1:8080/repo') is None
    assert extract_host('http://localhost/repo') is None
    assert extract_host(r'C:\repos\pkg') is None

    assert parse_retry_after('12') == 12.0
    assert parse_retry_after('-5') == 0.0
    assert parse_retry_after('Wed, 21 Oct 2015 07:28:10 GMT', now_wall=1445412480.0) == 10.0
    assert parse_retry_after('not-a-date') is None
    assert parse_retry_after(MagicMock()) is None

    assert parse_rate_limit_reset('1700000100', now_wall=1700000040.0) == 60.0
    assert parse_rate_limit_reset('45') == 45.0
    assert parse_rate_limit_reset('bad') is None

    assert is_git_rate_limit_error('fatal: unable to access: The requested URL returned error: 429')
    assert is_git_rate_limit_error('remote: You have exceeded a secondary rate limit')
    assert is_git_rate_limit_error('HTTP 429 Too Many Requests')
    assert not is_git_rate_limit_error('fatal: repository not found (404)')
    assert not is_git_rate_limit_error('')


def test_rate_limiter_pacing_isolation_and_concurrency_cap():
    now = [100.0]
    sleeps = []

    def fake_sleep(dt):
        sleeps.append(dt)
        now[0] += dt

    limiter = RateLimiter(
        http_host_intervals={'api.github.com': 0.5, 'gitlab.com': 0.2},
        http_host_concurrency={'api.github.com': 2, 'gitlab.com': 2},
        monotonic_fn=lambda: now[0],
        wall_time_fn=lambda: 1700000000.0,
    )

    # First slot on each host is immediate; second slot on same host is paced
    with limiter.request_slot('http', 'api.github.com', sleep_fn=fake_sleep):
        pass
    with limiter.request_slot('http', 'gitlab.com', sleep_fn=fake_sleep):
        pass
    assert sleeps == []

    with limiter.request_slot('http', 'api.github.com', sleep_fn=fake_sleep):
        pass
    assert sleeps == [0.5]

    # Local/empty host is never paced
    with limiter.request_slot('http', None, sleep_fn=fake_sleep):
        pass
    assert sleeps == [0.5]

    # Concurrency cap restricts active in-flight slots per host and traces contended waits
    from rosdistro import cache_profiler as cache_profiler_mod

    cap_limiter = RateLimiter(
        http_host_intervals={'api.github.com': 0.0},
        http_host_concurrency={'api.github.com': 2},
    )
    active = [0]
    peak = [0]
    lock = threading.Lock()
    errors = []
    has_otel = cache_profiler_mod.HAS_OTEL_API and cache_profiler_mod.HAS_OTEL_SDK
    profiler = (
        cache_profiler_mod.CacheProfiler(enabled=True, record_spans=True)
        if has_otel else None
    )

    if profiler is not None:
        with cache_profiler_mod.use_profiler(profiler):
            # Uncontended acquire must not emit a wait span
            with cap_limiter.request_slot('http', 'api.github.com', fetch_type='github', target='fast'):
                pass
        assert 'http:rate_limit_wait' not in profiler.to_dict(include_trace_events=False)['rosdistroProfile']['by_operation']

    def worker(captured_ctx=None):
        try:
            if profiler is not None and captured_ctx is not None:
                with profiler.attach_thread_context(captured_ctx):
                    with cap_limiter.request_slot('http', 'api.github.com', fetch_type='github', target='cap'):
                        with lock:
                            active[0] += 1
                            peak[0] = max(peak[0], active[0])
                        time.sleep(0.05)
                        with lock:
                            active[0] -= 1
            else:
                with cap_limiter.request_slot('http', 'api.github.com', fetch_type='github', target='cap'):
                    with lock:
                        active[0] += 1
                        peak[0] = max(peak[0], active[0])
                    time.sleep(0.05)
                    with lock:
                        active[0] -= 1
        except Exception as e:
            errors.append(e)

    if profiler is not None:
        with cache_profiler_mod.use_profiler(profiler):
            with profiler.span('cap_test', kind='root'):
                captured = profiler.capture_thread_context()
                threads = [threading.Thread(target=worker, args=(captured,)) for _ in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        ops = profiler.to_dict(include_trace_events=False)['rosdistroProfile']['by_operation']
        assert 'http:rate_limit_wait' in ops
        assert ops['http:rate_limit_wait']['count'] >= 4
        assert ops['http:rate_limit_wait']['total_s'] > 0.05
        profiler.shutdown()
    else:
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    assert peak[0] == 2


def test_rate_limiter_coordinated_backoff_aimd_and_proactive_headers():
    now = [1000.0]
    wall = [1700000000.0]
    limiter = RateLimiter(
        http_host_intervals={'api.github.com': 0.25},
        monotonic_fn=lambda: now[0],
        wall_time_fn=lambda: wall[0],
        jitter_fn=lambda d: d,
    )

    # Recording a rate limit increases current_interval and blocks host
    delay, should_warn, _ = limiter.record_rate_limit(
        'http', 'api.github.com', retry_after=2.0, remaining_budget=10.0,
    )
    assert delay == 2.0
    assert not should_warn
    bucket = limiter._get_bucket('http', 'api.github.com')
    assert bucket.current_interval >= 0.5

    # Another thread with budget=1.0 fails fast with HostRateLimitError
    try:
        with limiter.request_slot('http', 'api.github.com', remaining_budget=1.0):
            assert False, 'Expected HostRateLimitError'
    except HostRateLimitError as e:
        assert e.host == 'api.github.com'
        assert e.retry_after >= 1.9

    # Advancing clock and recording successes decays current_interval back toward base
    now[0] += 5.0
    for _ in range(10):
        limiter.record_success('http', 'api.github.com')
    assert bucket.current_interval == 0.25

    # Proactive block when 200 OK reports X-RateLimit-Remaining: 0
    limiter.record_response_headers(
        'http', 'api.github.com',
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(int(wall[0]) + 600)},
    )
    try:
        with limiter.request_slot('http', 'api.github.com', remaining_budget=30.0):
            assert False, 'Expected proactive HostRateLimitError'
    except HostRateLimitError as e:
        assert e.status_code == 403
        assert e.retry_after >= 599.0


def test_github_primary_rate_limit_short_circuits_parallel_workers():
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'repo_%d' % i: {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/org/repo_%d.git' % i,
                    'version': 'main',
                }
            }
            for i in range(6)
        },
    }
    index = Index({'type': 'index', 'version': 4, 'distributions': {'rolling': {'distribution': ['d.yaml']}}}, '')
    reset_ts = str(int(time.time()) + 1800)
    http_calls = []
    http_lock = threading.Lock()
    git_calls = []
    git_lock = threading.Lock()

    def rate_limited_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        with http_lock:
            http_calls.append(url)
        raise HTTPError(
            url, 403, 'rate limit exceeded',
            {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset_ts},
            io.BytesIO(b'{"message": "API rate limit exceeded"}'),
        )

    def fake_git_provider(repo, filepaths=None, metadata=None):
        with git_lock:
            git_calls.append(repo.name)
        return SourceRepositoryCache.from_ref('deadbeef')

    warnings = []

    class _CapHandler(logging.Handler):
        def emit(self, record):
            if record.levelno == logging.WARNING:
                warnings.append(record.getMessage())

    handler = _CapHandler()
    logger.addHandler(handler)
    limiter = RateLimiter(http_host_intervals={'api.github.com': 0.0}, http_host_concurrency={'api.github.com': 1})
    try:
        with patch('rosdistro.distribution_cache_generator._get_dist_file_data', return_value=dist_file_data), \
                patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 30), \
                patch.object(github_mp, '_rate_limit_warned', False), \
                patch('rosdistro.manifest_provider.github.urlopen', side_effect=rate_limited_urlopen), \
                patch('rosdistro.distribution.Distribution.default_source_manifest_providers',
                      [github_mp.github_source_manifest_provider, fake_git_provider]):
            cache = generate_distribution_cache(
                index, 'rolling', preclean=True, include_source=True, max_workers=4, rate_limiter=limiter,
            )
    finally:
        logger.removeHandler(handler)

    # First worker trips the rate limit; the other 5 short-circuit immediately and fall back to git
    assert len(http_calls) == 1
    assert sorted(git_calls) == ['repo_%d' % i for i in range(6)]
    assert len(cache.source_repo_resources) == 6
    assert len([w for w in warnings if 'GITHUB_TOKEN' in w]) == 1


def test_gitlab_rate_limit_backoff_and_short_circuit():
    url = 'https://gitlab.com/api/v4/projects/org%2Frepo/repository/commits'
    sleeps = []
    attempts = [0]

    def retry_then_ok(req, timeout=None):
        attempts[0] += 1
        if attempts[0] == 1:
            raise HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '2'}, None)
        return _mock_http_response('[{"id": "abc"}]')

    limiter = RateLimiter(http_host_intervals={'gitlab.com': 0.0})
    with use_rate_limiter(limiter), \
            patch.object(gitlab_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=retry_then_ok), \
            patch('rosdistro.manifest_provider.gitlab.time.sleep', side_effect=sleeps.append):
        with gitlab_mp._gitlab_urlopen(url) as res:
            assert json.loads(res.read().decode('utf-8')) == [{'id': 'abc'}]
    assert attempts[0] == 2
    assert sleeps == [2.0]

    # Headerless 403 on GitLab fails fast without retrying
    sleeps.clear()
    with patch.object(gitlab_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch('rosdistro.manifest_provider.gitlab.urlopen',
                  side_effect=HTTPError(url, 403, 'Forbidden', {}, None)) as mock_open, \
            patch('rosdistro.manifest_provider.gitlab.time.sleep', side_effect=sleeps.append):
        try:
            with gitlab_mp._gitlab_urlopen(url):
                assert False, 'Expected HTTPError'
        except HTTPError as e:
            assert e.code == 403
    assert mock_open.call_count == 1
    assert sleeps == []

    # RateLimit-Reset far in the future fails fast and short-circuits next request
    reset_ts = str(int(time.time()) + 600)
    limiter2 = RateLimiter(http_host_intervals={'gitlab.com': 0.0})
    with use_rate_limiter(limiter2), \
            patch.object(gitlab_mp, 'ROSDISTRO_RETRY_MAX', 30), \
            patch.object(gitlab_mp, '_rate_limit_warned', False), \
            patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=HTTPError(
                url, 429, 'Too Many Requests',
                {'RateLimit-Remaining': '0', 'RateLimit-Reset': reset_ts}, None,
            )) as mock_open, \
            patch('rosdistro.manifest_provider.gitlab.time.sleep', side_effect=sleeps.append):
        try:
            with gitlab_mp._gitlab_urlopen(url):
                assert False, 'Expected HTTPError'
        except HTTPError as e:
            assert e.code == 429
        try:
            with gitlab_mp._gitlab_urlopen(url):
                assert False, 'Expected HostRateLimitError'
        except HostRateLimitError as e:
            assert e.host == 'gitlab.com'
    assert mock_open.call_count == 1
    assert sleeps == []


def test_git_clone_and_ls_remote_backoff_and_no_negative_cache_poisoning():
    # 1. Shallow clone retries HTTP 429 without degrading to full clone
    clone_calls = []
    sleeps = []

    class FakeGit(object):
        def __init__(self, cwd=None):
            self.cwd = cwd

        def version_gte(self, _v):
            return True

        def command(self, *args):
            clone_calls.append(args)
            if len(clone_calls) == 1:
                return {'returncode': 128, 'output': '', 'error': 'The requested URL returned error: 429'}
            return {'returncode': 0, 'output': '', 'error': ''}

    with patch('rosdistro.manifest_provider.git.Git', FakeGit), \
            patch('rosdistro.manifest_provider.git.time.sleep', side_effect=sleeps.append), \
            patch.dict(os.environ, {'ROSDISTRO_RETRY_MAX': '10'}, clear=False):
        with git_mp._temp_git_clone('https://github.com/org/repo.git', 'main') as path:
            assert os.path.isdir(path)
    assert len(clone_calls) == 2
    assert all('--depth' in c for c in clone_calls)
    assert sleeps == [1.0]

    # 2. Exhausted git 429 raises HostRateLimitError and does NOT poison failed_source_repos
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'rate_limited_repo': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/org/rate_limited.git',
                    'version': 'main',
                }
            }
        },
    }
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
    repo = SourceRepositorySpecification('rate_limited_repo', {
        'type': 'git',
        'url': 'https://github.com/org/rate_limited.git',
        'version': 'main',
    })

    class Always429Git(object):
        def __init__(self, cwd=None):
            pass

        def version_gte(self, _v):
            return True

        def command(self, *args):
            return {'returncode': 128, 'output': '', 'error': 'HTTP 429 Too Many Requests'}

    provider = CachedSourceManifestProvider(cache, [git_mp.git_source_manifest_provider])
    with patch('rosdistro.manifest_provider.git.Git', Always429Git), \
            patch.dict(os.environ, {'ROSDISTRO_RETRY_MAX': '0'}, clear=False):
        assert provider(repo) is None
    assert 'rate_limited_repo' not in cache.failed_source_repos
    assert not cache.is_source_repo_failed('rate_limited_repo', repo.url, repo.version)

    # 3. ls-remote retries HTTP 429, and when exhausted does not record failed_source_repos
    ls_calls = [0]
    ls_sleeps = []

    class FlakyLsRemoteGit(object):
        def command(self, *args):
            ls_calls[0] += 1
            if ls_calls[0] == 1:
                return {'returncode': 128, 'output': '', 'error': 'The requested URL returned error: 429'}
            return {'returncode': 0, 'output': 'deadbeef\trefs/heads/main\n', 'error': ''}

    with patch('rosdistro.distribution_cache.Git', FlakyLsRemoteGit), \
            patch('rosdistro.distribution_cache.time.sleep', side_effect=ls_sleeps.append), \
            patch.dict(os.environ, {'ROSDISTRO_RETRY_MAX': '10'}, clear=False):
        ref_hash, err_msg, res = _resolve_git_remote_ref_details('https://github.com/org/repo.git', 'main')
    assert ref_hash == 'deadbeef'
    assert err_msg is None
    assert ls_calls[0] == 2
    assert ls_sleeps == [1.0]

    # Exhausted ls-remote 429 marks rate_limited=True so _should_record_ls_remote_failure returns False
    with patch('rosdistro.distribution_cache.Git', Always429Git), \
            patch.dict(os.environ, {'ROSDISTRO_RETRY_MAX': '0'}, clear=False):
        ref_hash, err_msg, res = _resolve_git_remote_ref_details('https://github.com/org/repo.git', 'main')
    assert ref_hash is None
    assert res.get('rate_limited') is True
    assert not DistributionCache._should_record_ls_remote_failure(repo, res)
