import copy
import datetime
import gzip
import json
import os
from unittest.mock import patch

from jsonschema import validate
import pytest
import yaml

from rosdistro import get_index
from rosdistro.distribution_cache import CacheConfig, DistributionCache
from rosdistro.distribution_cache_generator import (
    generate_distribution_cache,
    generate_distribution_caches,
)
from rosdistro.manifest_provider.cache import sanitize_and_truncate_docs
from rosdistro.source_repository_cache import SourceRepositoryCache

from . import path_to_url


SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'rosdistro', 'rosdistro_cache_3.schema.json',
)


def _load_schema():
    with open(SCHEMA_PATH, 'r') as f:
        return json.load(f)


def _pkg_xml(name, version='1.0.0'):
    return (
        f'<?xml version="1.0"?>'
        f'<package format="2">'
        f'<name>{name}</name>'
        f'<version>{version}</version>'
        f'<description>{name} description</description>'
        f'<maintainer email="dev@example.com">Dev</maintainer>'
        f'<license>BSD</license>'
        f'</package>'
    )


def _write_index_and_distros(tmp_path, distros_dict):
    """Write an index-v4.yaml and per-distro distribution.yaml files under tmp_path."""
    index_data = {
        'type': 'index',
        'version': 4,
        'distributions': {},
    }
    for dist_name, repos in distros_dict.items():
        dist_dir = tmp_path / dist_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        dist_file = dist_dir / 'distribution.yaml'
        dist_file.write_text(yaml.safe_dump({
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


def _make_source_repo_cache(
    ref,
    pkg_name='shared_pkg',
    pkg_version='1.0.0',
    readme='# Shared README\nLine 2',
    changelog='1.0.0\n* init',
    stars=42,
    description='Shared repo',
    tags=None,
    last_commit_time='2026-04-01T12:00:00Z',
    contributing='# Contributing',
    root_readme='# Root README',
    launch_files=None,
    msg_files=None,
    srv_files=None,
    action_files=None,
    last_update_time=None,
    include_assets=True,
):
    rc = SourceRepositoryCache.from_ref(ref)
    if stars is not None:
        rc.set_stars(stars)
    if description is not None:
        rc.set_description(description)
    if tags is not None:
        rc.set_tags(list(tags))
    if last_commit_time is not None:
        rc.set_last_commit_time(last_commit_time)
    if contributing is not None:
        rc.set_contributing(contributing)
    if root_readme is not None:
        rc.set_readme(root_readme)
    rc.add(pkg_name, pkg_name, _pkg_xml(pkg_name, pkg_version), 'package.xml')
    if readme is not None:
        rc.add(pkg_name, pkg_name, readme, 'README.md')
    if changelog is not None:
        rc.add(pkg_name, pkg_name, changelog, 'CHANGELOG.rst')
    if include_assets:
        rc.add(pkg_name, pkg_name, list(launch_files or ['launch/run.launch.py']), 'launch_files')
        rc.add(pkg_name, pkg_name, list(msg_files or ['msg/Foo.msg']), 'msg_files')
        rc.add(pkg_name, pkg_name, list(srv_files or []), 'srv_files')
        rc.add(pkg_name, pkg_name, list(action_files or []), 'action_files')
    if last_update_time is not None:
        rc.set_last_update_time(last_update_time)
    return rc


def _make_fake_providers(release_map, source_map):
    """Build recording fake release and source manifest providers."""
    release_calls = []
    source_calls = []

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        tag = repo.get_release_tag(pkg_name)
        release_calls.append((dist_name, repo.url, tag, pkg_name, filepath))
        key = (repo.type, repo.url, tag, filepath)
        if key not in release_map:
            if filepath != 'package.xml':
                return None
            raise RuntimeError(f'Missing test release resource: {key}')
        return release_map[key]

    def fake_source_mp(repo, filepaths=None, metadata=None):
        source_calls.append((repo.name, repo.type, repo.url, repo.version))
        key = (repo.type, repo.url, repo.version)
        if key not in source_map:
            raise RuntimeError(f'Missing test source repo: {key}')
        val = source_map[key]
        return val(repo, filepaths, metadata) if callable(val) else copy.deepcopy(val)

    return fake_release_mp, fake_source_mp, release_calls, source_calls


def _shared_repo_stanza(
    release_tag='release/shared/{package}/{version}',
    release_version='1.0.0-1',
    release_url='https://github.com/org/shared-release.git',
    source_url='https://github.com/org/shared.git',
    source_version='main',
    pkg_name='shared_pkg',
):
    stanza = {
        'release': {
            'packages': [pkg_name],
            'tags': {'release': release_tag},
            'url': release_url,
            'version': release_version,
        },
    }
    if source_url is not None:
        stanza['source'] = {
            'type': 'git',
            'url': source_url,
            'version': source_version,
        }
    return stanza


def test_side_by_side_in_memory_neighbor_copy_and_exact_equivalence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    sha = 'a' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'package.xml'):
            _pkg_xml('shared_pkg', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# Shared Release README\nLine 2\n',
        # CHANGELOG.rst intentionally absent (returns None, confirmed missing)
    }
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache(sha, tags=['ros2', 'shared']),
    }

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers(release_map, source_map)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'],
            preclean=False, config='extended',
        )

    # distro_a fetched all 3 release resources and 1 source repo; distro_b fetched ZERO remotely
    assert [c[0] for c in rel_calls] == ['distro_a', 'distro_a', 'distro_a']
    assert len(src_calls) == 1
    mock_git.return_value.command.assert_not_called()

    # Validate both caches against Cache Format 3 schema
    data_a = caches['distro_a'].get_data(version=3)
    data_b = caches['distro_b'].get_data(version=3)
    validate(instance=data_a, schema=schema)
    validate(instance=data_b, schema=schema)

    # Now build distro_b with preclean=True and verify strictly identical content
    fake_rel2, fake_src2, rel_calls2, src_calls2 = _make_fake_providers(release_map, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel2]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src2]):
        clean_caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'],
            preclean=True, config='extended',
        )

    # With preclean=True, BOTH distros must fetch remotely (no neighbor copying!)
    assert [c[0] for c in rel_calls2] == [
        'distro_a', 'distro_a', 'distro_a', 'distro_b', 'distro_b', 'distro_b',
    ]
    assert len(src_calls2) == 2

    # Content in distro_b must be strictly identical to preclean build (modulo _last_update_time)
    assert data_b['release_resources'] == clean_caches['distro_b'].get_data(version=3)['release_resources']
    b_src = copy.deepcopy(data_b['source_repo_resources']['shared_repo'])
    clean_src = copy.deepcopy(clean_caches['distro_b'].get_data(version=3)['source_repo_resources']['shared_repo'])
    assert '_last_update_time' in b_src
    b_src.pop('_last_update_time', None)
    clean_src.pop('_last_update_time', None)
    assert b_src == clean_src

    # Verify deep-copy isolation between distro_a and distro_b
    caches['distro_b'].source_repo_resources['shared_repo'].tags().append('mutated')
    assert 'mutated' not in caches['distro_a'].source_repo_resources['shared_repo'].tags()


