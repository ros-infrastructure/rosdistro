import datetime
import json
import re
import threading
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from rosdistro.distribution_cache import CacheConfig, DistributionCache
from rosdistro.distribution_cache_generator import _NeighborCachePool
from rosdistro.fetch_reporter import FetchReporter, use_reporter
from rosdistro.manifest_provider import github as github_mp
from rosdistro.rate_limiter import RateLimiter, use_rate_limiter
from rosdistro.source_repository_cache import SourceRepositoryCache


_FIELD_REF_RE = re.compile(r'([htq]\d+):\s*ref\(qualifiedName:\s*"([^"]+)"\)')


def _mock_http_response(body, headers=None):
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.headers = headers or {}
    resp.getheader.side_effect = lambda name, default=None: (headers or {}).get(name, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _make_sha(idx):
    return '%040x' % (idx + 1)


def _make_expired_cache(dist_name, repos, cached_shas, min_update_delta=3600):
    dist_data = {'type': 'distribution', 'version': 2, 'repositories': repos}
    cache = DistributionCache(
        dist_name,
        distribution_file_data=dist_data,
        config=CacheConfig({'source_package_resources': {'package.xml': True}, 'min_update_delta': min_update_delta}),
    )
    old_ts = datetime.datetime.now() - datetime.timedelta(hours=3)
    for repo_name, sha in cached_shas.items():
        rc = SourceRepositoryCache.from_ref(sha)
        rc.add(repo_name + '_pkg', '', '<package><name>%s_pkg</name></package>' % repo_name, 'package.xml')
        rc.set_last_update_time(old_ts)
        cache.source_repo_resources[repo_name] = rc
    return cache, dist_data


def _parse_graphql_request(req):
    assert req.get_full_url() == github_mp.GITHUB_GRAPHQL_URL
    assert req.get_method() == 'POST'
    assert req.get_header('Authorization') == 'Bearer test-token'
    assert req.get_header('Content-type') == 'application/json'
    payload = json.loads(req.data.decode('utf-8'))
    return payload['query']


def _build_graphql_response_from_query(query, ref_table, rate_limit=None, errors=None, repo_overrides=None):
    """Evaluate a generated GraphQL query against ``ref_table[(owner, name, qualified_name)]``."""
    data = {}
    repo_overrides = repo_overrides or {}
    # Split by top-level repository blocks
    pos = 0
    while True:
        m = re.search(r'(r\d+):\s*repository\(owner:\s*"([^"]+)",\s*name:\s*"([^"]+)"\)\s*\{', query[pos:])
        if not m:
            break
        alias, owner, name = m.group(1), m.group(2), m.group(3)
        body_start = pos + m.end()
        depth = 1
        i = body_start
        while i < len(query) and depth > 0:
            if query[i] == '{':
                depth += 1
            elif query[i] == '}':
                depth -= 1
            i += 1
        block = query[body_start:i - 1]
        pos = i

        if (owner, name) in repo_overrides:
            data[alias] = repo_overrides[(owner, name)]
            continue

        repo_obj = {}
        for field_alias, qname in _FIELD_REF_RE.findall(block):
            val = ref_table.get((owner, name, qname))
            repo_obj[field_alias] = val
        data[alias] = repo_obj

    data['rateLimit'] = rate_limit if rate_limit is not None else {
        'remaining': 4999, 'resetAt': '2026-11-20T07:00:00Z', 'cost': 1,
    }
    resp_obj = {'data': data}
    if errors is not None:
        resp_obj['errors'] = errors
    return _mock_http_response(json.dumps(resp_obj))


def test_graphql_batching_chunking_ttl_refresh_and_console_quiet(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.setenv('ROSDISTRO_GITHUB_GRAPHQL_BATCH_SIZE', '50')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    repos = {}
    cached_shas = {}
    ref_table = {}
    for i in range(120):
        name = 'repo_%03d' % i
        sha = _make_sha(i)
        repos[name] = {
            'source': {
                'type': 'git',
                'url': 'https://github.com/ros2/%s.git' % name,
                'version': 'rolling',
            }
        }
        # Repo 119 has moved so we verify dropped entries are detected and printed
        cached_shas[name] = _make_sha(9999) if i == 119 else sha
        ref_table[('ros2', name, 'refs/heads/rolling')] = {'target': {'oid': sha}}

    cache, dist_data = _make_expired_cache('rolling', repos, cached_shas)
    gql_calls = []
    call_lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        query = _parse_graphql_request(req)
        with call_lock:
            gql_calls.append(query)
        return _build_graphql_response_from_query(query, ref_table)

    before = datetime.datetime.now()
    reporter = FetchReporter(debug=False)
    with use_reporter(reporter), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        cache.update_distribution([dist_data], max_workers=4)

    assert len(gql_calls) == 3
    mock_git.return_value.command.assert_not_called()
    assert len(cache.source_repo_resources) == 119
    assert 'repo_119' not in cache.source_repo_resources
    # Kept entries must have refreshed _last_update_time
    assert cache.source_repo_resources['repo_000'].last_update_time() >= before

    out = capsys.readouterr().out
    assert 'github_graphql=120' in out
    assert 'kept=119, dropped=1' in out
    # Kept github_graphql entries must not spam stdout, while dropped entries still print
    assert 'repo_000 via=github_graphql' not in out
    assert 'repo_119 via=github_graphql (dropped' in out

    # Immediate second run should skip GraphQL & git ls-remote because TTL was refreshed
    gql_calls.clear()
    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git2:
        cache.update_distribution([dist_data], max_workers=4)
    assert gql_calls == []
    mock_git2.return_value.command.assert_not_called()


def test_graphql_ref_types_and_same_repo_grouping(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    branch_sha = _make_sha(1)
    light_tag_sha = _make_sha(2)
    annot_tag_sha = _make_sha(3)
    qual_sha = _make_sha(4)
    same_both_sha = _make_sha(5)

    ref_table = {
        ('ros2', 'rclcpp', 'refs/heads/rolling'): {'target': {'oid': branch_sha}},
        ('ros2', 'rclcpp', 'refs/tags/v1.0.0'): {'target': {'oid': light_tag_sha}},
        ('ros2', 'rclcpp', 'refs/tags/v2.0.0'): {'target': {'target': {'oid': annot_tag_sha}}},
        ('ros2', 'rclcpp', 'refs/heads/qualified'): {'target': {'oid': qual_sha}},
        ('ros2', 'rclcpp', 'refs/heads/same_ref'): {'target': {'oid': same_both_sha}},
        ('ros2', 'rclcpp', 'refs/tags/same_ref'): {'target': {'target': {'oid': same_both_sha}}},
    }

    queries = []

    def fake_urlopen(req, timeout=None):
        query = _parse_graphql_request(req)
        queries.append(query)
        return _build_graphql_response_from_query(query, ref_table)

    pairs = [
        ('https://github.com/ros2/rclcpp.git', 'rolling'),
        ('https://github.com/ROS2/rclcpp', 'rolling'),  # case + no .git variant grouped together
        ('https://github.com/ros2/rclcpp.git', 'v1.0.0'),
        ('https://github.com/ros2/rclcpp.git', 'v2.0.0'),
        ('https://github.com/ros2/rclcpp.git', 'refs/heads/qualified'),
        ('https://github.com/ros2/rclcpp.git', 'same_ref'),
    ]

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen):
        resolved = github_mp.resolve_github_remote_refs(pairs, batch_size=50, max_workers=2)

    assert len(queries) == 1
    # Only one repository alias r0 for all 6 pairs on ros2/rclcpp
    assert queries[0].count('repository(owner:') == 1
    assert 'q3: ref(qualifiedName: "refs/heads/qualified")' in queries[0]
    assert resolved == {
        ('https://github.com/ros2/rclcpp.git', 'rolling'): branch_sha,
        ('https://github.com/ROS2/rclcpp', 'rolling'): branch_sha,
        ('https://github.com/ros2/rclcpp.git', 'v1.0.0'): light_tag_sha,
        ('https://github.com/ros2/rclcpp.git', 'v2.0.0'): annot_tag_sha,
        ('https://github.com/ros2/rclcpp.git', 'refs/heads/qualified'): qual_sha,
        ('https://github.com/ros2/rclcpp.git', 'same_ref'): same_both_sha,
    }


def test_graphql_ambiguous_branch_and_tag_falls_back_to_ls_remote(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    branch_sha = _make_sha(10)
    tag_sha = _make_sha(11)
    repos = {
        'ambig': {
            'source': {'type': 'git', 'url': 'https://github.com/org/ambig.git', 'version': 'v1'},
        }
    }
    cache, dist_data = _make_expired_cache('rolling', repos, {'ambig': tag_sha})

    ref_table = {
        ('org', 'ambig', 'refs/heads/v1'): {'target': {'oid': branch_sha}},
        ('org', 'ambig', 'refs/tags/v1'): {'target': {'oid': tag_sha}},
    }

    def fake_urlopen(req, timeout=None):
        return _build_graphql_response_from_query(_parse_graphql_request(req), ref_table)

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {
            'returncode': 0,
            'output': '%s\trefs/heads/v1\n%s\trefs/tags/v1\n' % (branch_sha, tag_sha),
        }
        cache.update_distribution([dist_data], max_workers=2)

    mock_git.return_value.command.assert_called_once()
    assert 'ambig' in cache.source_repo_resources
    assert 'git_ls_remote=1' in capsys.readouterr().out


def test_graphql_mixed_hosts_and_partial_errors_fallback_without_poisoning(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    gh_ok_sha = _make_sha(20)
    gh_priv_sha = _make_sha(21)
    gl_sha = _make_sha(22)
    bb_sha = _make_sha(23)
    custom_sha = _make_sha(24)

    repos = {
        'gh_ok': {'source': {'type': 'git', 'url': 'https://github.com/org/gh_ok.git', 'version': 'main'}},
        'gh_missing': {'source': {'type': 'git', 'url': 'https://github.com/org/gh_missing.git', 'version': 'main'}},
        'gl_repo': {'source': {'type': 'git', 'url': 'https://gitlab.com/org/gl_repo.git', 'version': 'main'}},
        'bb_repo': {'source': {'type': 'git', 'url': 'https://bitbucket.org/org/bb_repo.git', 'version': 'main'}},
        'custom_repo': {'source': {'type': 'git', 'url': 'https://git.example.org/org/custom.git', 'version': 'main'}},
    }
    cache, dist_data = _make_expired_cache('rolling', repos, {
        'gh_ok': gh_ok_sha,
        'gh_missing': gh_priv_sha,
        'gl_repo': gl_sha,
        'bb_repo': bb_sha,
        'custom_repo': custom_sha,
    })

    ref_table = {('org', 'gh_ok', 'refs/heads/main'): {'target': {'oid': gh_ok_sha}}}

    def fake_urlopen(req, timeout=None):
        query = _parse_graphql_request(req)
        assert 'gitlab.com' not in query
        assert 'bitbucket.org' not in query
        assert 'example.org' not in query
        # 'gh_missing' sorts before 'gh_ok', so it receives alias r0
        m_missing = re.search(r'(r\d+):\s*repository\(owner:\s*"org",\s*name:\s*"gh_missing"\)', query)
        assert m_missing is not None
        return _build_graphql_response_from_query(
            query,
            ref_table,
            repo_overrides={('org', 'gh_missing'): None},
            errors=[{'type': 'NOT_FOUND', 'path': [m_missing.group(1)], 'message': 'Could not resolve'}],
        )

    ls_remote_urls = []
    sha_by_url = {
        'https://github.com/org/gh_missing.git': gh_priv_sha,
        'https://gitlab.com/org/gl_repo.git': gl_sha,
        'https://bitbucket.org/org/bb_repo.git': bb_sha,
        'https://git.example.org/org/custom.git': custom_sha,
    }

    def fake_git_cmd(*args):
        url = args[1]
        ls_remote_urls.append(url)
        return {'returncode': 0, 'output': '%s\trefs/heads/main\n' % sha_by_url[url]}

    known_refs = {}
    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.side_effect = fake_git_cmd
        cache.update_distribution([dist_data], known_remote_refs=known_refs, max_workers=4)

    # gh_ok resolved via GraphQL; gh_missing was NOT poisoned with None and succeeded via git ls-remote
    assert sorted(ls_remote_urls) == sorted(sha_by_url.keys())
    assert known_refs[('https://github.com/org/gh_ok.git', 'main')] == gh_ok_sha
    assert known_refs[('https://github.com/org/gh_missing.git', 'main')] == gh_priv_sha
    assert len(cache.source_repo_resources) == 5
    assert cache.failed_source_repos == {}
    out = capsys.readouterr().out
    assert 'github_graphql=1' in out
    assert 'git_ls_remote=4' in out


def test_graphql_batch_auth_rate_limit_and_http_error_fallbacks(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    pairs = [('https://github.com/org/r%d.git' % i, 'main') for i in range(6)]

    # 1. HTTP 401 stops remaining batches immediately
    calls = [0]

    def fail_401(req, timeout=None):
        calls[0] += 1
        raise HTTPError(req.get_full_url(), 401, 'Unauthorized', {}, None)

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fail_401):
        assert github_mp.resolve_github_remote_refs(pairs, batch_size=2, max_workers=1) == {}
    assert calls[0] == 1

    # 2. HTTP 502 falls back cleanly (omits failed batch keys, returns {} when all fail)
    calls[0] = 0

    def fail_502(req, timeout=None):
        calls[0] += 1
        raise HTTPError(req.get_full_url(), 502, 'Bad Gateway', {}, None)

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fail_502):
        assert github_mp.resolve_github_remote_refs(pairs[:2], batch_size=2, max_workers=1) == {}
    assert calls[0] == 1

    # 3. GraphQL RATE_LIMITED error stops remaining batches
    calls[0] = 0

    def fail_gql_rl(req, timeout=None):
        calls[0] += 1
        return _mock_http_response(json.dumps({
            'data': None,
            'errors': [{'type': 'RATE_LIMITED', 'message': 'API rate limit exceeded'}],
        }))

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fail_gql_rl):
        assert github_mp.resolve_github_remote_refs(pairs, batch_size=2, max_workers=1) == {}
    assert calls[0] == 1

    # 4. Low rateLimit.remaining (< 50) keeps batch 1 results and stops batches 2 & 3
    calls[0] = 0
    ref_table = {('org', 'r%d' % i, 'refs/heads/main'): {'target': {'oid': _make_sha(i)}} for i in range(6)}

    def low_remaining(req, timeout=None):
        calls[0] += 1
        return _build_graphql_response_from_query(
            _parse_graphql_request(req),
            ref_table,
            rate_limit={'remaining': 12, 'resetAt': '2026-11-20T07:00:00Z', 'cost': 1},
        )

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=low_remaining):
        res = github_mp.resolve_github_remote_refs(pairs, batch_size=2, max_workers=1)
    assert calls[0] == 1
    assert set(res.keys()) == {pairs[0], pairs[1]}

    # 5. HostRateLimitError on http_graphql bucket stops GraphQL WITHOUT blocking REST http bucket
    limiter = RateLimiter(http_host_intervals={'api.github.com': 0.0})
    limiter.record_rate_limit(
        'http_graphql', 'api.github.com', retry_after=600.0, remaining_budget=30.0, status_code=403,
    )
    calls[0] = 0
    with use_rate_limiter(limiter), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=AssertionError('should not call HTTP')):
        assert github_mp.resolve_github_remote_refs(pairs, batch_size=2, max_workers=1) == {}
    # REST 'http' bucket on api.github.com remains completely unblocked
    with limiter.request_slot('http', 'api.github.com', remaining_budget=1.0) as waited:
        assert waited == 0.0


