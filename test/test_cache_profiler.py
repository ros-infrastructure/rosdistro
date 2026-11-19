import datetime
import io
import json
import os
import re
import tarfile
import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest
import yaml

from rosdistro import (
    CacheConfig,
    CacheProfiler,
    DistributionCache,
    RosdistroSpanProcessor,
    format_fetch_event,
    get_provider_name,
    use_profiler,
)
import rosdistro.cache_profiler as cache_profiler_mod
from rosdistro.cache_profiler import (
    FetchStats,
    format_duration,
    sanitize_url_for_trace,
    trace_operation,
)

requires_otel = pytest.mark.skipif(
    not (cache_profiler_mod.HAS_OTEL_API and cache_profiler_mod.HAS_OTEL_SDK),
    reason='opentelemetry-api / opentelemetry-sdk not installed',
)
from rosdistro.cli.rosdistro_build_cache import main as build_cache_main
from rosdistro.distribution_cache_generator import (
    generate_distribution_cache,
    generate_distribution_caches,
)
from rosdistro.manifest_provider import SkipManifestProvider
from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import (
    CachedManifestProvider,
    CachedSourceManifestProvider,
)
from rosdistro.manifest_provider.git import (
    git_manifest_provider,
    git_source_manifest_provider,
)
from rosdistro.manifest_provider.github import (
    github_manifest_provider,
    github_source_manifest_provider,
)
from rosdistro.manifest_provider.gitlab import (
    gitlab_manifest_provider,
    gitlab_source_manifest_provider,
)
from rosdistro.manifest_provider.tar import (
    tar_manifest_provider,
    tar_source_manifest_provider,
)
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro.source_repository_specification import SourceRepositorySpecification


def _pkg_xml(name, version='1.0.0'):
    return (
        f'<?xml version="1.0"?>'
        f'<package format="2">'
        f'<name>{name}</name>'
        f'<version>{version}</version>'
        f'<description>{name} package</description>'
        f'<maintainer email="dev@example.com">Dev</maintainer>'
        f'<license>BSD</license>'
        f'</package>'
    )


def _make_mock_http_response(body, headers=None):
    headers = headers or {}
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.getheader.side_effect = lambda k, default=None: headers.get(k, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _build_tar_bytes(files_dict):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, content in files_dict.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.getvalue()


def _write_index_and_distros(tmp_path, distros_dict):
    index_data = {
        'type': 'index',
        'version': 4,
        'distributions': {},
    }
    for dist_name, repos in distros_dict.items():
        dist_dir = tmp_path / dist_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / 'distribution.yaml').write_text(yaml.safe_dump({
            'type': 'distribution',
            'version': 2,
            'repositories': repos,
        }))
        index_data['distributions'][dist_name] = {
            'distribution': [f'{dist_name}/distribution.yaml'],
            'distribution_status': 'active',
            'distribution_type': 'ros2',
            'python_version': 3,
        }
    index_path = tmp_path / 'index-v4.yaml'
    index_path.write_text(yaml.safe_dump(index_data))
    return str(index_path)


def test_helper_functions_and_stats():
    assert get_provider_name(github_manifest_provider) == 'github'
    assert get_provider_name(github_source_manifest_provider) == 'github'
    assert get_provider_name(gitlab_manifest_provider) == 'gitlab'
    assert get_provider_name(gitlab_source_manifest_provider) == 'gitlab'
    assert get_provider_name(bitbucket_manifest_provider) == 'bitbucket'
    assert get_provider_name(git_manifest_provider) == 'git'
    assert get_provider_name(git_source_manifest_provider) == 'git'
    assert get_provider_name(tar_manifest_provider) == 'tar'
    assert get_provider_name(tar_source_manifest_provider) == 'tar'
    assert get_provider_name('custom') == 'custom'
    assert get_provider_name(None) == 'unknown'

    def my_custom_manifest_provider():
        pass

    assert get_provider_name(my_custom_manifest_provider) == 'my_custom'

    assert sanitize_url_for_trace('https://user:secret@github.com/org/repo.git') == (
        'https://github.com/org/repo.git'
    )
    assert sanitize_url_for_trace('https://token@gitlab.com:8443/org/repo.git?a=1') == (
        'https://gitlab.com:8443/org/repo.git?a=1'
    )
    assert sanitize_url_for_trace('https://github.com/org/repo.git') == (
        'https://github.com/org/repo.git'
    )

    assert format_duration(0.1234) == '0.123s'
    assert format_duration(75.5) == '1m 15.50s'
    assert format_duration(3725.2) == '1h 02m 05.2s'

    msg = format_fetch_event(
        'release:package.xml', 'my_pkg', 'github',
        status='ok', duration_s=0.125, dist_name='rolling', detail='version=1.0.0-1',
    )
    assert msg == '  [rolling] [release:package.xml] my_pkg via=github (ok, 0.125s, version=1.0.0-1)'

    stats = FetchStats()
    for val in (0.1, 0.2, 0.3, 0.4, 1.0):
        stats.add(val, self_s=val / 2.0, status='ok')
    d = stats.to_dict(total_run_s=4.0)
    assert d['count'] == 5
    assert d['total_s'] == pytest.approx(2.0)
    assert d['self_s'] == pytest.approx(1.0)
    assert d['pct_of_total'] == pytest.approx(50.0)
    assert d['min_s'] == pytest.approx(0.1)
    assert d['max_s'] == pytest.approx(1.0)
    assert d['p50_s'] == pytest.approx(0.3)
    assert d['statuses'] == {'ok': 5}