def test_preclean_disables_on_disk_and_explicit_neighbor_caches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = 'b' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # Write a fresh on-disk neighbor cache for distro_a
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
    neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, last_update_time=datetime.datetime.now(),
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'package.xml'):
            _pkg_xml('shared_pkg', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# Fresh Remote README',
    }
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache('c' * 40, readme='# Fresh Remote Source'),
    }
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers(release_map, source_map)

    index = get_index(path_to_url(index_path))
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        cache_b = generate_distribution_cache(
            index, 'distro_b', preclean=True, config='extended',
            neighbor_caches={'distro_a': neighbor},
        )

    assert len(rel_calls) == 3
    assert len(src_calls) == 1
    assert cache_b.release_resources['shared_pkg']['README.md'] == '# Fresh Remote README'
    assert cache_b.source_repo_resources['shared_repo'].ref() == 'c' * 40


@pytest.mark.parametrize('compressed', [False, True])
def test_on_disk_neighbor_cache_discovery_and_preserves_ttl(tmp_path, monkeypatch, compressed):
    monkeypatch.chdir(tmp_path)
    sha = 'd' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    ten_min_ago = datetime.datetime.now() - datetime.timedelta(minutes=10)
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': '# On-disk README',
        'CHANGELOG.rst': None,
    }
    neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, last_update_time=ten_min_ago,
    )
    dumped = yaml.safe_dump(neighbor.get_data(version=3)).encode('utf-8')
    if compressed:
        with gzip.open(tmp_path / 'distro_a-extended-cache.yaml.gz', 'wb') as f:
            f.write(dumped)
    else:
        (tmp_path / 'distro_a-extended-cache.yaml').write_bytes(dumped)

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert rel_calls == []
    assert src_calls == []
    mock_git.return_value.command.assert_not_called()
    cache_b = caches['distro_b']
    assert cache_b.release_resources['shared_pkg']['README.md'] == '# On-disk README'
    assert cache_b.release_resources['shared_pkg']['CHANGELOG.rst'] is None
    assert cache_b.source_repo_resources['shared_repo'].ref() == sha
    # TTL timestamp must be preserved (not refreshed) when copied via TTL
    assert cache_b.source_repo_resources['shared_repo'].last_update_time() == ten_min_ago


def test_ignore_local_skips_disk_neighbors_but_reuses_in_memory_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(
        tmp_path, {'distro_disk': repos, 'distro_a': repos, 'distro_b': repos}
    )

    # Put a fresh local cache on disk for distro_disk
    disk_neighbor = DistributionCache(
        'distro_disk',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    disk_neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': '# Disk Neighbor README',
        'CHANGELOG.rst': None,
    }
    disk_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        '1' * 40, last_update_time=datetime.datetime.now(),
    )
    (tmp_path / 'distro_disk-extended-cache.yaml').write_text(
        yaml.safe_dump(disk_neighbor.get_data(version=3))
    )

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'package.xml'):
            _pkg_xml('shared_pkg', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# Authoritative Remote README',
    }
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache('2' * 40),
    }
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers(release_map, source_map)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'],
            preclean=False, ignore_local=True, config='extended',
        )

    # distro_a ignored distro_disk on disk and fetched remotely; distro_b reused distro_a in memory
    assert [c[0] for c in rel_calls] == ['distro_a', 'distro_a', 'distro_a']
    assert len(src_calls) == 1
    assert caches['distro_a'].release_resources['shared_pkg']['README.md'] == '# Authoritative Remote README'
    assert caches['distro_b'].release_resources['shared_pkg']['README.md'] == '# Authoritative Remote README'
    assert caches['distro_b'].source_repo_resources['shared_repo'].ref() == '2' * 40