def test_graphql_disabled_without_token_or_env_flag(monkeypatch):
    repos = {
        'r0': {'source': {'type': 'git', 'url': 'https://github.com/org/r0.git', 'version': 'main'}},
    }
    sha = _make_sha(50)

    # No GITHUB_TOKEN
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.setattr(github_mp, 'GITHUB_TOKEN', None)
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)
    cache, dist_data = _make_expired_cache('rolling', repos, {'r0': sha})
    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=AssertionError('no GraphQL')), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {'returncode': 0, 'output': '%s\trefs/heads/main\n' % sha}
        cache.update_distribution([dist_data], max_workers=2)
    mock_git.return_value.command.assert_called_once()

    # ROSDISTRO_GITHUB_GRAPHQL=0 with GITHUB_TOKEN set
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.setenv('ROSDISTRO_GITHUB_GRAPHQL', '0')
    cache2, dist_data2 = _make_expired_cache('rolling', repos, {'r0': sha})
    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=AssertionError('no GraphQL')), \
            patch('rosdistro.distribution_cache.Git') as mock_git2:
        mock_git2.return_value.command.return_value = {'returncode': 0, 'output': '%s\trefs/heads/main\n' % sha}
        cache2.update_distribution([dist_data2], max_workers=2)
    mock_git2.return_value.command.assert_called_once()