@requires_otel
def test_profiler_deferred_labels_self_time_invariant_and_otel_export():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    buf = io.StringIO()
    exporter = InMemorySpanExporter()
    external_provider = TracerProvider(shutdown_on_exit=False)
    external_provider.add_span_processor(SimpleSpanProcessor(exporter))

    profiler = CacheProfiler(
        enabled=True,
        record_spans=True,
        record_trace_events=True,
        console_stream=buf,
        tracer_provider=external_provider,
    )
    assert isinstance(profiler.span_processor, RosdistroSpanProcessor)

    with use_profiler(profiler):
        with profiler.span('build_cache', kind='root'):
            with profiler.span('distro:rolling', kind='distro', dist_name='rolling'):
                with profiler.span('phase:release_fetch', kind='phase', dist_name='rolling'):
                    with profiler.span(
                        'release:package.xml',
                        kind='fetch',
                        resource_type='release:package.xml',
                        target='pkg_<special>&"1"',
                        dist_name='rolling',
                        deferred_label=True,
                    ) as fspan:
                        with profiler.span('provider:github', kind='provider', fetch_type='github'):
                            with profiler.span('http:get', kind='operation', fetch_type='github'):
                                time.sleep(0.01)
                        fspan.fetch_type = 'github'
                        fspan.status = 'ok'
                    with profiler.span(
                        'release:README.md',
                        kind='fetch',
                        resource_type='release:README.md',
                        target='broken_pkg',
                        dist_name='rolling',
                    ) as fail_span:
                        fail_span.fetch_type = 'failed'
                        fail_span.status = 'failed'

    profiler.shutdown()
    assert not profiler.has_active_span()

    # Deferred parent label must appear in the child's folded stack
    folded = profiler.get_folded_stacks()
    child_stack = (
        'build_cache;distro:rolling;phase:release_fetch;'
        'release:package.xml [via=github];provider:github [ok];http:get [via=github]'
    )
    assert child_stack in folded
    assert folded[child_stack] >= 5000  # at least 5ms

    # Sum of folded stack self-times must match total root duration (no double-counting!)
    total_folded_s = sum(folded.values()) / 1_000_000.0
    assert total_folded_s == pytest.approx(profiler.total_duration_s(), rel=0.05, abs=0.002)

    # Real OpenTelemetry spans must have been exported with rosdistro.* attributes & ERROR status on failure
    otel_spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in otel_spans}
    assert 'build_cache' in by_name
    assert by_name['release:package.xml'].attributes['rosdistro.fetch_type'] == 'github'
    assert by_name['release:package.xml'].attributes['rosdistro.distro'] == 'rolling'
    assert by_name['http:get'].parent.span_id == by_name['provider:github'].context.span_id
    assert by_name['release:README.md'].status.status_code == StatusCode.ERROR
    external_provider.shutdown()


def test_missing_opentelemetry_graceful_fallback_and_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache_profiler_mod, 'HAS_OTEL_API', False)
    monkeypatch.setattr(cache_profiler_mod, 'HAS_OTEL_SDK', False)

    # Normal non-profiling runs work cleanly without OpenTelemetry installed
    reporter_only = CacheProfiler(enabled=True, record_spans=False)
    with use_profiler(reporter_only):
        with reporter_only.span('build_cache', kind='root') as sp:
            with trace_operation('http:get', fetch_type='github', target='https://example.com'):
                time.sleep(0.002)
            assert sp.duration_s == 0.0
        assert sp.duration_s >= 0.001

    # Requesting profiling without OpenTelemetry raises a clear actionable error
    with pytest.raises(RuntimeError, match=r"rosdistro\[profile\]"):
        CacheProfiler(enabled=True, record_spans=True)

    # CLI --profile exits cleanly with rc=1 and install hint on stderr
    index_path = _write_index_and_distros(tmp_path, {'rolling': {}})
    rc = build_cache_main([index_path, 'rolling', '--profile'])
    assert rc == 1
    assert 'rosdistro[profile]' in capsys.readouterr().err