def test_release_strict_rejections_different_tag_version_url_type_and_max_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    long_readme = '\n'.join(f'Line {i}' for i in range(1, 121))

    repos_a = {
        'tag_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_tag', release_tag='release/distro_a/{package}/{version}', source_url=None,
        ),
        'ver_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_ver', release_version='1.0.0-1', source_url=None,
        ),
        'url_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_url', release_url='https://github.com/org/a-release.git', source_url=None,
        ),
        'same_repo': _shared_repo_stanza(
            pkg_name='pkg_same', source_url=None,
        ),
    }
    repos_b = {
        'tag_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_tag', release_tag='release/distro_b/{package}/{version}', source_url=None,
        ),
        'ver_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_ver', release_version='1.0.1-1', source_url=None,
        ),
        'url_diff_repo': _shared_repo_stanza(
            pkg_name='pkg_url', release_url='https://github.com/org/b-release.git', source_url=None,
        ),
        'same_repo': _shared_repo_stanza(
            pkg_name='pkg_same', source_url=None,
        ),
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    # Neighbor distro_a has max_lines=50 for README.md
    neighbor_cfg = CacheConfig({
        'release_package_resources': {
            'package.xml': True,
            'README.md': {'enabled': True, 'max_lines': 50},
            'CHANGELOG.rst': True,
        }
    })
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos_a},
        config=neighbor_cfg,
    )
    for pkg_name, ver in (('pkg_tag', '1.0.0-1'), ('pkg_ver', '1.0.0-1'), ('pkg_url', '1.0.0-1'), ('pkg_same', '1.0.0-1')):
        neighbor.release_resources[pkg_name] = {
            'version': ver,
            'package.xml': _pkg_xml(pkg_name, ver.split('-')[0]),
            'README.md': sanitize_and_truncate_docs(long_readme, 50),
            'CHANGELOG.rst': 'neighbor changelog',
        }
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/distro_b/pkg_tag/1.0.0-1', 'package.xml'):
            _pkg_xml('pkg_tag', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/distro_b/pkg_tag/1.0.0-1', 'README.md'):
            '# distro_b tag README',
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_ver/1.0.1-1', 'package.xml'):
            _pkg_xml('pkg_ver', '1.0.1'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_ver/1.0.1-1', 'README.md'):
            '# distro_b ver README',
        ('git', 'https://github.com/org/b-release.git', 'release/shared/pkg_url/1.0.0-1', 'package.xml'):
            _pkg_xml('pkg_url', '1.0.0'),
        ('git', 'https://github.com/org/b-release.git', 'release/shared/pkg_url/1.0.0-1', 'README.md'):
            '# distro_b url README',
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_same/1.0.0-1', 'README.md'):
            long_readme,
    }
    fake_rel, fake_src, rel_calls, _ = _make_fake_providers(release_map, {})

    # Target distro_b wants max_lines=100 for README.md (different from neighbor's 50!)
    target_cfg = CacheConfig({
        'release_package_resources': {
            'package.xml': True,
            'README.md': {'enabled': True, 'max_lines': 100},
            'CHANGELOG.rst': True,
        }
    })
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config=target_cfg,
        )

    called_pkg_files = {(c[3], c[4]) for c in rel_calls}
    # pkg_tag, pkg_ver, pkg_url MUST all have been fetched remotely (all 3 resources each)
    for p in ('pkg_tag', 'pkg_ver', 'pkg_url'):
        assert (p, 'package.xml') in called_pkg_files
        assert (p, 'README.md') in called_pkg_files
        assert (p, 'CHANGELOG.rst') in called_pkg_files
    # For pkg_same: package.xml and CHANGELOG.rst copied from neighbor, README.md fetched remotely due to max_lines mismatch!
    assert ('pkg_same', 'package.xml') not in called_pkg_files
    assert ('pkg_same', 'CHANGELOG.rst') not in called_pkg_files
    assert ('pkg_same', 'README.md') in called_pkg_files
    assert 'Truncated content at 100 of 120 lines' in caches['distro_b'].release_resources['pkg_same']['README.md']


def test_release_max_lines_smaller_target_also_rejected_to_preserve_exact_line_count(tmp_path, monkeypatch):
    """If neighbor truncated 120 lines to 100, target wanting 50 MUST NOT re-truncate neighbor's
    output (which would say 'at 50 of 100 lines' instead of 'at 50 of 120 lines').
    """
    monkeypatch.chdir(tmp_path)
    long_readme = '\n'.join(f'Line {i}' for i in range(1, 121))
    repos = {'shared_repo': _shared_repo_stanza(source_url=None)}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig({'release_package_resources': {'README.md': {'max_lines': 100}}}),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': sanitize_and_truncate_docs(long_readme, 100),
    }
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            long_readme,
    }
    fake_rel, fake_src, rel_calls, _ = _make_fake_providers(release_map, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False,
            config={'release_package_resources': {'README.md': {'max_lines': 50}}},
        )

    assert {(c[3], c[4]) for c in rel_calls} == {('shared_pkg', 'README.md')}
    assert 'Truncated content at 50 of 120 lines' in caches['distro_b'].release_resources['shared_pkg']['README.md']


def test_untruncated_neighbor_max_lines_none_truncates_identically_for_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    long_readme = '\n'.join(f'Line {i}' for i in range(1, 15))
    sha = 'e' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    neighbor_cfg = CacheConfig({
        'release_package_resources': {'README.md': {'enabled': True, 'max_lines': None}},
        'source_package_resources': {'package.xml': True, 'README.md': {'enabled': True, 'max_lines': None}},
    })
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=neighbor_cfg,
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': sanitize_and_truncate_docs(long_readme, None),
    }
    neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, readme=sanitize_and_truncate_docs(long_readme, None),
        changelog=None, stars=None, description=None, tags=None,
        last_commit_time=None, contributing=None, root_readme=None,
        include_assets=False, last_update_time=datetime.datetime.now(),
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    target_cfg = CacheConfig({
        'release_package_resources': {'README.md': {'enabled': True, 'max_lines': 5}},
        'source_package_resources': {'package.xml': True, 'README.md': {'enabled': True, 'max_lines': 5}},
    })
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config=target_cfg,
        )

    assert rel_calls == []
    assert src_calls == []
    expected = sanitize_and_truncate_docs(long_readme, 5)
    assert caches['distro_b'].release_resources['shared_pkg']['README.md'] == expected
    assert caches['distro_b'].source_repo_resources['shared_repo']['shared_pkg']['README.md'] == expected


