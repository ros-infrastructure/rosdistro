# Software License Agreement (BSD License)
#
# Copyright (c) 2026, Open Source Robotics Foundation, Inc.
# All rights reserved.

import datetime
import gzip
import json
import os
import pkgutil
import re
import shutil
import tempfile
import time
from unittest.mock import patch

from jsonschema import validate
import pytest
import yaml

from rosdistro import DEFAULT_INDEX_URL
from rosdistro.cli.rosdistro_build_cache import main as build_cache_main, parse_args as build_cache_parse_args
from rosdistro.distribution_cache import DistributionCache
from rosdistro.distribution_cache_generator import (
    CacheYamlDumper,
    _get_cached_distribution,
    generate_distribution_caches,
)
from rosdistro.manifest_provider.cache import (
    CachedSourceManifestProvider,
    sanitize_and_truncate_docs,
    sanitize_xml,
)
from rosdistro.source_repository_cache import SourceRepositoryCache


class BenchmarkResults:
    """Collects timing and size metrics across benchmark tests and prints a summary."""

    def __init__(self):
        self.entries = []

    def record(self, category, name, duration_s, details=''):
        self.entries.append({
            'category': category,
            'name': name,
            'duration_ms': duration_s * 1000.0,
            'details': details,
        })

    def format_summary(self):
        if not self.entries:
            return ''
        lines = [
            '',
            '=' * 100,
            f"{'Category':<22} | {'Benchmark Case':<36} | {'Time (ms)':>10} | {'Details'}",
            '-' * 100,
        ]
        for entry in self.entries:
            lines.append(
                f"{entry['category']:<22} | {entry['name']:<36} | "
                f"{entry['duration_ms']:>10.2f} | {entry['details']}"
            )
        lines.append('=' * 100)
        lines.append('')
        return '\n'.join(lines)


@pytest.fixture(scope='module')
def benchmark_results():
    results = BenchmarkResults()
    yield results
    print(results.format_summary())


def _load_cache_schema():
    schema_data = pkgutil.get_data('rosdistro', 'rosdistro_cache_3.schema.json')
    return json.loads(schema_data.decode('utf-8'))


def _make_package_xml(pkg_name, version='1.0.0', extra_comment=False):
    comment = '<!-- extra comment with whitespace   -->\n  ' if extra_comment else ''
    return (
        '<?xml version="1.0"?>\n'
        '<?xml-model href="http://download.ros.org/schema/package_format3.xsd" '
        'schematypens="http://www.w3.org/2001/XMLSchema"?>\n'
        '<package format="3">\n'
        f'  {comment}<name>{pkg_name}</name>\n'
        f'  <version>{version}</version>\n'
        f'  <description>Benchmark test package {pkg_name} with enough description text '
        'to exceed the 300 character YAML alias threshold in CacheYamlDumper.</description>\n'
        '  <maintainer email="benchmark@openrobotics.org">Benchmark Maintainer</maintainer>\n'
        '  <license>Apache License 2.0</license>\n'
        '  <buildtool_depend>ament_cmake</buildtool_depend>\n'
        '  <depend>rclcpp</depend>\n'
        '  <depend>std_msgs</depend>\n'
        '  <export>\n'
        '    <build_type>ament_cmake</build_type>\n'
        '</export>\n'
        '</package>\n'
    )


def _make_readme(pkg_name):
    return (
        f'# {pkg_name}\n\n'
        f'This is the README documentation for `{pkg_name}`.\n\n'
        '## Overview\n\n'
        'This README is intentionally longer than 300 characters so that `CacheYamlDumper` '
        'will deduplicate identical release and source README strings using YAML anchors '
        '(`&id...`) and aliases (`*id...`).\n\n'
        '## Usage\n\n'
        f'Run `ros2 run {pkg_name} {pkg_name}_node` to start the benchmark node.\n'
    )


def _make_changelog(pkg_name, version='1.0.0', num_lines=140):
    header = [
        '^' * len(f'Changelog for package {pkg_name}'),
        f'Changelog for package {pkg_name}',
        '^' * len(f'Changelog for package {pkg_name}'),
        '',
        f'{version} (2026-03-20)',
        '------------------',
    ]
    body = [f'* Change entry {i} for {pkg_name} in release {version}' for i in range(num_lines)]
    return '\n'.join(header + body)