def test_graphql_prescan_exclusions_and_cross_distro_reuse(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    sha_expired = _make_sha(60)
    sha_fresh = _make_sha(61)
    sha_pinned = _make_sha(62)
    sha_known = _make_sha(63)

    repos = {
        'expired_repo': {'source': {'type': 'git', 'url': 'https://github.com/org/expired.git', 'version': 'main'}},
        'fresh_repo': {'source': {'type': 'git', 'url': 'https://github.com/org/fresh.git', 'version': 'main'}},
        'pinned_repo': {'source': {'type': 'git', 'url': 'https://github.com/org/pinned.git', 'version': sha_pinned}},
        'known_repo': {'source': {'type': 'git', 'url': 'https://github.com/org/known.git', 'version': 'main'}},
    }
    cache_a, dist_data_a = _make_expired_cache('distro_a', repos, {
        'expired_repo': sha_expired,
        'fresh_repo': sha_fresh,
        'pinned_repo': sha_pinned,
        'known_repo': sha_known,
    })
    # Make fresh_repo within TTL
    cache_a.source_repo_resources['fresh_repo'].set_last_update_time(datetime.datetime.now())

    pool = _NeighborCachePool()
    pool.record_remote_ref('https://github.com/org/known.git', 'main', sha_known)

    ref_table = {('org', 'expired', 'refs/heads/main'): {'target': {'oid': sha_expired}}}
    queries = []

    def fake_urlopen(req, timeout=None):
        query = _parse_graphql_request(req)
        queries.append(query)
        return _build_graphql_response_from_query(query, ref_table)

    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        cache_a.update_distribution([dist_data_a], known_remote_refs=pool.known_remote_refs, max_workers=2)

    assert len(queries) == 1
    assert 'expired' in queries[0]
    assert 'fresh' not in queries[0]
    assert 'pinned' not in queries[0]
    assert 'known' not in queries[0]
    mock_git.return_value.command.assert_not_called()
    assert pool.known_remote_refs[('https://github.com/org/expired.git', 'main')] == sha_expired

    # Second distro sharing expired_repo reuses pool.known_remote_refs without GraphQL or git ls-remote
    repos_b = {
        'expired_repo': {'source': {'type': 'git', 'url': 'https://github.com/org/expired.git', 'version': 'main'}},
    }
    cache_b, dist_data_b = _make_expired_cache('distro_b', repos_b, {'expired_repo': sha_expired})
    capsys.readouterr()
    with patch('rosdistro.manifest_provider.github.urlopen', side_effect=AssertionError('should reuse known_remote_refs')), \
            patch('rosdistro.distribution_cache.Git') as mock_git_b:
        cache_b.update_distribution([dist_data_b], known_remote_refs=pool.known_remote_refs, max_workers=2)
    mock_git_b.return_value.command.assert_not_called()
    assert 'known_remote_ref=1' in capsys.readouterr().out


def test_graphql_otel_profiling_metadata_and_slowest_filter(monkeypatch):
    from rosdistro import cache_profiler as cache_profiler_mod
    if not (cache_profiler_mod.HAS_OTEL_API and cache_profiler_mod.HAS_OTEL_SDK):
        return

    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.setenv('ROSDISTRO_GITHUB_GRAPHQL_BATCH_SIZE', '2')
    monkeypatch.delenv('ROSDISTRO_GITHUB_GRAPHQL', raising=False)

    repos = {}
    cached_shas = {}
    ref_table = {}
    for i in range(4):
        name = 'gh_%d' % i
        sha = _make_sha(80 + i)
        repos[name] = {'source': {'type': 'git', 'url': 'https://github.com/org/%s.git' % name, 'version': 'main'}}
        cached_shas[name] = sha
        ref_table[('org', name, 'refs/heads/main')] = {'target': {'oid': sha}}
    gl_sha = _make_sha(90)
    repos['gl_0'] = {'source': {'type': 'git', 'url': 'https://gitlab.com/org/gl_0.git', 'version': 'main'}}
    cached_shas['gl_0'] = gl_sha

    cache, dist_data = _make_expired_cache('rolling', repos, cached_shas)

    def fake_urlopen(req, timeout=None):
        return _build_graphql_response_from_query(_parse_graphql_request(req), ref_table)

    profiler = cache_profiler_mod.CacheProfiler(enabled=True, record_spans=True, record_trace_events=True)
    with cache_profiler_mod.use_profiler(profiler), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_urlopen), \
            patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {'returncode': 0, 'output': '%s\trefs/heads/main\n' % gl_sha}
        with profiler.span('build_cache', kind='root'):
            with profiler.span('distro:rolling', kind='distro', dist_name='rolling'):
                with profiler.span('phase:update_cache', kind='phase', dist_name='rolling'):
                    cache.update_distribution([dist_data], max_workers=2)

    report = profiler.to_dict(include_trace_events=True)
    profile = report['rosdistroProfile']
    events = report['traceEvents']

    batch_events = [e for e in events if e['name'].startswith('cache:github_graphql_batch')]
    assert len(batch_events) == 1
    assert batch_events[0]['args'].get('fetch_type') == 'github_graphql'
    assert batch_events[0]['args'].get('detail') == 'requested=4, resolved=4'

    post_events = [e for e in events if e['name'] == 'http:post [via=github_graphql]']
    assert len(post_events) == 2
    assert sorted(e['args'].get('detail') for e in post_events) == [
        'batch=1/2, repos=2',
        'batch=2/2, repos=2',
    ]

    # Sub-5ms github_graphql validation hits must not flood slowest_events
    assert all(ev['fetch_type'] != 'github_graphql' for ev in profile['slowest_events'])
    profiler.shutdown()
