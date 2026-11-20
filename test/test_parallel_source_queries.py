from contextlib import contextmanager
import datetime
import json
import logging
import os
import threading
import time
from unittest.mock import patch

from jsonschema import validate
import pytest
import yaml

from rosdistro import (
    CacheConfig,
    CacheProfiler,
    DistributionCache,
    logger,
)
import rosdistro.cache_profiler as cache_profiler_mod
from rosdistro.cli.rosdistro_build_cache import main as build_cache_main
from rosdistro.cli.rosdistro_build_cache import parse_args as build_cache_parse_args
from rosdistro.distribution_cache_generator import (
    DEFAULT_SOURCE_WORKERS,
    generate_distribution_caches,
    get_default_source_workers,
)
from rosdistro.manifest_provider import SkipManifestProvider
from rosdistro.source_repository_cache import SourceRepositoryCache

requires_otel = pytest.mark.skipif(
    not (cache_profiler_mod.HAS_OTEL_API and cache_profiler_mod.HAS_OTEL_SDK),
    reason='opentelemetry-api / opentelemetry-sdk not installed',
)

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'rosdistro', 'rosdistro_cache_3.schema.json',
)


def _load_schema():
    with open(SCHEMA_PATH, 'r') as f:
        return json.load(f)


def _pkg_xml(name, version='1.0.0'):
    return (
        '<?xml version="1.0"?>'
        '<package format="2">'
        '<name>%s</name><version>%s</version>'
        '<description>%s package</description>'
        '<maintainer email="dev@example.com">Dev</maintainer>'
        '<license>BSD</license>'
        '</package>'
    ) % (name, version, name)


def _make_source_cache(pkg_name, sha='a' * 40):
    rc = SourceRepositoryCache.from_ref(sha)
    rc.set_stars(10)
    rc.set_description('%s repo' % pkg_name)
    rc.set_tags(['ros2'])
    rc.set_last_commit_time('2026-04-01T12:00:00Z')
    rc.set_contributing('# Contributing')
    rc.set_readme('# Root README')
    rc.add(pkg_name, pkg_name, _pkg_xml(pkg_name), 'package.xml')
    rc.add(pkg_name, pkg_name, '# %s' % pkg_name, 'README.md')
    rc.add(pkg_name, pkg_name, '1.0.0', 'CHANGELOG.rst')
    for asset_key in ('launch_files', 'msg_files', 'srv_files', 'action_files'):
        rc.add(pkg_name, pkg_name, [], asset_key)
    return rc


def _write_index_and_distros(tmp_path, distros_dict):
    index_data = {'type': 'index', 'version': 4, 'distributions': {}}
    for dist_name, repos in distros_dict.items():
        dist_dir = tmp_path / dist_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / 'distribution.yaml').write_text(yaml.safe_dump({
            'type': 'distribution', 'version': 2, 'repositories': repos,
        }))
        index_data['distributions'][dist_name] = {
            'distribution': ['%s/distribution.yaml' % dist_name],
            'distribution_status': 'active',
            'distribution_type': 'ros2',
            'python_version': 3,
        }
    index_path = tmp_path / 'index-v4.yaml'
    index_path.write_text(yaml.safe_dump(index_data))
    return str(index_path)


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


def test_parallel_source_fetch_concurrency_speedup_and_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {
        'repo_%02d' % i: {
            'source': {'type': 'git', 'url': 'https://github.com/org/repo_%02d.git' % i, 'version': 'main'}
        }
        for i in range(8)
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})

    state = {'cur': 0, 'peak': 0}
    lock = threading.Lock()

    def slow_source_mp(repo, filepaths=None, metadata=None):
        with lock:
            state['cur'] += 1
            state['peak'] = max(state['peak'], state['cur'])
        time.sleep(0.08)
        with lock:
            state['cur'] -= 1
        return _make_source_cache(repo.name)

    slow_source_mp.__name__ = 'git_source_manifest_provider'

    t0 = time.monotonic()
    with patch('rosdistro.Distribution.default_source_manifest_providers', [slow_source_mp]):
        caches = generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, config='extended', max_workers=4,
        )
    elapsed_parallel = time.monotonic() - t0
    assert len(caches['rolling'].source_repo_resources) == 8
    assert state['peak'] >= 3
    assert elapsed_parallel < 0.50

    state['cur'] = state['peak'] = 0
    with patch('rosdistro.Distribution.default_source_manifest_providers', [slow_source_mp]):
        generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, config='extended', max_workers=1,
        )
    assert state['peak'] == 1

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('ROSDISTRO_SOURCE_WORKERS', None)
        assert get_default_source_workers() == DEFAULT_SOURCE_WORKERS
    with patch.dict(os.environ, {'ROSDISTRO_SOURCE_WORKERS': '6'}, clear=False):
        assert get_default_source_workers() == 6
    with patch.dict(os.environ, {'ROSDISTRO_SOURCE_WORKERS': '0'}, clear=False):
        with pytest.raises(ValueError):
            get_default_source_workers()

    assert build_cache_parse_args(['index.yaml', 'rolling', '-j', '6']).jobs == 6
    with pytest.raises(SystemExit):
        build_cache_parse_args(['index.yaml', 'rolling', '--jobs', '0'])

    with patch('rosdistro.cli.rosdistro_build_cache.generate_distribution_caches') as mock_gen:
        rc = build_cache_main([index_path, 'rolling', '--extended', '--jobs', '3'])
    assert not rc
    assert mock_gen.call_args.kwargs['max_workers'] == 3