def test_expired_git_neighbor_verified_via_ls_remote_same_and_moved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_sha = 'a' * 40
    new_sha = 'f' * 40
    repos = {
        'unchanged_repo': _shared_repo_stanza(
            pkg_name='pkg_unchanged', source_url='https://github.com/org/unchanged.git',
        ),
        'moved_repo': _shared_repo_stanza(
            pkg_name='pkg_moved', source_url='https://github.com/org/moved.git',
        ),
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    expired = datetime.datetime.now() - datetime.timedelta(hours=3)
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    for pkg in ('pkg_unchanged', 'pkg_moved'):
        neighbor.release_resources[pkg] = {
            'version': '1.0.0-1',
            'package.xml': _pkg_xml(pkg, '1.0.0'),
            'README.md': None,
            'CHANGELOG.rst': None,
        }
    neighbor.source_repo_resources['unchanged_repo'] = _make_source_repo_cache(
        old_sha, pkg_name='pkg_unchanged', last_update_time=expired,
    )
    neighbor.source_repo_resources['moved_repo'] = _make_source_repo_cache(
        old_sha, pkg_name='pkg_moved', last_update_time=expired,
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    def fake_git_command(*args, **kwargs):
        if args == ('ls-remote', 'https://github.com/org/unchanged.git', 'main'):
            return {'returncode': 0, 'output': f'{old_sha}\trefs/heads/main'}
        if args == ('ls-remote', 'https://github.com/org/moved.git', 'main'):
            return {'returncode': 0, 'output': f'{new_sha}\trefs/heads/main'}
        raise AssertionError(f'Unexpected git command: {args}')

    source_map = {
        ('git', 'https://github.com/org/moved.git', 'main'):
            _make_source_repo_cache(new_sha, pkg_name='pkg_moved', readme='# Updated!'),
    }
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, source_map)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.side_effect = fake_git_command
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert rel_calls == []
    # Only moved_repo was fetched remotely; unchanged_repo was verified by ls-remote and copied locally
    assert [c[0] for c in src_calls] == ['moved_repo']
    cache_b = caches['distro_b']
    assert cache_b.source_repo_resources['unchanged_repo'].ref() == old_sha
    # Verified via ls-remote just now -> _last_update_time refreshed
    assert (datetime.datetime.now() - cache_b.source_repo_resources['unchanged_repo'].last_update_time()).total_seconds() < 60
    assert cache_b.source_repo_resources['moved_repo'].ref() == new_sha
    assert cache_b.source_repo_resources['moved_repo']['pkg_moved']['README.md'] == '# Updated!'


def test_pinned_commit_hash_copies_from_branch_neighbor_without_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = '1234567890abcdef' * 2 + '12345678'
    repos_a = {'repo_a': _shared_repo_stanza(source_version='main')}
    repos_b = {'repo_b_renamed': _shared_repo_stanza(source_version=sha)}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    very_old = datetime.datetime.now() - datetime.timedelta(days=30)
    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos_a},
        config=CacheConfig.extended(),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': None,
        'CHANGELOG.rst': None,
    }
    neighbor.source_repo_resources['repo_a'] = _make_source_repo_cache(sha, last_update_time=very_old)
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert rel_calls == []
    assert src_calls == []
    mock_git.return_value.command.assert_not_called()
    assert caches['distro_b'].source_repo_resources['repo_b_renamed'].ref() == sha
    assert caches['distro_b'].distribution_file.source_packages['shared_pkg'].repository_name == 'repo_b_renamed'


def test_different_branch_not_copied_unless_remote_hash_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha_a = 'a' * 40
    sha_b = 'b' * 40
    repos_a = {'shared_repo': _shared_repo_stanza(source_version='humble')}
    repos_b = {'shared_repo': _shared_repo_stanza(source_version='jazzy')}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos_a},
        config=CacheConfig.extended(),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': None,
        'CHANGELOG.rst': None,
    }
    neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha_a, readme='# Humble branch', last_update_time=datetime.datetime.now(),
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor.get_data(version=3)))

    # Case 1: distro_b has no local cache -> different branch must NOT copy and must NOT call ls-remote
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'jazzy'):
            _make_source_repo_cache(sha_b, readme='# Jazzy branch'),
    }
    fake_rel, fake_src, _, src_calls = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert len(src_calls) == 1
    mock_git.return_value.command.assert_not_called()
    assert caches['distro_b'].source_repo_resources['shared_repo'].ref() == sha_b

    # Case 2: distro_b has an expired old cache; update_distribution runs ls-remote and discovers
    # that branch 'jazzy' actually points to sha_a (same commit as distro_a's 'humble' branch!)
    old_b = DistributionCache(
        'distro_b',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos_b},
        config=CacheConfig.extended(),
    )
    old_b.release_resources['shared_pkg'] = copy.deepcopy(neighbor.release_resources['shared_pkg'])
    old_b.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        '0' * 40, last_update_time=datetime.datetime.now() - datetime.timedelta(hours=3),
    )
    (tmp_path / 'distro_b-extended-cache.yaml').write_text(yaml.safe_dump(old_b.get_data(version=3)))

    fake_rel2, fake_src2, _, src_calls2 = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel2]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src2]), \
         patch('rosdistro.distribution_cache.Git') as mock_git2:
        mock_git2.return_value.command.return_value = {
            'returncode': 0,
            'output': f'{sha_a}\trefs/heads/jazzy',
        }
        caches2 = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    mock_git2.return_value.command.assert_called_once_with(
        'ls-remote', 'https://github.com/org/shared.git', 'jazzy'
    )
    assert src_calls2 == []
    assert caches2['distro_b'].source_repo_resources['shared_repo'].ref() == sha_a


def test_known_remote_ref_prevents_copying_stale_fresh_ttl_neighbor(tmp_path, monkeypatch):
    """If distro_b's update_distribution ran ls-remote and observed remote moved to new_sha,
    it MUST NOT copy old_sha from a neighbor whose local cache is still within min_update_delta!
    """
    monkeypatch.chdir(tmp_path)
    old_sha = 'a' * 40
    new_sha = 'b' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # distro_a on disk is only 5 minutes old (within TTL), still at old_sha
    neighbor_a = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    neighbor_a.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': None,
        'CHANGELOG.rst': None,
    }
    neighbor_a.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        old_sha, last_update_time=datetime.datetime.now() - datetime.timedelta(minutes=5),
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(neighbor_a.get_data(version=3)))

    # distro_b on disk is 3 hours old (expired), triggers ls-remote which returns new_sha
    old_b = DistributionCache(
        'distro_b',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    old_b.release_resources['shared_pkg'] = copy.deepcopy(neighbor_a.release_resources['shared_pkg'])
    old_b.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        old_sha, last_update_time=datetime.datetime.now() - datetime.timedelta(hours=3),
    )
    (tmp_path / 'distro_b-extended-cache.yaml').write_text(yaml.safe_dump(old_b.get_data(version=3)))

    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache(new_sha, readme='# New Commit Content'),
    }
    fake_rel, fake_src, _, src_calls = _make_fake_providers({}, source_map)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {
            'returncode': 0,
            'output': f'{new_sha}\trefs/heads/main',
        }
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert len(src_calls) == 1
    assert caches['distro_b'].source_repo_resources['shared_repo'].ref() == new_sha
    assert caches['distro_b'].source_repo_resources['shared_repo']['shared_pkg']['README.md'] == '# New Commit Content'