def _create_local_rosdistro_fixture(base_dir, num_repos=25, release_versions=None):
    """Creates a local index-v4.yaml and distribution.yaml with `num_repos` repositories."""
    release_versions = release_versions or {}
    repositories = {}
    for i in range(num_repos):
        repo_name = f'pkg_{i:03d}'
        version = release_versions.get(repo_name, '1.0.0-1')
        repositories[repo_name] = {
            'doc': {
                'type': 'git',
                'url': f'https://github.com/ros2/{repo_name}.git',
                'version': 'rolling',
            },
            'release': {
                'tags': {'release': 'release/rolling/{package}/{version}'},
                'url': f'https://github.com/ros2-gbp/{repo_name}-release.git',
                'version': version,
            },
            'source': {
                'type': 'git',
                'url': f'https://github.com/ros2/{repo_name}.git',
                'version': 'rolling',
            },
            'status': 'maintained',
        }

    dist_data = {
        'release_platforms': {'ubuntu': ['noble']},
        'repositories': repositories,
        'type': 'distribution',
        'version': 2,
    }
    dist_path = os.path.join(base_dir, 'rolling_distribution.yaml')
    with open(dist_path, 'w') as f:
        yaml.safe_dump(dist_data, f)

    index_data = {
        'distributions': {
            'rolling': {
                'distribution': ['rolling_distribution.yaml'],
                'distribution_cache': 'rolling-cache.yaml',
                'distribution_status': 'active',
                'distribution_type': 'ros2',
                'python_version': 3,
            }
        },
        'type': 'index',
        'version': 4,
    }
    index_path = os.path.join(base_dir, 'index-v4.yaml')
    with open(index_path, 'w') as f:
        yaml.safe_dump(index_data, f)

    return index_path, dist_path


def _set_source_repo_update_time(repo_cache, dt):
    """Sets the update timestamp on a SourceRepositoryCache across both pre/post TTL fix."""
    if hasattr(repo_cache, 'set_last_update_time'):
        repo_cache.set_last_update_time(dt)
    else:
        repo_cache._data['_last_update_time'] = dt
        for pkg_name in repo_cache.keys():
            if isinstance(repo_cache._data.get(pkg_name), dict):
                repo_cache._data[pkg_name]['_last_update_time'] = dt


def _write_cache_files(dist_name, cache):
    """Writes `<dist_name>-cache.yaml` and `<dist_name>-cache.yaml.gz` like rosdistro_build_cache."""
    data = yaml.dump(cache.get_data(), Dumper=CacheYamlDumper).encode('utf-8')
    yaml_path = f'{dist_name}-cache.yaml'
    gz_path = f'{dist_name}-cache.yaml.gz'
    with open(yaml_path, 'wb') as f:
        f.write(data)
    with gzip.open(gz_path, 'wb') as f:
        f.write(data)
    return yaml_path, gz_path, data