def test_parallel_source_fetch_thread_safe_last_fetch_info_and_console(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    repos = {}
    for i in range(4):
        repos['gh_%02d' % i] = {
            'source': {'type': 'git', 'url': 'https://github.com/org/gh_%02d.git' % i, 'version': 'main'}
        }
        repos['gl_%02d' % i] = {
            'source': {'type': 'git', 'url': 'https://gitlab.com/org/gl_%02d.git' % i, 'version': 'main'}
        }
        repos['git_%02d' % i] = {
            'source': {'type': 'git', 'url': 'https://codeberg.org/org/git_%02d.git' % i, 'version': 'main'}
        }
    repos['broken_repo'] = {
        'source': {'type': 'git', 'url': 'https://github.com/org/broken.git', 'version': 'main'}
    }
    repos['release_only'] = {}
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})

    def fake_gh(repo, filepaths=None, metadata=None):
        if 'github.com' not in repo.url:
            raise SkipManifestProvider('not github')
        if 'broken' in repo.url:
            raise RuntimeError('boom')
        time.sleep(0.02)
        return _make_source_cache(repo.name, sha='1' * 40)

    fake_gh.__name__ = 'github_source_manifest_provider'

    def fake_gl(repo, filepaths=None, metadata=None):
        if 'gitlab.com' not in repo.url:
            raise SkipManifestProvider('not gitlab')
        time.sleep(0.03)
        return _make_source_cache(repo.name, sha='2' * 40)

    fake_gl.__name__ = 'gitlab_source_manifest_provider'

    def fake_git(repo, filepaths=None, metadata=None):
        if 'broken' in repo.url:
            raise RuntimeError('git clone failed')
        time.sleep(0.01)
        return _make_source_cache(repo.name, sha='3' * 40)

    fake_git.__name__ = 'git_source_manifest_provider'

    with patch('rosdistro.Distribution.default_source_manifest_providers', [fake_gh, fake_gl, fake_git]), \
            _capture_logs(level=logging.ERROR):
        caches = generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, config='extended', max_workers=4,
        )

    out = capsys.readouterr().out
    assert ', 4 workers)' in out
    for i in range(4):
        assert '[rolling] [source:repo] gh_%02d via=github (ok,' % i in out
        assert '[rolling] [source:repo] gl_%02d via=gitlab (ok,' % i in out
        assert '[rolling] [source:repo] git_%02d via=git (ok,' % i in out
    assert '[rolling] [source:repo] broken_repo via=failed (failed,' in out

    cache = caches['rolling']
    assert len(cache.source_repo_resources) == 12
    assert 'broken_repo' in cache.failed_source_repos
    validate(instance=cache.get_data(version=3), schema=schema)


def test_parallel_neighbor_pool_loads_once_and_deduplicates_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {
        'shared_%02d' % i: {
            'source': {'type': 'git', 'url': 'https://github.com/org/shared_%02d.git' % i, 'version': 'main'}
        }
        for i in range(6)
    }
    repos['dup_broken_a'] = {
        'source': {'type': 'git', 'url': 'https://github.com/org/same_broken.git', 'version': 'main'}
    }
    repos['dup_broken_b'] = {
        'source': {'type': 'git', 'url': 'https://github.com/org/same_broken.git', 'version': 'main'}
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    now = datetime.datetime.now()
    for i in range(6):
        rc = _make_source_cache('shared_%02d' % i)
        rc.set_last_update_time(now)
        neighbor.source_repo_resources['shared_%02d' % i] = rc
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(
        yaml.safe_dump(neighbor.get_data(version=3))
    )

    from rosdistro import distribution_cache_generator as dcg
    real_load = dcg._load_local_cache_file
    load_calls = {'n': 0}
    broken_calls = {'n': 0}
    lock = threading.Lock()

    def tracking_load(dist_name, path=None):
        with lock:
            load_calls['n'] += 1
        time.sleep(0.03)
        return real_load(dist_name, path=path)

    def failing_source(repo, filepaths=None, metadata=None):
        with lock:
            broken_calls['n'] += 1
        time.sleep(0.05)
        raise RuntimeError('404 Not Found')

    failing_source.__name__ = 'github_source_manifest_provider'

    with patch('rosdistro.distribution_cache_generator._load_local_cache_file', side_effect=tracking_load), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [failing_source]), \
            _capture_logs(level=logging.ERROR):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended', max_workers=4,
        )

    assert load_calls['n'] == 1
    for i in range(6):
        assert 'shared_%02d' % i in caches['distro_b'].source_repo_resources
    assert broken_calls['n'] == 1