@requires_otel
def test_homogenized_console_output_and_profiling_across_fetch_types(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    sha = 'a' * 40

    repos_a = {
        'shared_repo': {
            'release': {
                'packages': ['shared_pkg'],
                'tags': {'release': 'release/shared/{package}/{version}'},
                'url': 'https://github.com/org/shared-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/shared.git',
                'version': 'main',
            },
        },
        'release_only_repo': {
            'release': {
                'packages': ['rel_only_pkg'],
                'tags': {'release': 'release/rel_only/{package}/{version}'},
                'url': 'https://gitlab.com/org/rel-only-release.git',
                'version': '2.0.0-1',
            },
        },
        'failing_source_repo': {
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/broken.git',
                'version': 'main',
            },
        },
    }
    repos_b = {
        'shared_repo': repos_a['shared_repo'],
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    def github_manifest_provider_fake(_dist_name, repo, pkg_name, filepath='package.xml'):
        if 'github.com' not in repo.url:
            raise SkipManifestProvider('not github')
        if filepath == 'package.xml':
            return _pkg_xml(pkg_name, '1.0.0')
        if filepath == 'README.md':
            return '# Shared README\n'
        return None  # CHANGELOG.rst confirmed missing

    github_manifest_provider_fake.__name__ = 'github_manifest_provider'

    def gitlab_manifest_provider_fake(_dist_name, repo, pkg_name, filepath='package.xml'):
        if 'gitlab.com' not in repo.url:
            raise SkipManifestProvider('not gitlab')
        if filepath == 'package.xml':
            return _pkg_xml(pkg_name, '2.0.0')
        return '# GitLab doc\n'

    gitlab_manifest_provider_fake.__name__ = 'gitlab_manifest_provider'

    def github_source_manifest_provider_fake(repo, filepaths=None, metadata=None):
        if 'broken' in repo.url:
            raise RuntimeError('404 Not Found')
        rc = SourceRepositoryCache.from_ref(sha)
        rc.set_stars(42)
        rc.set_description('Shared repo')
        rc.set_tags(['ros2'])
        rc.set_last_commit_time('2026-04-01T12:00:00Z')
        rc.set_contributing('# Contributing')
        rc.set_readme('# Root README')
        rc.add('shared_pkg', 'shared_pkg', _pkg_xml('shared_pkg', '1.0.0'), 'package.xml')
        rc.add('shared_pkg', 'shared_pkg', '# Shared README', 'README.md')
        rc.add('shared_pkg', 'shared_pkg', '1.0.0', 'CHANGELOG.rst')
        rc.add('shared_pkg', 'shared_pkg', ['launch/run.launch.py'], 'launch_files')
        rc.add('shared_pkg', 'shared_pkg', ['msg/A.msg'], 'msg_files')
        rc.add('shared_pkg', 'shared_pkg', [], 'srv_files')
        rc.add('shared_pkg', 'shared_pkg', [], 'action_files')
        return rc

    github_source_manifest_provider_fake.__name__ = 'github_source_manifest_provider'

    # 1. Cold build across distro_a and distro_b (non-debug mode)
    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True, debug=False)
    with patch(
        'rosdistro.Distribution.default_manifest_providers',
        [github_manifest_provider_fake, gitlab_manifest_provider_fake],
    ), patch(
        'rosdistro.Distribution.default_source_manifest_providers',
        [github_source_manifest_provider_fake],
    ):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'],
            preclean=False, config='extended', debug=False, profiler=profiler,
        )

    out = capsys.readouterr().out
    # Standardized console output for every datatype and fetch means
    assert '[distro_a] [release:package.xml] shared_pkg via=github (ok,' in out
    assert '[distro_a] [release:README.md] shared_pkg via=github (ok,' in out
    assert '[distro_a] [release:CHANGELOG.rst] shared_pkg via=github (not_found,' in out
    assert '[distro_a] [release:package.xml] rel_only_pkg via=gitlab (ok,' in out
    assert '[distro_a] [source:repo] shared_repo via=github (ok,' in out
    assert '[distro_a] [source:repo] failing_source_repo via=failed (failed,' in out
    assert '[distro_b] [release:package.xml] shared_pkg via=local_neighbor (ok,' in out
    assert 'neighbor=distro_a' in out
    assert '[distro_b] [release:README.md] shared_pkg via=local_neighbor (ok,' in out
    assert '[distro_b] [release:CHANGELOG.rst] shared_pkg via=local_neighbor (not_found,' in out
    assert '[distro_b] [source:repo] shared_repo via=local_neighbor (ok,' in out
    # Non-debug mode must NOT spam skipped entries or legacy ad-hoc strings
    assert 'via=skipped' not in out
    assert 'updated manifest of package' not in out
    assert 'dist cache fetch' not in out
    assert 'dist cache source fetch' not in out
    assert 'release fetch summary [distro_a]' in out
    assert 'source fetch summary [distro_b]' in out

    # Check profiler tracked every resource type and fetch type including local_neighbor
    profile = profiler.to_dict()['rosdistroProfile']
    assert 'release:package.xml' in profile['by_resource_type']
    assert 'release:README.md' in profile['by_resource_type']
    assert 'release:CHANGELOG.rst' in profile['by_resource_type']
    assert 'source:repo' in profile['by_resource_type']
    assert 'cache:load' in profile['by_resource_type']

    assert 'github' in profile['by_fetch_type']
    assert 'gitlab' in profile['by_fetch_type']
    assert 'local_neighbor' in profile['by_fetch_type']
    assert 'failed' in profile['by_fetch_type']
    assert 'skipped' in profile['by_fetch_type']

    # 2. Persist distro_a cache to disk, then rebuild distro_a in debug mode -> verify cache hits & cached_failure
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(
        yaml.safe_dump(caches['distro_a'].get_data(version=3))
    )
    warm_profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True, debug=True)
    with patch(
        'rosdistro.Distribution.default_manifest_providers',
        [github_manifest_provider_fake, gitlab_manifest_provider_fake],
    ), patch(
        'rosdistro.Distribution.default_source_manifest_providers',
        [github_source_manifest_provider_fake],
    ):
        generate_distribution_caches(
            index_path, dist_names=['distro_a'],
            preclean=False, config='extended', debug=True, profiler=warm_profiler,
        )

    warm_out = capsys.readouterr().out
    assert '[distro_a] [cache:load] distro_a via=local_file (ok,' in warm_out
    assert '[distro_a] [cache:validate_source_ref] shared_repo via=ttl_skip (kept,' in warm_out
    assert '[distro_a] [release:package.xml] shared_pkg via=cache (ok,' in warm_out
    assert '[distro_a] [release:README.md] shared_pkg via=cache (ok,' in warm_out
    assert '[distro_a] [release:CHANGELOG.rst] shared_pkg via=cache (not_found,' in warm_out
    assert '[distro_a] [source:repo] shared_repo via=cache (ok,' in warm_out
    assert '[distro_a] [source:repo] failing_source_repo via=cached_failure (skipped,' in warm_out
    assert '[distro_a] [source:repo] release_only_repo via=skipped (skipped,' in warm_out

    warm_profile = warm_profiler.to_dict()['rosdistroProfile']
    assert 'cache' in warm_profile['by_fetch_type']
    assert 'ttl_skip' in warm_profile['by_fetch_type']
    assert 'cached_failure' in warm_profile['by_fetch_type']
    assert 'local_file' in warm_profile['by_fetch_type']
    assert 'sanitize:xml' in warm_profile['by_operation']
    assert 'yaml:load' in warm_profile['by_operation']