def test_benchmark_cold_vs_warm_cache_lifecycle(benchmark_results):
    """Benchmarks cold build, warm TTL hit, warm ls-remote check, and partial invalidation."""
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix='rosdistro_bench_lifecycle_')
    limit = 10
    total_repos = 25
    simulated_net_delay_s = 0.012

    release_calls = []
    source_calls = []
    ls_remote_calls = []
    repo_hashes = {f'pkg_{i:03d}': f'{i + 1:040x}' for i in range(total_repos)}

    def fake_release_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
        time.sleep(simulated_net_delay_s)
        release_calls.append((pkg_name, filepath, repo.version))
        ver_no_inc = repo.version.split('-')[0]
        if filepath == 'package.xml':
            return _make_package_xml(pkg_name, version=ver_no_inc)
        if filepath == 'README.md':
            return _make_readme(pkg_name)
        if filepath == 'CHANGELOG.rst':
            return _make_changelog(pkg_name, version=ver_no_inc)
        return None

    def fake_source_provider(repo, filepaths=None):
        time.sleep(simulated_net_delay_s * 3)
        source_calls.append(repo.name)
        cache = SourceRepositoryCache.from_ref(repo_hashes[repo.name])
        cache.set_stars(42)
        cache.set_description(f'Repository for {repo.name}')
        cache.set_tags(['ros2', 'benchmark'])
        pkg_xml = sanitize_xml(_make_package_xml(repo.name, version='1.0.0', extra_comment=True))
        cache.add(repo.name, '', pkg_xml, 'package.xml')
        for fp in filepaths or ['CHANGELOG.rst', 'README.md']:
            if fp == 'README.md':
                content = sanitize_and_truncate_docs(_make_readme(repo.name))
            else:
                content = sanitize_and_truncate_docs(_make_changelog(repo.name, version='1.0.0'))
            cache.add(repo.name, '', content, fp)
        return cache

    def fake_git_command(cmd, *args):
        if cmd == 'ls-remote':
            time.sleep(simulated_net_delay_s)
            url = args[0]
            repo_name = url.rstrip('/').split('/')[-1].replace('.git', '')
            ls_remote_calls.append(repo_name)
            return {'returncode': 0, 'output': f'{repo_hashes[repo_name]}\trefs/heads/rolling'}
        return {'returncode': 0, 'output': ''}

    try:
        os.chdir(temp_dir)
        index_path, _ = _create_local_rosdistro_fixture(temp_dir, num_repos=total_repos)

        with patch(
            'rosdistro.distribution.Distribution.default_manifest_providers',
            [fake_release_provider],
        ), patch(
            'rosdistro.distribution.Distribution.default_source_manifest_providers',
            [fake_source_provider],
        ), patch(
            'rosdistro.distribution_cache.Git.command',
            side_effect=fake_git_command,
        ):
            # 1. Cold build (--preclean --include-source --limit 10)
            t0 = time.perf_counter()
            caches = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=True,
                ignore_local=True,
                include_source=True,
                limit=limit,
            )
            cold_time = time.perf_counter() - t0
            cache = caches['rolling']
            _write_cache_files('rolling', cache)

            assert len(cache.release_resources) == limit
            assert len(cache.source_repo_resources) == limit
            assert len(release_calls) == limit * 3
            assert len(source_calls) == limit
            assert len(ls_remote_calls) == 0

            benchmark_results.record(
                'Cold vs Warm',
                f'1. Cold build (limit={limit})',
                cold_time,
                f'release_fetches={len(release_calls)}, source_fetches={len(source_calls)}',
            )

            # 2. Warm build with fresh TTL (< 1 hour)
            release_calls.clear()
            source_calls.clear()
            ls_remote_calls.clear()

            now = datetime.datetime.now()
            for repo_cache in cache.source_repo_resources.values():
                _set_source_repo_update_time(repo_cache, now)
            _write_cache_files('rolling', cache)

            t0 = time.perf_counter()
            caches_warm_ttl = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=False,
                ignore_local=False,
                include_source=True,
                limit=limit,
            )
            warm_ttl_time = time.perf_counter() - t0

            assert len(release_calls) == 0, 'Warm build should not re-fetch release manifests'
            assert len(source_calls) == 0, 'Warm build should not re-fetch source repos'
            if hasattr(SourceRepositoryCache, 'last_update_time'):
                assert len(ls_remote_calls) == 0, 'Fresh TTL should skip git ls-remote'
            assert warm_ttl_time < cold_time

            benchmark_results.record(
                'Cold vs Warm',
                f'2. Warm build (TTL fresh, limit={limit})',
                warm_ttl_time,
                f'release_fetches={len(release_calls)}, ls_remote={len(ls_remote_calls)}, '
                f'speedup={cold_time / max(warm_ttl_time, 1e-6):.1f}x',
            )

            # 3. Warm build with expired TTL (> 1 hour, commit hashes unchanged)
            release_calls.clear()
            source_calls.clear()
            ls_remote_calls.clear()

            expired_time = datetime.datetime.now() - datetime.timedelta(hours=2)
            cache_expired = caches_warm_ttl['rolling']
            for repo_cache in cache_expired.source_repo_resources.values():
                _set_source_repo_update_time(repo_cache, expired_time)
            _write_cache_files('rolling', cache_expired)

            t0 = time.perf_counter()
            caches_warm_ls = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=False,
                ignore_local=False,
                include_source=True,
                limit=limit,
            )
            warm_ls_time = time.perf_counter() - t0

            assert len(release_calls) == 0
            assert len(source_calls) == 0, 'Unchanged git hash should avoid source tree fetch'
            assert len(ls_remote_calls) == limit

            benchmark_results.record(
                'Cold vs Warm',
                f'3. Warm build (TTL expired, limit={limit})',
                warm_ls_time,
                f'ls_remote={len(ls_remote_calls)}, source_fetches={len(source_calls)}, '
                f'speedup={cold_time / max(warm_ls_time, 1e-6):.1f}x',
            )

            # 4. Warm build with partial invalidation (1 release version bump + 1 source hash change)
            release_calls.clear()
            source_calls.clear()
            ls_remote_calls.clear()

            cache_partial = caches_warm_ls['rolling']
            for repo_cache in cache_partial.source_repo_resources.values():
                _set_source_repo_update_time(repo_cache, expired_time)
            _write_cache_files('rolling', cache_partial)

            # Bump pkg_000 release version to 2.0.0-1 and pkg_001 source commit hash
            _create_local_rosdistro_fixture(
                temp_dir,
                num_repos=total_repos,
                release_versions={'pkg_000': '2.0.0-1'},
            )
            repo_hashes['pkg_001'] = 'f' * 40

            t0 = time.perf_counter()
            caches_partial = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=False,
                ignore_local=False,
                include_source=True,
                limit=limit,
            )
            partial_time = time.perf_counter() - t0

            assert len(release_calls) == 3, 'Only pkg_000 (3 resources) should be re-fetched'
            assert {c[0] for c in release_calls} == {'pkg_000'}
            assert caches_partial['rolling'].release_resources['pkg_000']['version'] == '2.0.0-1'
            assert source_calls == ['pkg_001'], 'Only pkg_001 source repo should be re-fetched'
            assert caches_partial['rolling'].source_repo_resources['pkg_001'].ref() == 'f' * 40

            benchmark_results.record(
                'Cold vs Warm',
                f'4. Partial invalidation (limit={limit})',
                partial_time,
                f'release_fetches={len(release_calls)}, source_fetches={len(source_calls)}',
            )
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)