def test_parallel_update_distribution_validates_source_refs_concurrently_and_deduplicates():
    repos = {
        'repo_%02d' % i: {
            'source': {'type': 'git', 'url': 'https://example.com/repo_%02d.git' % i, 'version': 'main'}
        }
        for i in range(6)
    }
    repos['repo_00_alias'] = {
        'source': {'type': 'git', 'url': 'https://example.com/repo_00.git', 'version': 'main'}
    }
    dist_data = {'type': 'distribution', 'version': 2, 'repositories': repos}
    cache = DistributionCache('rolling', distribution_file_data=dist_data, config=CacheConfig.extended())
    old_ts = datetime.datetime.now() - datetime.timedelta(hours=3)
    for name in repos:
        rc = _make_source_cache(name, sha='a' * 40)
        rc.set_last_update_time(old_ts)
        cache.source_repo_resources[name] = rc

    state = {'cur': 0, 'peak': 0, 'urls': []}
    lock = threading.Lock()

    def fake_git_cmd(*args, **kwargs):
        url = args[1]
        with lock:
            state['cur'] += 1
            state['peak'] = max(state['peak'], state['cur'])
            state['urls'].append(url)
        time.sleep(0.06)
        with lock:
            state['cur'] -= 1
        sha = 'f' * 40 if 'repo_05' in url else 'a' * 40
        return {'returncode': 0, 'output': '%s\trefs/heads/main' % sha}

    with patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.side_effect = fake_git_cmd
        cache.update_distribution([dist_data], max_workers=4)

    assert state['peak'] >= 3
    assert len(state['urls']) == 6
    assert 'repo_05' not in cache.source_repo_resources
    assert 'repo_00' in cache.source_repo_resources
    assert 'repo_00_alias' in cache.source_repo_resources


@requires_otel
def test_parallel_source_fetch_otel_tracing_and_profiling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {
        'repo_%02d' % i: {
            'source': {'type': 'git', 'url': 'https://github.com/org/repo_%02d.git' % i, 'version': 'main'}
        }
        for i in range(6)
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})

    def slow_source(repo, filepaths=None, metadata=None):
        with cache_profiler_mod.trace_operation('fake:io', fetch_type='git', target=repo.name):
            time.sleep(0.06)
        return _make_source_cache(repo.name)

    slow_source.__name__ = 'git_source_manifest_provider'

    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True)
    with patch('rosdistro.Distribution.default_source_manifest_providers', [slow_source]):
        generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, config='extended',
            max_workers=4, profiler=profiler,
        )

    report = profiler.to_dict(include_trace_events=True)
    profile = report['rosdistroProfile']
    assert any(
        s.startswith('build_cache;distro:rolling;phase:source_fetch;source:repo [via=git];provider:git [ok];fake:io')
        for s in profile['folded_stacks']
    )
    source_tids = {
        ev['tid'] for ev in report['traceEvents']
        if ev.get('args', {}).get('resource_type') == 'source:repo'
    }
    assert len(source_tids) >= 2
    assert profile['wall_duration_s'] > 0
    assert profile['total_duration_s'] > profile['wall_duration_s'] * 1.3
    assert profile['by_distro']['rolling']['pct_of_total'] > 90.0
    assert profile['by_phase']['phase:source_fetch']['pct_of_total'] > 80.0
    assert '(wall:' in profiler.format_summary()
    profiler.shutdown()


def test_parallel_source_fetch_works_without_opentelemetry(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cache_profiler_mod, 'HAS_OTEL_API', False)
    monkeypatch.setattr(cache_profiler_mod, 'HAS_OTEL_SDK', False)
    repos = {
        'repo_%02d' % i: {
            'source': {'type': 'git', 'url': 'https://github.com/org/repo_%02d.git' % i, 'version': 'main'}
        }
        for i in range(4)
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})

    def fake_src(repo, filepaths=None, metadata=None):
        time.sleep(0.02)
        return _make_source_cache(repo.name)

    fake_src.__name__ = 'git_source_manifest_provider'

    with patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, config='extended', max_workers=4,
        )

    assert len(caches['rolling'].source_repo_resources) == 4
    out = capsys.readouterr().out
    for i in range(4):
        assert '[rolling] [source:repo] repo_%02d via=git (ok,' % i in out