@requires_otel
def test_real_providers_sub_resource_and_operation_tracing(tmp_path):
    """Verify github, gitlab, bitbucket, git, and tar providers record sub-resources and operations."""
    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True)

    # 1. GitHub source + release (including rate-limit retry sleep)
    gh_src_repo = SourceRepositorySpecification('gh_repo', {
        'type': 'git',
        'url': 'https://github.com/org/gh_repo.git',
        'version': 'main',
    })
    gh_rel_repo = ReleaseRepositorySpecification('gh_repo', {
        'url': 'https://github.com/org/gh_repo-release.git',
        'version': '1.0.0-1',
        'tags': {'release': 'release/rolling/{package}/{version}'},
    })
    gh_tree = {
        'sha': 'b' * 40,
        'truncated': False,
        'tree': [
            {'path': 'README.md', 'type': 'blob'},
            {'path': 'CONTRIBUTING.md', 'type': 'blob'},
            {'path': 'pkg_gh/package.xml', 'type': 'blob'},
            {'path': 'pkg_gh/README.md', 'type': 'blob'},
            {'path': 'pkg_gh/launch/a.launch.py', 'type': 'blob'},
            {'path': 'pkg_gh/msg/Foo.msg', 'type': 'blob'},
            {'path': 'pkg_gh/srv/Bar.srv', 'type': 'blob'},
            {'path': 'pkg_gh/action/Baz.action', 'type': 'blob'},
        ],
    }
    rate_limit_calls = {'n': 0}

    def fake_gh_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if 'raw.githubusercontent.com/org/gh_repo-release' in url and rate_limit_calls['n'] == 0:
            rate_limit_calls['n'] += 1
            raise HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '0.01'}, io.BytesIO(b'rate limit'))
        if '/commits/' in url:
            return _make_mock_http_response('a' * 40)
        if '/git/trees/' in url:
            return _make_mock_http_response(json.dumps(gh_tree))
        if url.endswith('/repos/org/gh_repo'):
            return _make_mock_http_response(json.dumps({
                'stargazers_count': 7, 'description': 'desc', 'topics': ['ros2'], 'pushed_at': '2026-01-01T00:00:00Z'
            }))
        if url.endswith('package.xml'):
            return _make_mock_http_response(_pkg_xml('pkg_gh', '1.0.0'))
        if url.endswith('README.md'):
            return _make_mock_http_response('# README')
        if url.endswith('CONTRIBUTING.md'):
            return _make_mock_http_response('# Contrib')
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    # 2. GitLab source + release
    gl_src_repo = SourceRepositorySpecification('gl_repo', {
        'type': 'git',
        'url': 'https://gitlab.com/org/gl_repo.git',
        'version': 'main',
    })
    gl_rel_repo = ReleaseRepositorySpecification('gl_repo', {
        'url': 'https://gitlab.com/org/gl_repo-release.git',
        'version': '1.0.0-1',
        'tags': {'release': 'release/rolling/{package}/{version}'},
    })

    def fake_gl_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if '/repository/commits?' in url:
            return _make_mock_http_response(json.dumps([{'id': 'c' * 40, 'committed_date': '2026-01-01T00:00:00Z'}]))
        if '/repository/tree?' in url:
            return _make_mock_http_response(json.dumps([
                {'path': 'pkg_gl/package.xml', 'type': 'blob'},
            ]))
        if url.endswith('/projects/org%2Fgl_repo/'):
            return _make_mock_http_response(json.dumps({'star_count': 3, 'description': 'gl', 'tag_list': []}))
        if 'package.xml/raw' in url or 'pkg_gl%2Fpackage.xml/raw' in url:
            return _make_mock_http_response(_pkg_xml('pkg_gl', '1.0.0'))
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    # 3. Bitbucket release
    bb_rel_repo = ReleaseRepositorySpecification('bb_repo', {
        'url': 'https://bitbucket.org/org/bb_repo-release.git',
        'version': '1.0.0-1',
        'tags': {'release': 'release/rolling/{package}/{version}'},
    })

    # 4. Tar source + release
    tar_bytes = _build_tar_bytes({
        'tar_pkg-1.0/package.xml': _pkg_xml('tar_pkg', '1.0.0'),
        'tar_pkg-1.0/README.md': '# Tar README',
    })
    tar_src_repo = SourceRepositorySpecification('tar_repo', {
        'type': 'tar',
        'url': 'https://example.com/tar_pkg.tar.gz',
    })
    tar_rel_repo = ReleaseRepositorySpecification('tar_repo', {
        'type': 'tar',
        'url': 'https://example.com/tar_pkg.tar.gz',
        'version': '1.0.0-1',
        'tags': {'release': 'tar_pkg-1.0'},
    })

    # 5. Git source (with mocked clone dir)
    git_dir = tmp_path / 'cloned_git_repo'
    (git_dir / 'pkg_git').mkdir(parents=True)
    (git_dir / 'README.md').write_text('# Git Root')
    (git_dir / 'pkg_git' / 'package.xml').write_text(_pkg_xml('pkg_git', '1.0.0'))
    git_src_repo = SourceRepositorySpecification('git_repo', {
        'type': 'git',
        'url': 'https://example.com/git_repo.git',
        'version': 'main',
    })

    from contextlib import contextmanager

    @contextmanager
    def fake_clone(_url, _ref):
        yield str(git_dir)

    class FakeGit(object):
        def __init__(self, cwd=None):
            self.cwd = cwd

        def command(self, *args):
            if args[0] == 'rev-parse':
                return {'returncode': 0, 'output': 'd' * 40}
            if args[0] == 'log':
                return {'returncode': 0, 'output': '2026-02-01T00:00:00Z'}
            return {'returncode': 0, 'output': ''}

    with use_profiler(profiler), \
            profiler.span('build_cache', kind='root'), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=fake_gh_urlopen), \
            patch('rosdistro.manifest_provider.github.time.sleep', return_value=None), \
            patch.object(gh_rel_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_gl_urlopen), \
            patch('rosdistro.manifest_provider.bitbucket.urlopen', return_value=_make_mock_http_response(_pkg_xml('pkg_bb', '1.0.0'))), \
            patch.object(bb_rel_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.tar.urlopen', side_effect=lambda *a, **kw: _make_mock_http_response(tar_bytes)), \
            patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone), \
            patch('rosdistro.manifest_provider.git.Git', FakeGit):
        assert '<name>pkg_gh</name>' in github_manifest_provider('rolling', gh_rel_repo, 'pkg_gh')
        assert github_source_manifest_provider(gh_src_repo) is not None
        assert '<name>pkg_gl</name>' in gitlab_manifest_provider('rolling', gl_rel_repo, 'pkg_gl')
        assert gitlab_source_manifest_provider(gl_src_repo) is not None
        assert '<name>pkg_bb</name>' in bitbucket_manifest_provider('rolling', bb_rel_repo, 'pkg_bb')
        assert '<name>tar_pkg</name>' in tar_manifest_provider('rolling', tar_rel_repo, 'tar_pkg')
        assert tar_source_manifest_provider(tar_src_repo) is not None
        assert git_source_manifest_provider(git_src_repo) is not None

    profile = profiler.to_dict()['rosdistroProfile']
    sub_pairs = {
        (row['resource_type'], row['fetch_type'])
        for row in profile['by_sub_resource_and_fetch_type']
    }
    for provider in ('github', 'gitlab', 'git', 'tar'):
        assert ('source:repo_tree', provider) in sub_pairs
        assert ('source:partition_tree', provider) in sub_pairs
        assert ('source:package.xml', provider) in sub_pairs
        assert ('source:launch_files', provider) in sub_pairs
        assert ('source:msg_files', provider) in sub_pairs
        assert ('source:srv_files', provider) in sub_pairs
        assert ('source:action_files', provider) in sub_pairs

    assert ('source:metadata:repo_info', 'github') in sub_pairs
    assert ('source:metadata:contributing', 'github') in sub_pairs
    assert ('source:metadata:readme', 'github') in sub_pairs
    assert ('source:metadata:repo_info', 'gitlab') in sub_pairs
    assert ('source:metadata:last_commit_time', 'git') in sub_pairs

    ops = profile['by_operation']
    assert 'http:get' in ops
    assert 'http:rate_limit_sleep' in ops
    assert 'tar:extract' in ops
    assert 'fs:list_repo_files' in ops


@requires_otel
def test_provider_fallback_traced_in_flamegraph():
    """Verify failed primary provider attempts appear alongside successful fallback provider."""
    dist_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'my_repo': {
                'source': {
                    'type': 'git',
                    'url': 'https://github.com/org/my_repo.git',
                    'version': 'main',
                },
            },
        },
    }
    cache = DistributionCache('rolling', distribution_file_data=dist_data, config=CacheConfig.extended())
    repo = cache.distribution_file.repositories['my_repo'].source_repository

    def github_source_manifest_provider_fail(_repo, filepaths=None, metadata=None):
        time.sleep(0.005)
        raise RuntimeError('transient timeout')

    github_source_manifest_provider_fail.__name__ = 'github_source_manifest_provider'

    def git_source_manifest_provider_ok(_repo, filepaths=None, metadata=None):
        time.sleep(0.005)
        rc = SourceRepositoryCache.from_ref('e' * 40)
        rc.add('my_pkg', '', _pkg_xml('my_pkg', '1.0.0'), 'package.xml')
        return rc

    git_source_manifest_provider_ok.__name__ = 'git_source_manifest_provider'

    provider = CachedSourceManifestProvider(
        cache, [github_source_manifest_provider_fail, git_source_manifest_provider_ok]
    )
    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True)
    with use_profiler(profiler), profiler.span('build_cache', kind='root'):
        with profiler.span(
            'source:repo', kind='fetch', resource_type='source:repo',
            target='my_repo', dist_name='rolling', deferred_label=True,
        ) as sp:
            result = provider(repo)
            assert result is not None
            sp.fetch_type = provider.last_fetch_info['fetch_type']
            sp.status = provider.last_fetch_info['status']

    assert provider.last_fetch_info['fetch_type'] == 'git'
    folded = profiler.get_folded_stacks()
    assert any(
        'source:repo [via=git];provider:github [failed]' in s
        for s in folded
    )
    assert any(
        'source:repo [via=git];provider:git [ok]' in s
        for s in folded
    )