def test_shared_known_remote_refs_avoids_duplicate_ls_remote_across_distros(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_sha = 'a' * 40
    new_sha = 'b' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    expired = datetime.datetime.now() - datetime.timedelta(hours=3)
    for d in ('distro_a', 'distro_b'):
        c = DistributionCache(
            d,
            distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
            config=CacheConfig.extended(),
        )
        c.release_resources['shared_pkg'] = {
            'version': '1.0.0-1',
            'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
            'README.md': None,
            'CHANGELOG.rst': None,
        }
        c.source_repo_resources['shared_repo'] = _make_source_repo_cache(old_sha, last_update_time=expired)
        (tmp_path / f'{d}-extended-cache.yaml').write_text(yaml.safe_dump(c.get_data(version=3)))

    # Remote moved to new_sha: distro_a runs ls-remote once and crawls new_sha;
    # distro_b reuses known_remote_refs (no 2nd ls-remote!) and copies new_sha from distro_a (no 2nd crawl!)
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache(new_sha, readme='# New Content'),
    }
    fake_rel, fake_src, _, src_calls = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {
            'returncode': 0,
            'output': f'{new_sha}\trefs/heads/main',
        }
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'], preclean=False, config='extended',
        )

    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://github.com/org/shared.git', 'main'
    )
    assert len(src_calls) == 1
    assert caches['distro_a'].source_repo_resources['shared_repo'].ref() == new_sha
    assert caches['distro_b'].source_repo_resources['shared_repo'].ref() == new_sha


def test_source_config_compatibility_superset_projection_and_strict_rejections(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = 'c' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # 1. Superset neighbor (extended) projected to subset target
    ext_neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    ext_neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': '# Release README',
        'CHANGELOG.rst': 'Release Changelog',
    }
    ext_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, tags=['ros2'], last_update_time=datetime.datetime.now(),
    )

    subset_cfg = CacheConfig({
        'release_package_resources': {'package.xml': True},
        'source_package_resources': {'package.xml': True, 'README.md': True},
        'source_repo_metadata': {'stars': True},
    })
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    index = get_index(path_to_url(index_path))
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        cache_b = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config=subset_cfg, neighbor_caches={'distro_a': ext_neighbor},
        )

    assert rel_calls == []
    assert src_calls == []
    assert set(cache_b.release_resources['shared_pkg'].keys()) == {'version', 'package.xml'}
    src_b = cache_b.source_repo_resources['shared_repo']
    assert src_b.stars() == 42
    assert src_b.description() is None
    assert '_description' not in src_b._data
    assert '_tags' not in src_b._data
    assert '_contributing' not in src_b._data
    assert '_readme' not in src_b._data
    assert set(src_b['shared_pkg'].keys()) == {'package_path', 'package.xml', 'README.md'}

    # 2. Subset neighbor MUST be rejected by extended target (missing metadata & resources)
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache(sha, readme='# Re-crawled extended'),
    }
    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# Re-fetched release README',
    }
    fake_rel2, fake_src2, rel_calls2, src_calls2 = _make_fake_providers(release_map, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel2]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src2]):
        cache_b_ext = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config='extended', neighbor_caches={'distro_a': cache_b},
        )

    # package.xml copied, README.md & CHANGELOG.rst fetched, source repo re-crawled
    assert {(c[3], c[4]) for c in rel_calls2} == {('shared_pkg', 'README.md'), ('shared_pkg', 'CHANGELOG.rst')}
    assert len(src_calls2) == 1
    assert cache_b_ext.source_repo_resources['shared_repo']['shared_pkg']['README.md'] == '# Re-crawled extended'

    # 3. Metadata max_lines mismatch (e.g. readme max_lines 50 vs 100) MUST reject source copy
    meta_50_neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig({
            'source_package_resources': {'package.xml': True},
            'source_repo_metadata': {'readme': {'enabled': True, 'max_lines': 50}},
        }),
    )
    meta_50_neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
    }
    meta_50_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, include_assets=False, last_update_time=datetime.datetime.now(),
    )
    fake_rel3, fake_src3, _, src_calls3 = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel3]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src3]):
        generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config={
                'source_package_resources': {'package.xml': True},
                'source_repo_metadata': {'readme': {'enabled': True, 'max_lines': 100}},
            },
            neighbor_caches={'distro_a': meta_50_neighbor},
        )
    assert len(src_calls3) == 1

    # 4. Incomplete source entry (missing required asset list despite config claiming enabled) MUST reject
    incomplete_neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    incomplete_neighbor.release_resources['shared_pkg'] = copy.deepcopy(ext_neighbor.release_resources['shared_pkg'])
    incomplete_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, include_assets=False, last_update_time=datetime.datetime.now(),
    )
    fake_rel4, fake_src4, _, src_calls4 = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel4]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src4]):
        generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config='extended', neighbor_caches={'distro_a': incomplete_neighbor},
        )
    assert len(src_calls4) == 1