def test_benchmark_release_only_vs_include_source(benchmark_results):
    """Benchmarks release-only cache build vs --include-source cache build with --limit."""
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix='rosdistro_bench_rel_vs_src_')
    limit = 10
    simulated_net_delay_s = 0.005

    def fake_release_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
        time.sleep(simulated_net_delay_s)
        ver_no_inc = repo.version.split('-')[0]
        if filepath == 'package.xml':
            return _make_package_xml(pkg_name, version=ver_no_inc)
        if filepath == 'README.md':
            return _make_readme(pkg_name)
        if filepath == 'CHANGELOG.rst':
            return _make_changelog(pkg_name, version=ver_no_inc)
        return None

    def fake_source_provider(repo, filepaths=None):
        time.sleep(simulated_net_delay_s * 3)
        cache = SourceRepositoryCache.from_ref('a' * 40)
        cache.set_stars(15)
        cache.set_description(f'Source description for {repo.name}')
        cache.set_tags(['ros2'])
        cache.add(repo.name, '', sanitize_xml(_make_package_xml(repo.name)), 'package.xml')
        for fp in filepaths or ['CHANGELOG.rst', 'README.md']:
            content = _make_readme(repo.name) if fp == 'README.md' else _make_changelog(repo.name)
            cache.add(repo.name, '', sanitize_and_truncate_docs(content), fp)
        return cache

    try:
        os.chdir(temp_dir)
        index_path, _ = _create_local_rosdistro_fixture(temp_dir, num_repos=20)

        with patch(
            'rosdistro.distribution.Distribution.default_manifest_providers',
            [fake_release_provider],
        ), patch(
            'rosdistro.distribution.Distribution.default_source_manifest_providers',
            [fake_source_provider],
        ):
            # Release-only build
            t0 = time.perf_counter()
            rel_caches = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=True,
                ignore_local=True,
                include_source=False,
                limit=limit,
            )
            rel_time = time.perf_counter() - t0
            _, _, rel_bytes = _write_cache_files('rolling', rel_caches['rolling'])

            assert len(rel_caches['rolling'].release_resources) == limit
            assert len(rel_caches['rolling'].source_repo_resources) == 0

            benchmark_results.record(
                'Release vs Source',
                f'Release-only (limit={limit})',
                rel_time,
                f'yaml_size={len(rel_bytes) / 1024:.1f} KiB',
            )

            # Release + Source (--include-source) build
            t0 = time.perf_counter()
            src_caches = generate_distribution_caches(
                index_path,
                dist_names=['rolling'],
                preclean=True,
                ignore_local=True,
                include_source=True,
                limit=limit,
            )
            src_time = time.perf_counter() - t0
            _, _, src_bytes = _write_cache_files('rolling', src_caches['rolling'])

            assert len(src_caches['rolling'].release_resources) == limit
            assert len(src_caches['rolling'].source_repo_resources) == limit

            benchmark_results.record(
                'Release vs Source',
                f'Include-source (limit={limit})',
                src_time,
                f'yaml_size={len(src_bytes) / 1024:.1f} KiB',
            )
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)