@requires_otel
def test_on_disk_neighbor_load_and_ls_remote_validation_traced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = 'f' * 40
    repos = {
        'shared_repo': {
            'release': {
                'packages': ['shared_pkg'],
                'tags': {'release': 'release/shared/{package}/{version}'},
                'url': 'https://github.com/org/shared-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/shared.git',
                'version': 'main',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # Write an expired neighbor cache for distro_a on disk
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': '# Neighbor README',
        'CHANGELOG.rst': None,
    }
    rc = SourceRepositoryCache.from_ref(sha)
    rc.set_stars(10)
    rc.set_description('d')
    rc.set_tags(['ros2'])
    rc.set_last_commit_time('2026-01-01T00:00:00Z')
    rc.set_contributing('# C')
    rc.set_readme('# R')
    rc.add('shared_pkg', '', _pkg_xml('shared_pkg', '1.0.0'), 'package.xml')
    rc.add('shared_pkg', '', '# Neighbor README', 'README.md')
    rc.add('shared_pkg', '', '', 'CHANGELOG.rst')
    rc.add('shared_pkg', '', [], 'launch_files')
    rc.add('shared_pkg', '', [], 'msg_files')
    rc.add('shared_pkg', '', [], 'srv_files')
    rc.add('shared_pkg', '', [], 'action_files')
    rc.set_last_update_time(datetime.datetime.now() - datetime.timedelta(hours=3))
    neighbor.source_repo_resources['shared_repo'] = rc
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(
        yaml.safe_dump(neighbor.get_data(version=3))
    )

    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True)
    with patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {
            'returncode': 0,
            'output': f'{sha}\trefs/heads/main',
        }
        generate_distribution_caches(
            index_path, dist_names=['distro_b'],
            preclean=False, config='extended', profiler=profiler,
        )

    profile = profiler.to_dict()['rosdistroProfile']
    assert 'cache:neighbor_load' in profile['by_resource_type']
    assert 'neighbor:lookup' in profile['by_operation']
    assert 'local_neighbor' in profile['by_fetch_type']


@requires_otel
def test_cli_profiling_flags_and_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repos = {
        'demo_repo': {
            'release': {
                'packages': ['demo_pkg'],
                'tags': {'release': 'release/demo/{package}/{version}'},
                'url': 'https://github.com/org/demo-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/demo.git',
                'version': 'main',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})

    def fake_rel(_dist_name, _repo, pkg_name, filepath='package.xml'):
        return _pkg_xml(pkg_name, '1.0.0') if filepath == 'package.xml' else '# doc'

    fake_rel.__name__ = 'github_manifest_provider'

    def fake_src(_repo, filepaths=None, metadata=None):
        rc = SourceRepositoryCache.from_ref('1' * 40)
        rc.add('demo_pkg', '', _pkg_xml('demo_pkg', '1.0.0'), 'package.xml')
        return rc

    fake_src.__name__ = 'github_source_manifest_provider'

    prof_dir = tmp_path / 'prof_out'
    custom_folded = tmp_path / 'custom.folded'
    custom_json = tmp_path / 'custom.json'

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        rc = build_cache_main([
            index_path,
            'rolling',
            '--extended',
            '--profile-dir', str(prof_dir),
            '--profile-folded', str(custom_folded),
            '--profile-json', str(custom_json),
        ])

    assert not rc
    stdout = capsys.readouterr().out
    assert 'ROSDISTRO CACHE PROFILING SUMMARY' in stdout
    assert 'By Fetch Type' in stdout
    assert 'By Resource Type' in stdout
    assert 'By Resource Type x Fetch Type' in stdout
    assert 'speedscope.app' in stdout

    # Verify all artifact files exist and are valid
    for p in (
        prof_dir / 'profile-stacks.folded',
        prof_dir / 'profile-trace.json',
        prof_dir / 'profile-summary.txt',
        custom_folded,
        custom_json,
    ):
        assert p.exists() and p.stat().st_size > 0

    # Validate folded stacks format and phases
    folded_text = custom_folded.read_text(encoding='utf-8')
    assert 'phase:write_cache' in folded_text
    assert 'phase:release_fetch' in folded_text
    assert 'phase:source_fetch' in folded_text
    folded_lines = [line for line in folded_text.splitlines() if line]
    assert len(folded_lines) > 0
    for line in folded_lines:
        assert re.match(r'^.+ \d+$', line), f'Invalid folded stack line: {line}'

    # Validate Chrome trace + profile summary JSON
    trace_payload = json.loads(custom_json.read_text(encoding='utf-8'))
    assert 'traceEvents' in trace_payload
    assert 'rosdistroProfile' in trace_payload
    assert len(trace_payload['traceEvents']) > 0
    for ev in trace_payload['traceEvents']:
        assert ev['ph'] == 'X'
        assert isinstance(ev['ts'], int)
        assert isinstance(ev['dur'], int)
        assert 'name' in ev and 'cat' in ev and 'pid' in ev and 'tid' in ev
    assert 'cache:write' in trace_payload['rosdistroProfile']['by_resource_type']
    assert 'yaml:dump' in trace_payload['rosdistroProfile']['by_operation']
    assert 'gzip:write' in trace_payload['rosdistroProfile']['by_operation']


@requires_otel
def test_cli_writes_profiling_even_on_build_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repos = {
        'broken_repo': {
            'release': {
                'packages': ['broken_pkg'],
                'tags': {'release': 'release/broken/{package}/{version}'},
                'url': 'https://github.com/org/broken-release.git',
                'version': '1.0.0-1',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})
    folded_path = tmp_path / 'err_stacks.folded'

    def failing_rel(*_args, **_kwargs):
        raise RuntimeError('boom')

    failing_rel.__name__ = 'github_manifest_provider'

    with patch('rosdistro.Distribution.default_manifest_providers', [failing_rel]):
        rc = build_cache_main([
            index_path, 'rolling', '--strict', '--profile-folded', str(folded_path),
        ])

    assert rc == 1
    assert folded_path.exists()
    assert 'phase:release_fetch' in folded_path.read_text(encoding='utf-8')
    out = capsys.readouterr().out
    assert 'ROSDISTRO CACHE PROFILING SUMMARY' in out


@requires_otel
def test_custom_resource_generic_reporting_and_env_profile_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repos = {
        'custom_repo': {
            'release': {
                'packages': ['custom_pkg'],
                'tags': {'release': 'release/custom/{package}/{version}'},
                'url': 'https://github.com/org/custom-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/custom.git',
                'version': 'main',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})
    cfg_path = tmp_path / 'custom_cfg.yaml'
    cfg_path.write_text(yaml.safe_dump({
        'release_package_resources': {
            'package.xml': True,
            'LICENSE': True,
        },
        'source_package_resources': {
            'package.xml': True,
            'LICENSE': True,
        },
    }))

    def fake_rel(_dist_name, _repo, pkg_name, filepath='package.xml'):
        if filepath == 'package.xml':
            return _pkg_xml(pkg_name, '1.0.0')
        if filepath == 'LICENSE':
            return 'BSD-3-Clause'
        return None

    fake_rel.__name__ = 'github_manifest_provider'

    def fake_src(_repo, filepaths=None, metadata=None):
        rc = SourceRepositoryCache.from_ref('2' * 40)
        rc.add('custom_pkg', '', _pkg_xml('custom_pkg', '1.0.0'), 'package.xml')
        if filepaths and 'LICENSE' in filepaths:
            rc.add('custom_pkg', '', 'BSD-3-Clause', 'LICENSE')
        return rc

    fake_src.__name__ = 'github_source_manifest_provider'

    env_prof_dir = tmp_path / 'env_prof'
    with patch.dict(os.environ, {'ROSDISTRO_PROFILE_DIR': str(env_prof_dir)}, clear=False), \
            patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        rc = build_cache_main([index_path, 'rolling', '--config', str(cfg_path)])

    assert not rc
    out = capsys.readouterr().out
    assert '[rolling] [release:package.xml] custom_pkg via=github (ok,' in out
    assert '[rolling] [release:LICENSE] custom_pkg via=github (ok,' in out
    assert '[rolling] [source:repo] custom_repo via=github (ok,' in out

    assert (env_prof_dir / 'profile-stacks.folded').exists()
    trace_data = json.loads((env_prof_dir / 'profile-trace.json').read_text(encoding='utf-8'))
    assert 'release:LICENSE' in trace_data['rosdistroProfile']['by_resource_type']


@requires_otel
def test_real_local_git_commands_and_update_validation_traced(tmp_path, monkeypatch, capsys):
    """Exercise real local git subprocess calls (clone, rev-parse, log, ls-remote) without network."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    repo_dir = tmp_path / 'local_git_repo'
    repo_dir.mkdir()
    git_env = os.environ.copy()
    git_env.update({
        'GIT_AUTHOR_NAME': 'Test',
        'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'Test',
        'GIT_COMMITTER_EMAIL': 'test@example.com',
    })
    subprocess.run(['git', 'init', '-b', 'main'], cwd=str(repo_dir), check=True, env=git_env)
    (repo_dir / 'package.xml').write_text(_pkg_xml('local_pkg', '1.0.0'))
    (repo_dir / 'README.md').write_text('# Local Repo\n')
    subprocess.run(['git', 'add', '.'], cwd=str(repo_dir), check=True, env=git_env)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=str(repo_dir), check=True, env=git_env)
    head_sha = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=str(repo_dir), text=True
    ).strip()

    repo_url = 'file://' + str(repo_dir)
    dist_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'ttl_repo': {'source': {'type': 'git', 'url': 'https://example.com/fresh.git', 'version': 'main'}},
            'pinned_repo': {'source': {'type': 'git', 'url': repo_url, 'version': head_sha}},
            'known_repo': {'source': {'type': 'git', 'url': 'https://example.com/known.git', 'version': 'main'}},
            'ls_remote_repo': {'source': {'type': 'git', 'url': repo_url, 'version': 'main'}},
        },
    }
    cache = DistributionCache('rolling', distribution_file_data=dist_data, config=CacheConfig.extended())

    now = datetime.datetime.now()
    old = now - datetime.timedelta(hours=3)
    for name, ref, ts in (
        ('ttl_repo', head_sha, now),
        ('pinned_repo', head_sha, old),
        ('known_repo', '9' * 40, old),
        ('ls_remote_repo', head_sha, old),
        ('obsolete_repo', head_sha, old),
    ):
        rc = SourceRepositoryCache.from_ref(ref)
        rc.add('local_pkg', '', _pkg_xml('local_pkg', '1.0.0'), 'package.xml')
        rc.set_last_update_time(ts)
        cache.source_repo_resources[name] = rc

    src_spec = SourceRepositorySpecification('local_repo', {
        'type': 'git', 'url': repo_url, 'version': 'main',
    })

    profiler = CacheProfiler(enabled=True, record_spans=True, record_trace_events=True, debug=False)
    with use_profiler(profiler), profiler.span('build_cache', kind='root'):
        with profiler.span('distro:rolling', kind='distro', dist_name='rolling'):
            cache.update_distribution(
                [dist_data],
                known_remote_refs={('https://example.com/known.git', 'main'): '9' * 40},
            )
            with profiler.span(
                'source:repo', kind='fetch', resource_type='source:repo',
                fetch_type='git', target='local_repo', dist_name='rolling',
            ):
                fetched = git_source_manifest_provider(src_spec)
                assert fetched is not None
                assert fetched.ref() == head_sha

    out = capsys.readouterr().out
    # Real git ls-remote validation is printed even in non-debug mode; ttl_skip is summarized cleanly
    assert '[rolling] [cache:validate_source_ref] ls_remote_repo via=git_ls_remote (kept,' in out
    assert '[rolling] [cache:validate_source_ref] obsolete_repo via=obsolete (dropped,' in out
    assert 'via=ttl_skip' not in out

    profile = profiler.to_dict()['rosdistroProfile']
    pairs = {
        (r['resource_type'], r['fetch_type'])
        for r in profile['by_resource_and_fetch_type']
    }
    assert ('cache:validate_source_ref', 'ttl_skip') in pairs
    assert ('cache:validate_source_ref', 'pinned_hash') in pairs
    assert ('cache:validate_source_ref', 'known_remote_ref') in pairs
    assert ('cache:validate_source_ref', 'git_ls_remote') in pairs
    assert ('cache:validate_source_ref', 'obsolete') in pairs

    ops = profile['by_operation']
    assert 'git:ls-remote' in ops
    assert 'git:clone' in ops
    assert 'git:rev-parse' in ops
    assert 'git:log' in ops