def test_doc_only_fallback_and_renamed_repo_cross_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = 'e' * 40
    repos_a = {
        'doc_only_name': {
            'doc': {
                'type': 'git',
                'url': 'https://github.com/org/doc_repo.git',
                'version': 'main',
            },
        },
    }
    repos_b = {
        'src_renamed': {
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/doc_repo.git',
                'version': 'main',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    source_map = {
        ('git', 'https://github.com/org/doc_repo.git', 'main'):
            _make_source_repo_cache(sha, pkg_name='doc_pkg'),
    }
    fake_rel, fake_src, _, src_calls = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'], preclean=False, config='extended',
        )

    assert len(src_calls) == 1
    assert 'src_renamed' in caches['distro_b'].source_repo_resources
    assert caches['distro_b'].source_repo_resources['src_renamed'].ref() == sha
    assert caches['distro_b'].distribution_file.source_packages['doc_pkg'].repository_name == 'src_renamed'


def test_tar_source_repo_copied_when_fresh_and_rejected_when_expired(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tar_url = 'https://example.com/archive/my_pkg-1.0.tar.gz'
    repos = {
        'tar_repo': {
            'source': {
                'type': 'tar',
                'url': tar_url,
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})
    index = get_index(path_to_url(index_path))
    cfg = CacheConfig({'source_package_resources': {'package.xml': True}})

    # Fresh tar neighbor -> copied locally without remote fetch
    fresh_neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=cfg,
    )
    fresh_neighbor.source_repo_resources['tar_repo'] = _make_source_repo_cache(
        None, pkg_name='tar_pkg', include_assets=False,
        readme=None, changelog=None, stars=None, description=None,
        last_commit_time=None, contributing=None, root_readme=None,
        last_update_time=datetime.datetime.now() - datetime.timedelta(minutes=5),
    )
    fake_rel, fake_src, _, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        cache_b = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config=cfg, neighbor_caches={'distro_a': fresh_neighbor},
        )
    assert src_calls == []
    mock_git.return_value.command.assert_not_called()
    assert 'tar_pkg' in cache_b.source_repo_resources['tar_repo']

    # Expired tar neighbor -> cannot verify via git ls-remote, MUST fetch remotely
    expired_neighbor = copy.deepcopy(fresh_neighbor)
    expired_neighbor.source_repo_resources['tar_repo'].set_last_update_time(
        datetime.datetime.now() - datetime.timedelta(hours=3)
    )
    source_map = {
        ('tar', tar_url, None): _make_source_repo_cache(
            None, pkg_name='tar_pkg', pkg_version='2.0.0', include_assets=False,
            readme=None, changelog=None, stars=None, description=None,
            last_commit_time=None, contributing=None, root_readme=None,
        ),
    }
    fake_rel2, fake_src2, _, src_calls2 = _make_fake_providers({}, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel2]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src2]), \
         patch('rosdistro.distribution_cache.Git') as mock_git2:
        cache_b2 = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config=cfg, neighbor_caches={'distro_a': expired_neighbor},
        )
    assert len(src_calls2) == 1
    mock_git2.return_value.command.assert_not_called()
    assert '<version>2.0.0</version>' in cache_b2.source_repo_resources['tar_repo']['tar_pkg']['package.xml']


def test_corrupt_local_neighbor_skipped_and_v2_neighbor_supplies_only_package_xml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {'shared_repo': _shared_repo_stanza(source_url=None)}
    index_path = _write_index_and_distros(
        tmp_path, {'broken_dist': repos, 'v2_dist': repos, 'target_dist': repos}
    )

    # Corrupt extended cache file for broken_dist
    (tmp_path / 'broken_dist-extended-cache.yaml').write_text('::not valid yaml::: [')

    # Valid Format 2 minimal cache file for v2_dist
    v2_data = {
        'type': 'cache',
        'version': 2,
        'name': 'v2_dist',
        'distribution_file': {'type': 'distribution', 'version': 2, 'repositories': repos},
        'release_package_xmls': {'shared_pkg': _pkg_xml('shared_pkg', '1.0.0')},
        'source_repo_package_xmls': {},
    }
    (tmp_path / 'v2_dist-cache.yaml').write_text(yaml.safe_dump(v2_data))

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# Remote README',
    }
    fake_rel, fake_src, rel_calls, _ = _make_fake_providers(release_map, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['target_dist'], preclean=False,
            config={'release_package_resources': {'package.xml': True, 'README.md': True}},
        )

    # package.xml copied from v2_dist; README.md fetched remotely; broken_dist did not break build
    assert {(c[3], c[4]) for c in rel_calls} == {('shared_pkg', 'README.md')}
    assert '<name>shared_pkg</name>' in caches['target_dist'].release_resources['shared_pkg']['package.xml']
    assert caches['target_dist'].release_resources['shared_pkg']['README.md'] == '# Remote README'


def test_prefers_fresh_neighbor_over_expired_neighbor_without_ls_remote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha_old = '1' * 40
    sha_fresh = '2' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(
        tmp_path, {'old_dist': repos, 'fresh_dist': repos, 'target_dist': repos}
    )

    old_neighbor = DistributionCache(
        'old_dist',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    old_neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': None,
        'CHANGELOG.rst': None,
    }
    old_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha_old, last_update_time=datetime.datetime.now() - datetime.timedelta(hours=5),
    )

    fresh_neighbor = DistributionCache(
        'fresh_dist',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    fresh_neighbor.release_resources['shared_pkg'] = copy.deepcopy(old_neighbor.release_resources['shared_pkg'])
    fresh_neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha_fresh, last_update_time=datetime.datetime.now() - datetime.timedelta(minutes=5),
    )

    index = get_index(path_to_url(index_path))
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        # Pass fresh_dist first, old_dist second (so old_dist is checked first in reverse order!)
        # Even when old_dist is checked first, Pass 1 must continue to fresh_dist and NOT call ls-remote!
        cache = generate_distribution_cache(
            index, 'target_dist', preclean=False, ignore_local=True,
            config='extended',
            neighbor_caches={'fresh_dist': fresh_neighbor, 'old_dist': old_neighbor},
        )

    assert rel_calls == []
    assert src_calls == []
    mock_git.return_value.command.assert_not_called()
    assert cache.source_repo_resources['shared_repo'].ref() == sha_fresh