def test_benchmark_limit_scaling(benchmark_results):
    """Evaluates how build time and cache size scale across --limit values (5, 10, 20)."""
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix='rosdistro_bench_limit_scale_')
    simulated_net_delay_s = 0.002

    def fake_release_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
        time.sleep(simulated_net_delay_s)
        ver_no_inc = repo.version.split('-')[0]
        if filepath == 'package.xml':
            return _make_package_xml(pkg_name, version=ver_no_inc)
        if filepath == 'README.md':
            return _make_readme(pkg_name)
        if filepath == 'CHANGELOG.rst':
            return _make_changelog(pkg_name, version=ver_no_inc)
        return None

    def fake_source_provider(repo, filepaths=None):
        time.sleep(simulated_net_delay_s * 2)
        cache = SourceRepositoryCache.from_ref('b' * 40)
        cache.set_stars(10)
        cache.set_description(f'Repo {repo.name}')
        cache.set_tags(['ros2'])
        cache.add(repo.name, '', sanitize_xml(_make_package_xml(repo.name)), 'package.xml')
        for fp in filepaths or ['CHANGELOG.rst', 'README.md']:
            content = _make_readme(repo.name) if fp == 'README.md' else _make_changelog(repo.name)
            cache.add(repo.name, '', sanitize_and_truncate_docs(content), fp)
        return cache

    try:
        os.chdir(temp_dir)
        index_path, _ = _create_local_rosdistro_fixture(temp_dir, num_repos=30)

        with patch(
            'rosdistro.distribution.Distribution.default_manifest_providers',
            [fake_release_provider],
        ), patch(
            'rosdistro.distribution.Distribution.default_source_manifest_providers',
            [fake_source_provider],
        ):
            timings = {}
            for limit in (5, 10, 20):
                t0 = time.perf_counter()
                caches = generate_distribution_caches(
                    index_path,
                    dist_names=['rolling'],
                    preclean=True,
                    ignore_local=True,
                    include_source=True,
                    limit=limit,
                )
                elapsed = time.perf_counter() - t0
                timings[limit] = elapsed
                cache = caches['rolling']
                assert len(cache.release_resources) == limit
                assert len(cache.source_repo_resources) == limit
                _, _, raw_bytes = _write_cache_files('rolling', cache)

                benchmark_results.record(
                    'Limit Scaling',
                    f'Build with --limit {limit}',
                    elapsed,
                    f'pkgs={len(cache.release_resources)}, '
                    f'repos={len(cache.source_repo_resources)}, '
                    f'yaml={len(raw_bytes) / 1024:.1f} KiB',
                )

            assert timings[5] < timings[20]
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)