def test_min_update_delta_zero_disables_ttl_copy_and_requires_ls_remote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = '9' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    neighbor.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': None,
        'CHANGELOG.rst': None,
    }
    neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, last_update_time=datetime.datetime.now(),
    )

    cfg_zero_ttl = CacheConfig.extended().to_dict()
    cfg_zero_ttl['min_update_delta'] = 0

    index = get_index(path_to_url(index_path))
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {
            'returncode': 0,
            'output': f'{sha}\trefs/heads/main',
        }
        cache_b = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config=cfg_zero_ttl, neighbor_caches={'distro_a': neighbor},
        )

    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://github.com/org/shared.git', 'main'
    )
    assert rel_calls == []
    assert src_calls == []
    assert cache_b.source_repo_resources['shared_repo'].ref() == sha


def test_release_unversioned_or_corrupt_neighbor_entry_strictly_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repos = {
        'repo_unversioned': _shared_repo_stanza(pkg_name='pkg_unver', source_url=None),
        'repo_corrupt': _shared_repo_stanza(pkg_name='pkg_corrupt', source_url=None),
    }
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})
    index = get_index(path_to_url(index_path))

    neighbor = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    # Missing 'version' field -> must be rejected
    neighbor.release_resources['pkg_unver'] = {
        'package.xml': _pkg_xml('pkg_unver', '1.0.0'),
        'README.md': '# Unversioned README',
    }
    # Corrupt package.xml (wrong version inside XML) -> must be rejected
    neighbor.release_resources['pkg_corrupt'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('pkg_corrupt', '9.9.9'),
        'README.md': '# Corrupt pkg README',
    }

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_unver/1.0.0-1', 'package.xml'):
            _pkg_xml('pkg_unver', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_unver/1.0.0-1', 'README.md'):
            '# Authoritative unver README',
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/pkg_corrupt/1.0.0-1', 'package.xml'):
            _pkg_xml('pkg_corrupt', '1.0.0'),
    }
    fake_rel, fake_src, rel_calls, _ = _make_fake_providers(release_map, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        cache_b = generate_distribution_cache(
            index, 'distro_b', preclean=False, ignore_local=True,
            config={'release_package_resources': {'package.xml': True, 'README.md': True}},
            neighbor_caches={'distro_a': neighbor},
        )

    called_pkg_files = {(c[3], c[4]) for c in rel_calls}
    assert ('pkg_unver', 'package.xml') in called_pkg_files
    assert ('pkg_unver', 'README.md') in called_pkg_files
    assert ('pkg_corrupt', 'package.xml') in called_pkg_files
    assert cache_b.release_resources['pkg_unver']['README.md'] == '# Authoritative unver README'
    assert '<version>1.0.0</version>' in cache_b.release_resources['pkg_corrupt']['package.xml']


def test_source_package_resource_max_lines_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = '7' * 40
    long_readme = '\n'.join(f'Line {i}' for i in range(1, 121))
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})
    index = get_index(path_to_url(index_path))

    for neighbor_max, target_max in ((50, 100), (100, 50), (100, None)):
        neighbor_cfg = CacheConfig({
            'source_package_resources': {'package.xml': True, 'README.md': {'max_lines': neighbor_max}},
        })
        target_cfg = CacheConfig({
            'source_package_resources': {'package.xml': True, 'README.md': {'max_lines': target_max}},
        })
        neighbor = DistributionCache(
            'distro_a',
            distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
            config=neighbor_cfg,
        )
        neighbor.release_resources['shared_pkg'] = {
            'version': '1.0.0-1',
            'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        }
        neighbor.source_repo_resources['shared_repo'] = _make_source_repo_cache(
            sha, readme=sanitize_and_truncate_docs(long_readme, neighbor_max),
            changelog=None, stars=None, description=None, tags=None,
            last_commit_time=None, contributing=None, root_readme=None,
            include_assets=False, last_update_time=datetime.datetime.now(),
        )
        source_map = {
            ('git', 'https://github.com/org/shared.git', 'main'): _make_source_repo_cache(
                sha, readme=long_readme, changelog=None, stars=None, description=None,
                tags=None, last_commit_time=None, contributing=None, root_readme=None,
                include_assets=False,
            ),
        }
        fake_rel, fake_src, _, src_calls = _make_fake_providers({}, source_map)
        with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
             patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
            cache_b = generate_distribution_cache(
                index, 'distro_b', preclean=False, ignore_local=True,
                config=target_cfg, neighbor_caches={'distro_a': neighbor},
            )
        assert len(src_calls) == 1
        assert cache_b.source_repo_resources['shared_repo']['shared_pkg']['README.md'] == (
            sanitize_and_truncate_docs(long_readme, target_max)
        )


def test_cli_build_cache_reuses_neighbors_unless_preclean(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main

    monkeypatch.chdir(tmp_path)
    sha = '8' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    release_map = {
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'package.xml'):
            _pkg_xml('shared_pkg', '1.0.0'),
        ('git', 'https://github.com/org/shared-release.git', 'release/shared/shared_pkg/1.0.0-1', 'README.md'):
            '# CLI README',
    }
    source_map = {
        ('git', 'https://github.com/org/shared.git', 'main'):
            _make_source_repo_cache(sha, tags=['ros2']),
    }

    # 1. Without --preclean: distro_b copies from distro_a built in same CLI call
    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers(release_map, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        rc = main([index_path, 'distro_a', 'distro_b', '--extended'])
    assert not rc
    assert [c[0] for c in rel_calls] == ['distro_a', 'distro_a', 'distro_a']
    assert len(src_calls) == 1
    assert (tmp_path / 'distro_a-extended-cache.yaml').exists()
    assert (tmp_path / 'distro_b-extended-cache.yaml').exists()

    # 2. With --preclean: even with both local cache files on disk, BOTH distros fetch remotely
    fake_rel2, fake_src2, rel_calls2, src_calls2 = _make_fake_providers(release_map, source_map)
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel2]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src2]):
        rc2 = main([index_path, 'distro_a', 'distro_b', '--extended', '--preclean'])
    assert not rc2
    assert [c[0] for c in rel_calls2] == [
        'distro_a', 'distro_a', 'distro_a', 'distro_b', 'distro_b', 'distro_b',
    ]
    assert len(src_calls2) == 2