def test_benchmark_yaml_deduplication_and_schema(benchmark_results):
    """Benchmarks YAML anchor/alias deduplication (&id/*id) between release and source caches."""
    num_packages = 15
    dist_file_data = {
        'release_platforms': {'ubuntu': ['noble']},
        'repositories': {
            f'pkg_{i:03d}': {
                'release': {
                    'tags': {'release': 'release/rolling/{package}/{version}'},
                    'url': f'https://github.com/ros2-gbp/pkg_{i:03d}-release.git',
                    'version': '1.0.0-1',
                },
                'source': {
                    'type': 'git',
                    'url': f'https://github.com/ros2/pkg_{i:03d}.git',
                    'version': 'rolling',
                },
            }
            for i in range(num_packages)
        },
        'type': 'distribution',
        'version': 2,
    }

    # Build a cache with unlinked duplicate strings vs linked strings via CachedSourceManifestProvider
    unlinked_cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
    linked_cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    for i in range(num_packages):
        pkg_name = f'pkg_{i:03d}'
        rel_xml = sanitize_xml(_make_package_xml(pkg_name, version='1.0.0'))
        rel_readme = sanitize_and_truncate_docs(_make_readme(pkg_name))
        rel_changelog = sanitize_and_truncate_docs(_make_changelog(pkg_name, version='1.0.0'))

        # Populate release_resources in both caches
        for target_cache in (unlinked_cache, linked_cache):
            target_cache.release_resources[pkg_name] = {
                'version': '1.0.0-1',
                'package.xml': rel_xml,
                'README.md': rel_readme,
                'CHANGELOG.rst': rel_changelog,
            }

        # Populate unlinked_cache with distinct str instances (not deduplicated)
        src_unlinked = SourceRepositoryCache.from_ref(f'{i + 1:040x}')
        src_unlinked.set_stars(i * 5)
        src_unlinked.set_description(f'Repo {pkg_name}')
        src_unlinked.set_tags(['ros2', 'cached'])
        src_unlinked.add(pkg_name, '', ''.join([c for c in rel_xml]), 'package.xml', increment_update_time=False)
        src_unlinked.add(pkg_name, '', ''.join([c for c in rel_readme]), 'README.md', increment_update_time=False)
        src_unlinked.add(pkg_name, '', ''.join([c for c in rel_changelog]), 'CHANGELOG.rst', increment_update_time=False)
        unlinked_cache.source_repo_resources[pkg_name] = src_unlinked

        # Populate linked_cache using CachedSourceManifestProvider to trigger deduplication
        def _provider(repo, filepaths=None, _pkg=pkg_name, _idx=i):
            c = SourceRepositoryCache.from_ref(f'{_idx + 1:040x}')
            c.set_stars(_idx * 5)
            c.set_description(f'Repo {_pkg}')
            c.set_tags(['ros2', 'cached'])
            c.add(_pkg, '', _make_package_xml(_pkg, version='1.0.0', extra_comment=True), 'package.xml', increment_update_time=False)
            c.add(_pkg, '', _make_readme(_pkg), 'README.md', increment_update_time=False)
            c.add(_pkg, '', _make_changelog(_pkg, version='1.0.0'), 'CHANGELOG.rst', increment_update_time=False)
            return c

        cached_src_mp = CachedSourceManifestProvider(linked_cache, [_provider])
        repo_spec = linked_cache.distribution_file.repositories[pkg_name].source_repository
        cached_src_mp(repo_spec)

    # Verify object identity linking in linked_cache
    for i in range(num_packages):
        pkg_name = f'pkg_{i:03d}'
        for res_type in ('package.xml', 'README.md', 'CHANGELOG.rst'):
            assert (
                linked_cache.source_repo_resources[pkg_name][pkg_name][res_type]
                is linked_cache.release_resources[pkg_name][res_type]
            ), f'Expected {res_type} for {pkg_name} to share object identity with release_resources'

    # Benchmark YAML dump without deduplication vs with deduplication
    t0 = time.perf_counter()
    unlinked_yaml = yaml.dump(unlinked_cache.get_data(), Dumper=CacheYamlDumper)
    unlinked_dump_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    linked_yaml = yaml.dump(linked_cache.get_data(), Dumper=CacheYamlDumper)
    linked_dump_time = time.perf_counter() - t0

    anchors = re.findall(r'&id\d+', linked_yaml)
    aliases = re.findall(r'\*id\d+', linked_yaml)
    assert len(anchors) == num_packages * 3, f'Expected {num_packages * 3} YAML anchors, got {len(anchors)}'
    assert len(aliases) == num_packages * 3, f'Expected {num_packages * 3} YAML aliases, got {len(aliases)}'
    assert len(linked_yaml) < len(unlinked_yaml) * 0.65, (
        f'Expected deduplicated YAML ({len(linked_yaml)} B) to be at least 35% smaller '
        f'than unlinked YAML ({len(unlinked_yaml)} B)'
    )

    savings_pct = (1.0 - len(linked_yaml) / len(unlinked_yaml)) * 100.0
    benchmark_results.record(
        'Deduplication',
        f'Unlinked YAML dump ({num_packages} pkgs)',
        unlinked_dump_time,
        f'size={len(unlinked_yaml) / 1024:.1f} KiB, aliases=0',
    )
    benchmark_results.record(
        'Deduplication',
        f'Deduplicated YAML dump ({num_packages} pkgs)',
        linked_dump_time,
        f'size={len(linked_yaml) / 1024:.1f} KiB, aliases={len(aliases)} ({savings_pct:.1f}% saved)',
    )

    # Validate round-trip load and schema compliance
    t0 = time.perf_counter()
    reloaded_data = yaml.safe_load(linked_yaml)
    reload_time = time.perf_counter() - t0
    schema = _load_cache_schema()
    validate(instance=reloaded_data, schema=schema)

    benchmark_results.record(
        'Deduplication',
        f'Deduplicated YAML safe_load ({num_packages} pkgs)',
        reload_time,
        'schema valid (version 3)',
    )


def test_benchmark_yaml_vs_gzip_load_and_save(benchmark_results):
    """Benchmarks reading/writing uncompressed .yaml vs compressed .yaml.gz cache files."""
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix='rosdistro_bench_io_')

    # If dev_test/rolling-cache.yaml exists in the repo, benchmark loading it;
    # otherwise synthesize a representative 20-package cache.
    dev_test_yaml = os.path.join(original_cwd, 'dev_test', 'rolling-cache.yaml')
    try:
        os.chdir(temp_dir)
        if os.path.exists(dev_test_yaml):
            with open(dev_test_yaml, 'rb') as f:
                raw_bytes = f.read()
            source_label = 'dev_test/rolling-cache'
        else:
            _create_local_rosdistro_fixture(temp_dir, num_repos=20)
            dist_file_data = yaml.safe_load(open(os.path.join(temp_dir, 'rolling_distribution.yaml')))
            synth_cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
            for i in range(20):
                pkg_name = f'pkg_{i:03d}'
                synth_cache.release_resources[pkg_name] = {
                    'version': '1.0.0-1',
                    'package.xml': sanitize_xml(_make_package_xml(pkg_name)),
                    'README.md': sanitize_and_truncate_docs(_make_readme(pkg_name)),
                    'CHANGELOG.rst': sanitize_and_truncate_docs(_make_changelog(pkg_name)),
                }
            raw_bytes = yaml.dump(synth_cache.get_data(), Dumper=CacheYamlDumper).encode('utf-8')
            source_label = 'synthetic-20-pkgs'

        yaml_path = os.path.join(temp_dir, 'rolling-cache.yaml')
        gz_path = os.path.join(temp_dir, 'rolling-cache.yaml.gz')

        # 1. Benchmark writing .yaml
        t0 = time.perf_counter()
        with open(yaml_path, 'wb') as f:
            f.write(raw_bytes)
        write_yaml_time = time.perf_counter() - t0

        # 2. Benchmark writing .yaml.gz
        t0 = time.perf_counter()
        with gzip.open(gz_path, 'wb') as f:
            f.write(raw_bytes)
        write_gz_time = time.perf_counter() - t0

        yaml_size = os.path.getsize(yaml_path)
        gz_size = os.path.getsize(gz_path)
        assert gz_size < yaml_size

        # 3. Benchmark reading + parsing .yaml
        t0 = time.perf_counter()
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data_from_yaml = yaml.safe_load(f.read())
        read_yaml_time = time.perf_counter() - t0

        # 4. Benchmark reading + parsing .yaml.gz
        t0 = time.perf_counter()
        with gzip.open(gz_path, 'rb') as f:
            data_from_gz = yaml.safe_load(f.read())
        read_gz_time = time.perf_counter() - t0

        assert data_from_yaml['name'] == data_from_gz['name'] == 'rolling'

        benchmark_results.record(
            'YAML vs Gzip I/O',
            f'Write .yaml ({source_label})',
            write_yaml_time,
            f'size={yaml_size / 1024:.1f} KiB',
        )
        benchmark_results.record(
            'YAML vs Gzip I/O',
            f'Write .yaml.gz ({source_label})',
            write_gz_time,
            f'size={gz_size / 1024:.1f} KiB ({gz_size / yaml_size * 100:.1f}% of raw)',
        )
        benchmark_results.record(
            'YAML vs Gzip I/O',
            f'Load + parse .yaml ({source_label})',
            read_yaml_time,
            'uncompressed local file',
        )
        benchmark_results.record(
            'YAML vs Gzip I/O',
            f'Load + parse .yaml.gz ({source_label})',
            read_gz_time,
            'gzip decompressed + parsed',
        )
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)


def test_benchmark_sanitization_and_truncation(benchmark_results):
    """Benchmarks sanitize_xml and sanitize_and_truncate_docs on large inputs."""
    large_changelog = _make_changelog('large_pkg', version='2.5.0', num_lines=3500)
    large_xml = _make_package_xml('large_pkg', version='2.5.0', extra_comment=True)
    iterations = 100

    t0 = time.perf_counter()
    for _ in range(iterations):
        truncated = sanitize_and_truncate_docs(large_changelog, max_length=100)
    truncate_time = time.perf_counter() - t0

    # Re-sanitizing already truncated content (warm cache load path)
    t0 = time.perf_counter()
    for _ in range(iterations):
        re_truncated = sanitize_and_truncate_docs(truncated, max_length=100)
        re_re_truncated = sanitize_and_truncate_docs(re_truncated, max_length=100)
    re_truncate_time = time.perf_counter() - t0

    assert re_truncated == re_re_truncated
    assert len(truncated.splitlines()) == 100
    assert 'Truncated content at 100 of ' in truncated

    t0 = time.perf_counter()
    for _ in range(iterations):
        sanitized_xml = sanitize_xml(large_xml)
    xml_time = time.perf_counter() - t0

    assert '<!--' not in sanitized_xml
    assert '\n' not in sanitized_xml

    benchmark_results.record(
        'Sanitize & Truncate',
        f'Truncate 3500-line doc (x{iterations})',
        truncate_time,
        f'reduced {len(large_changelog)} B -> {len(truncated)} B',
    )
    benchmark_results.record(
        'Sanitize & Truncate',
        f'Warm-cache re-truncate (x{iterations})',
        re_truncate_time,
        'cached doc re-sanitization',
    )
    benchmark_results.record(
        'Sanitize & Truncate',
        f'sanitize_xml (x{iterations})',
        xml_time,
        f'reduced {len(large_xml)} B -> {len(sanitized_xml)} B',
    )