def test_upgrade_from_minimal_v2_copies_missing_extended_resources_from_neighbor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sha = '5' * 40
    repos = {'shared_repo': _shared_repo_stanza()}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # distro_b has only a Format 2 minimal cache (package.xml only) on disk
    v2_b = {
        'type': 'cache',
        'version': 2,
        'name': 'distro_b',
        'distribution_file': {'type': 'distribution', 'version': 2, 'repositories': repos},
        'release_package_xmls': {'shared_pkg': _pkg_xml('shared_pkg', '1.0.0')},
        'source_repo_package_xmls': {},
    }
    (tmp_path / 'distro_b-cache.yaml').write_text(yaml.safe_dump(v2_b))

    # distro_a has a full Format 3 extended cache on disk
    ext_a = DistributionCache(
        'distro_a',
        distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
        config=CacheConfig.extended(),
    )
    ext_a.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
        'README.md': '# Extended Neighbor README',
        'CHANGELOG.rst': 'Extended Neighbor Changelog',
    }
    ext_a.source_repo_resources['shared_repo'] = _make_source_repo_cache(
        sha, last_update_time=datetime.datetime.now() - datetime.timedelta(minutes=5),
    )
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(yaml.safe_dump(ext_a.get_data(version=3)))

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_b'], preclean=False, config='extended',
        )

    assert rel_calls == []
    assert src_calls == []
    cache_b = caches['distro_b']
    assert cache_b.release_resources['shared_pkg']['README.md'] == '# Extended Neighbor README'
    assert cache_b.release_resources['shared_pkg']['CHANGELOG.rst'] == 'Extended Neighbor Changelog'
    assert cache_b.source_repo_resources['shared_repo'].ref() == sha


def test_multi_distro_update_loads_each_local_cache_file_at_most_once(tmp_path, monkeypatch):
    """When updating multiple distros side by side, neither already-processed distros nor future
    distros peeked at as neighbors should be loaded from disk more than once.
    """
    from rosdistro import distribution_cache_generator as dcg

    monkeypatch.chdir(tmp_path)
    now = datetime.datetime.now() - datetime.timedelta(minutes=5)
    sha_a, sha_b, sha_c = 'a' * 40, 'b' * 40, 'c' * 40

    repos = {
        'repo_a': _shared_repo_stanza(
            pkg_name='pkg_a',
            release_url='https://github.com/org/a-release.git',
            source_url='https://github.com/org/a.git',
        ),
        'repo_b': _shared_repo_stanza(
            pkg_name='pkg_b',
            release_url='https://github.com/org/b-release.git',
            source_url='https://github.com/org/b.git',
        ),
        'repo_c': _shared_repo_stanza(
            pkg_name='pkg_c',
            release_url='https://github.com/org/c-release.git',
            source_url='https://github.com/org/c.git',
        ),
    }
    index_path = _write_index_and_distros(
        tmp_path, {'distro_a': repos, 'distro_b': repos, 'distro_c': repos}
    )

    # Each distro on disk currently has only 1 of the 3 repos cached, so:
    # - distro_a must peek into future distros (distro_b and distro_c)
    # - distro_b and distro_c must reuse already-processed distro_a AND must NOT reload their own files!
    for dist_name, own_repo, own_pkg, own_sha in (
        ('distro_a', 'repo_a', 'pkg_a', sha_a),
        ('distro_b', 'repo_b', 'pkg_b', sha_b),
        ('distro_c', 'repo_c', 'pkg_c', sha_c),
    ):
        c = DistributionCache(
            dist_name,
            distribution_file_data={'type': 'distribution', 'version': 2, 'repositories': repos},
            config=CacheConfig.extended(),
        )
        c.release_resources[own_pkg] = {
            'version': '1.0.0-1',
            'package.xml': _pkg_xml(own_pkg, '1.0.0'),
            'README.md': f'# {own_pkg} README',
            'CHANGELOG.rst': None,
        }
        c.source_repo_resources[own_repo] = _make_source_repo_cache(
            own_sha, pkg_name=own_pkg, tags=[dist_name], last_update_time=now,
        )
        (tmp_path / f'{dist_name}-extended-cache.yaml').write_text(yaml.safe_dump(c.get_data(version=3)))

    loaded_dists = []
    real_loader = dcg._load_local_cache_file

    def tracking_loader(dist_name, path=None):
        loaded_dists.append(dist_name)
        return real_loader(dist_name, path=path)

    fake_rel, fake_src, rel_calls, src_calls = _make_fake_providers({}, {})
    with patch.object(dcg, '_load_local_cache_file', side_effect=tracking_loader), \
         patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b', 'distro_c'],
            preclean=False, config='extended',
        )

    assert rel_calls == []
    assert src_calls == []
    mock_git.return_value.command.assert_not_called()

    # Each distro's local cache file was loaded from disk AT MOST ONCE!
    assert loaded_dists.count('distro_a') == 1
    assert loaded_dists.count('distro_b') == 1
    assert loaded_dists.count('distro_c') == 1

    # Every distro now has all 3 release packages and all 3 source repos
    for dist_name in ('distro_a', 'distro_b', 'distro_c'):
        c = caches[dist_name]
        assert set(c.release_resources.keys()) == {'pkg_a', 'pkg_b', 'pkg_c'}
        assert c.source_repo_resources['repo_a'].ref() == sha_a
        assert c.source_repo_resources['repo_b'].ref() == sha_b
        assert c.source_repo_resources['repo_c'].ref() == sha_c

    # Verify deep-copy isolation even though distro_b and distro_c were handed over in-place
    caches['distro_b'].source_repo_resources['repo_b'].tags().append('only_b')
    assert 'only_b' not in caches['distro_a'].source_repo_resources['repo_b'].tags()
    assert 'only_b' not in caches['distro_c'].source_repo_resources['repo_b'].tags()