def test_benchmark_live_e2e_rolling_cold_and_warm(benchmark_results):
    """Live end-to-end benchmark against rosdistro index-v4.yaml using --limit."""
    limit = 10
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix='rosdistro_bench_live_e2e_')

    env_overrides = {'ROSDISTRO_RETRY_MAX': '10'}
    if not os.environ.get('SSH_AUTH_SOCK'):
        # Avoid rewriting public https://github.com/ URLs to ssh://git@github.com/ when no SSH agent is active
        env_overrides['GIT_CONFIG_GLOBAL'] = '/dev/null'

    try:
        os.chdir(temp_dir)
        schema = _load_cache_schema()

        with patch.dict(os.environ, env_overrides):
            # 1. Live cold build via CLI with --include-source --limit 5 --preclean
            cold_args = build_cache_parse_args([
                '--include-source',
                '--limit',
                str(limit),
                '--preclean',
                DEFAULT_INDEX_URL,
                'rolling',
            ])
            assert cold_args.limit == limit
            assert cold_args.include_source is True
            assert cold_args.preclean is True

            t0 = time.perf_counter()
            try:
                with patch('rosdistro.cli.rosdistro_build_cache.parse_args', return_value=cold_args):
                    rc_cold = build_cache_main()
            except Exception as exc:
                pytest.skip(f'Live network benchmark skipped (network unavailable: {exc})')
            live_cold_time = time.perf_counter() - t0

            if rc_cold:
                pytest.skip('Live network benchmark skipped (unable to reach rosdistro index)')

            assert os.path.exists('rolling-cache.yaml')
            assert os.path.exists('rolling-cache.yaml.gz')

            with open('rolling-cache.yaml', 'r', encoding='utf-8') as f:
                cold_yaml_str = f.read()
            cold_data = yaml.safe_load(cold_yaml_str)
            validate(instance=cold_data, schema=schema)

            assert len(cold_data['release_resources']) <= limit
            assert len(cold_data['release_resources']) > 0
            assert len(cold_data['source_repo_resources']) <= limit
            assert len(cold_data['source_repo_resources']) > 0

            aliases = re.findall(r'\*id\d+', cold_yaml_str)
            assert len(aliases) > 0, (
                'Expected YAML aliases (*id*) for matching release and source resources '
                f'in rolling with --limit {limit}'
            )

            benchmark_results.record(
                'Live E2E (rolling)',
                f'Cold CLI build (--limit {limit})',
                live_cold_time,
                f'rel={len(cold_data["release_resources"])}, '
                f'src={len(cold_data["source_repo_resources"])}, '
                f'aliases={len(aliases)}, size={len(cold_yaml_str) / 1024:.1f} KiB',
            )

            # 2. Live warm build via CLI reusing local rolling-cache.yaml (no --preclean)
            warm_args = build_cache_parse_args([
                '--include-source',
                '--limit',
                str(limit),
                DEFAULT_INDEX_URL,
                'rolling',
            ])
            t0 = time.perf_counter()
            with patch('rosdistro.cli.rosdistro_build_cache.parse_args', return_value=warm_args):
                rc_warm = build_cache_main()
            live_warm_time = time.perf_counter() - t0

            assert not rc_warm
            with open('rolling-cache.yaml', 'r', encoding='utf-8') as f:
                warm_yaml_str = f.read()
            warm_data = yaml.safe_load(warm_yaml_str)
            validate(instance=warm_data, schema=schema)

            assert live_warm_time < live_cold_time, (
                f'Expected warm build ({live_warm_time:.2f}s) to be faster than '
                f'cold build ({live_cold_time:.2f}s)'
            )

            benchmark_results.record(
                'Live E2E (rolling)',
                f'Warm CLI build (--limit {limit})',
                live_warm_time,
                f'speedup={live_cold_time / max(live_warm_time, 1e-6):.1f}x vs cold build',
            )
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)
