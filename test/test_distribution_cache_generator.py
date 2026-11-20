import datetime
import gzip
import json
import os
from unittest.mock import Mock, patch

from jsonschema import validate
import pytest
import yaml

from rosdistro.distribution_cache import DistributionCache
from rosdistro.distribution_cache_generator import (
    CacheYamlDumper,
    generate_distribution_cache,
    generate_distribution_caches,
)
from rosdistro.source_repository_cache import SourceRepositoryCache


SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'rosdistro', 'rosdistro_cache_3.schema.json'
)


def _load_schema():
    with open(SCHEMA_PATH, 'r') as f:
        return json.load(f)


def _sample_dist_file_data():
    return {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'my_repo': {
                'source': {
                    'type': 'git',
                    'url': 'https://example.com/my_repo.git',
                    'version': 'main',
                },
                'release': {
                    'packages': ['my_pkg'],
                    'tags': {'release': 'release/rolling/{package}/{version}'},
                    'url': 'https://example.com/my_repo-release.git',
                    'version': '1.0.0-1',
                },
            }
        },
    }


@patch('rosdistro.distribution_cache_generator._get_cached_distribution')
def test_generate_distribution_cache_version_invalidation(mock_get_cached_distribution):
    mock_index = Mock()
    mock_dist = Mock()
    mock_cache = Mock()
    
    mock_pkg_name = "test_pkg"
    mock_dist.release_packages = {mock_pkg_name: Mock(repository_name="test_repo")}
    
    mock_repo = Mock()
    mock_repo.release_repository.version = "1.2.3-1"
    mock_dist.repositories = {"test_repo": mock_repo}
    
    mock_dist.get_release_package_xml.return_value = '<package format="2"><name>test_pkg</name><version>1.2.3</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
    mock_dist.get_release_resource.return_value = "dummy"
    
    mock_cache.release_resources = {
        mock_pkg_name: {
            'version': '1.0.0-1',
            'package.xml': '<package format="2"><name>test_pkg</name><version>1.0.0</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
        }
    }
    
    mock_get_cached_distribution.return_value = (mock_dist, mock_cache)
    
    generate_distribution_cache(mock_index, "distro_name")
    
    # The cache should have been invalidated and the new version added
    assert mock_cache.release_resources[mock_pkg_name] == {'version': '1.2.3-1'}

@patch('rosdistro.distribution_cache_generator._get_cached_distribution')
def test_generate_distribution_cache_version_match(mock_get_cached_distribution):
    mock_index = Mock()
    mock_dist = Mock()
    mock_cache = Mock()
    
    mock_pkg_name = "test_pkg"
    mock_dist.release_packages = {mock_pkg_name: Mock(repository_name="test_repo")}
    
    mock_repo = Mock()
    mock_repo.release_repository.version = "1.2.3-1"
    mock_dist.repositories = {"test_repo": mock_repo}
    
    mock_dist.get_release_package_xml.return_value = '<package format="2"><name>test_pkg</name><version>1.2.3</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
    mock_dist.get_release_resource.return_value = "dummy"
    
    mock_cache.release_resources = {
        mock_pkg_name: {
            'version': '1.2.3-1',
            'package.xml': 'old_xml'
        }
    }
    
    mock_get_cached_distribution.return_value = (mock_dist, mock_cache)
    
    generate_distribution_cache(mock_index, "distro_name")
    
    # The cache should NOT have been cleared because versions match
    assert mock_cache.release_resources[mock_pkg_name]['version'] == '1.2.3-1'
    assert mock_cache.release_resources[mock_pkg_name]['package.xml'] == 'old_xml'


def test_source_repository_cache_last_update_time():
    repo_cache = SourceRepositoryCache.from_ref('abc1234')
    assert repo_cache.last_update_time() is None
    assert '_last_update_time' not in repo_cache.get_data()

    # Adding without incrementing update time leaves _last_update_time unset
    repo_cache.add('pkg_a', 'pkg_a', '<package/>', 'package.xml', increment_update_time=False)
    assert repo_cache.last_update_time() is None
    assert '_last_update_time' not in repo_cache.get_data()

    # Adding with increment_update_time=True sets repo-level _last_update_time as ISO string in data
    repo_cache.add('pkg_a', 'pkg_a', '<package/>', 'package.xml', increment_update_time=True)
    assert isinstance(repo_cache.last_update_time(), datetime.datetime)
    assert isinstance(repo_cache.get_data()['_last_update_time'], str)
    assert '_last_update_time' not in repo_cache['pkg_a']
    assert list(repo_cache) == ['pkg_a']

    # Re-initializing from get_data() preserves last_update_time
    reloaded = SourceRepositoryCache(dict(repo_cache.get_data()))
    assert reloaded.last_update_time() == repo_cache.last_update_time()


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_update_distribution_ttl_skip_recent(mock_git):
    dist_file_data = _sample_dist_file_data()
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    # Set update time to 10 minutes ago (well within the 1-hour TTL)
    recent_time = datetime.datetime.now() - datetime.timedelta(minutes=10)
    repo_cache.set_last_update_time(recent_time)
    cache.source_repo_resources['my_repo'] = repo_cache

    cache.update_distribution([dist_file_data])

    # git ls-remote should be skipped because entry_age < min_update_delta
    mock_git.return_value.command.assert_not_called()
    assert 'my_repo' in cache.source_repo_resources
    # ttl_skip must not slide _last_update_time forward
    assert cache.source_repo_resources['my_repo'].last_update_time() == recent_time


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_update_distribution_ttl_expired_same_hash(mock_git):
    dist_file_data = _sample_dist_file_data()
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    # Set update time to 2 hours ago (expired past the 1-hour TTL)
    expired_time = datetime.datetime.now() - datetime.timedelta(hours=2)
    repo_cache.set_last_update_time(expired_time)
    cache.source_repo_resources['my_repo'] = repo_cache

    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'deadbeef\trefs/heads/main',
    }

    before = datetime.datetime.now()
    cache.update_distribution([dist_file_data])

    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://example.com/my_repo.git', 'main'
    )
    assert 'my_repo' in cache.source_repo_resources
    # _last_update_time must be refreshed when git ls-remote confirms the ref is unchanged
    refreshed_time = cache.source_repo_resources['my_repo'].last_update_time()
    assert refreshed_time is not None
    assert refreshed_time >= before

    # A subsequent update within min_update_delta should now take ttl_skip (no second ls-remote)
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once()
    assert cache.source_repo_resources['my_repo'].last_update_time() == refreshed_time


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_update_distribution_ttl_expired_different_hash(mock_git):
    dist_file_data = _sample_dist_file_data()
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    expired_time = datetime.datetime.now() - datetime.timedelta(hours=2)
    repo_cache.set_last_update_time(expired_time)
    cache.source_repo_resources['my_repo'] = repo_cache

    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'cafebabe\trefs/heads/main',
    }

    cache.update_distribution([dist_file_data])

    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://example.com/my_repo.git', 'main'
    )
    # Cache entry should be dropped because remote hash changed
    assert 'my_repo' not in cache.source_repo_resources


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_schema_validation_and_yaml_roundtrip(mock_git):
    schema = _load_schema()
    dist_file_data = _sample_dist_file_data()
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    cache.release_resources['my_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': '<package format="2"><name>my_pkg</name></package>',
        'README.md': '# my_pkg',
        'CHANGELOG.rst': 'Changelog',
    }

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.set_stars(42)
    repo_cache.set_description('Test repository')
    repo_cache.set_tags(['ros2', 'test'])
    repo_cache.set_last_commit_time('2026-04-01T12:00:00Z')
    repo_cache.set_contributing('# Contributing Guide')
    repo_cache.set_readme('# Repo Root README')
    repo_cache.add('my_pkg', 'src/my_pkg', '<package format="2"><name>my_pkg</name></package>', 'package.xml')
    repo_cache.add('my_pkg', 'src/my_pkg', '# my_pkg', 'README.md')
    repo_cache.add('my_pkg', 'src/my_pkg', 'Changelog', 'CHANGELOG.rst')
    repo_cache.add('my_pkg', 'src/my_pkg', ['launch/my_pkg.launch.py'], 'launch_files')
    repo_cache.add('my_pkg', 'src/my_pkg', ['msg/MyMsg.msg'], 'msg_files')
    repo_cache.add('my_pkg', 'src/my_pkg', ['srv/MySrv.srv'], 'srv_files')
    repo_cache.add('my_pkg', 'src/my_pkg', ['action/MyAction.action'], 'action_files')
    cache.source_repo_resources['my_repo'] = repo_cache

    tar_repo_cache = SourceRepositoryCache.from_ref(None)
    tar_repo_cache.add('tar_pkg', 'tar_pkg-1.0', '<package format="2"><name>tar_pkg</name></package>', 'package.xml')
    cache.source_repo_resources['tar_repo'] = tar_repo_cache
    cache.record_source_repo_failure(
        'broken_repo',
        'https://github.com/example/broken_repo.git',
        'humble',
        error='HTTP Error 404: Not Found',
        permanent=True,
    )

    # Validate in-memory cache.get_data() against schema
    in_memory_data = cache.get_data()
    assert 'broken_repo' in in_memory_data['failed_source_repos']
    assert 'failed_source_repos' not in cache.get_legacy_data(include_source=True)
    validate(instance=in_memory_data, schema=schema)

    # Validate after yaml.safe_dump / yaml.safe_load and CacheYamlDumper round-trip
    for dumped in (
        yaml.safe_dump(in_memory_data),
        yaml.dump(in_memory_data, Dumper=CacheYamlDumper),
    ):
        loaded_data = yaml.safe_load(dumped)
        validate(instance=loaded_data, schema=schema)

        reloaded_cache = DistributionCache('rolling', data=loaded_data)
        assert isinstance(
            reloaded_cache.source_repo_resources['my_repo'].last_update_time(),
            datetime.datetime,
        )
        assert 'broken_repo' in reloaded_cache.failed_source_repos
        # Updating distribution immediately after reload should skip ls-remote due to TTL
        # and prune obsolete failed_source_repos entries not present in dist_file
        reloaded_cache.update_distribution([dist_file_data])
        mock_git.return_value.command.assert_not_called()
        assert 'my_repo' in reloaded_cache.source_repo_resources
        assert 'tar_repo' not in reloaded_cache.source_repo_resources
        assert 'broken_repo' not in reloaded_cache.failed_source_repos


def test_distribution_cache_v2_migration():
    schema = _load_schema()
    dist_file_data = _sample_dist_file_data()
    v2_data = {
        'type': 'cache',
        'version': 2,
        'name': 'rolling',
        'distribution_file': dist_file_data,
        'release_package_xmls': {
            'my_pkg': '<package><name>my_pkg</name></package>',
        },
        'source_repo_package_xmls': {
            'my_repo': {
                '_ref': 'deadbeef',
                'my_pkg': ['src/my_pkg', '<package><name>my_pkg</name></package>'],
            }
        },
    }

    cache = DistributionCache('rolling', data=v2_data)
    assert cache.version == 3
    assert cache.release_resources['my_pkg']['package.xml'] == '<package><name>my_pkg</name></package>'
    assert cache.source_repo_resources['my_repo']['my_pkg'] == {
        'package_path': 'src/my_pkg',
        'package.xml': '<package><name>my_pkg</name></package>',
    }
    validate(instance=cache.get_data(), schema=schema)


@patch('rosdistro.distribution_cache_generator.generate_distribution_cache')
@patch('rosdistro.distribution_cache_generator.get_index')
def test_cli_build_cache_writes_both_legacy_and_extended_files(
    mock_get_index, mock_gen_cache, tmp_path, monkeypatch
):
    from rosdistro.cli.rosdistro_build_cache import main
    from rosdistro.distribution_cache import CacheConfig

    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    dist_file_data = _sample_dist_file_data()

    config_file = tmp_path / 'cache_config.yaml'
    config_file.write_text(yaml.safe_dump({
        'release_package_resources': {
            'package.xml': True,
            'README.md': {'enabled': True, 'max_lines': 50},
            'LICENSE': True,
        },
        'source_package_resources': {
            'package.xml': True,
            'README.md': {'enabled': True, 'max_lines': 50},
        },
        'source_repo_metadata': {
            'stars': True,
        },
    }))

    cache = DistributionCache('rolling', distribution_file_data=dist_file_data, config=CacheConfig.from_file(str(config_file)))
    pkg_xml = '<package format="2"><name>my_pkg</name><version>1.0.0</version><description>d</description><maintainer email="a@b.c">a</maintainer><license>BSD</license></package>'
    cache.release_resources['my_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': pkg_xml,
        'README.md': '# my_pkg',
        'LICENSE': None,
    }
    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.set_stars(99)
    repo_cache.add('my_pkg', 'src/my_pkg', pkg_xml, 'package.xml')
    cache.source_repo_resources['my_repo'] = repo_cache
    mock_get_index.return_value = Mock(distributions={'rolling': {}})
    mock_gen_cache.return_value = cache

    # Without --config or --extended, only the legacy Format 2 cache files are written
    rc_legacy_only = main(['index.yaml', 'rolling'])
    assert not rc_legacy_only
    assert (tmp_path / 'rolling-cache.yaml').exists()
    assert (tmp_path / 'rolling-cache.yaml.gz').exists()
    assert not (tmp_path / 'rolling-extended-cache.yaml').exists()
    assert not (tmp_path / 'rolling-extended-cache.yaml.gz').exists()

    # With --config (or --extended), both legacy Format 2 and extended Format 3 cache files are written
    rc = main(['index.yaml', 'rolling', '--config', str(config_file)])
    assert not rc

    assert (tmp_path / 'rolling-cache.yaml').exists()
    assert (tmp_path / 'rolling-cache.yaml.gz').exists()
    assert (tmp_path / 'rolling-extended-cache.yaml').exists()
    assert (tmp_path / 'rolling-extended-cache.yaml.gz').exists()

    legacy_loaded = yaml.safe_load((tmp_path / 'rolling-cache.yaml').read_text())
    assert legacy_loaded['version'] == 2
    assert legacy_loaded['release_package_xmls'] == {'my_pkg': pkg_xml}
    assert legacy_loaded['source_repo_package_xmls'] == {}

    ext_loaded = yaml.safe_load((tmp_path / 'rolling-extended-cache.yaml').read_text())
    assert ext_loaded['version'] == 3
    validate(instance=ext_loaded, schema=schema)
    assert ext_loaded['config']['release_package_resources']['LICENSE']['enabled'] is True
    assert ext_loaded['release_resources']['my_pkg']['LICENSE'] is None


def test_warm_cache_apply_config_upgrades_and_v2_migration_preserves_versions():
    from rosdistro.distribution_cache import CacheConfig

    dist_file_data = _sample_dist_file_data()
    pkg_xml = '<package format="2"><name>my_pkg</name><version>1.0.0</version><description>d</description><maintainer email="a@b.c">a</maintainer><license>BSD</license></package>'

    # 1. Migrating from v2 sets 'version' from distribution_file so warm rebuild does not drop it
    v2_input = {
        'type': 'cache',
        'version': 2,
        'name': 'rolling',
        'distribution_file': copy.deepcopy(dist_file_data) if 'copy' in globals() else dict(dist_file_data),
        'release_package_xmls': {'my_pkg': pkg_xml},
        'source_repo_package_xmls': {},
    }
    cache = DistributionCache('rolling', data=v2_input)
    assert 'release_resources' not in v2_input  # caller input not mutated
    assert cache.release_resources['my_pkg']['version'] == '1.0.0-1'
    assert cache.release_resources['my_pkg']['package.xml'] == pkg_xml

    # 2. Upgrading config to extended keeps package.xml, and resizing max_lines invalidates old README.md
    cache.apply_config(CacheConfig({'release_package_resources': {'README.md': {'max_lines': 15}}}))
    cache.release_resources['my_pkg']['README.md'] = 'short readme'
    assert cache.release_resources['my_pkg']['package.xml'] == pkg_xml

    # Changing max_lines from 15 to 50 drops cached 'README.md' so it will be re-fetched at 50 lines
    cache.apply_config(CacheConfig({'release_package_resources': {'README.md': {'max_lines': 50}}}))
    assert cache.release_resources['my_pkg']['package.xml'] == pkg_xml
    assert 'README.md' not in cache.release_resources['my_pkg']


@patch('rosdistro.distribution_cache_generator._get_cached_distribution')
def test_generate_distribution_cache_crawls_doc_only_repo(mock_get_cached_distribution):
    mock_index = Mock()
    mock_dist = Mock()
    mock_cache = Mock()
    mock_dist.release_packages = {}

    src_repo = Mock()
    src_repo.source_repository = Mock()
    src_repo.doc_repository = None

    doc_only_repo = Mock()
    doc_only_repo.source_repository = None
    doc_only_repo.doc_repository = Mock()

    release_only_repo = Mock()
    release_only_repo.source_repository = None
    release_only_repo.doc_repository = None

    mock_dist.repositories = {
        'a_src_repo': src_repo,
        'b_doc_only_repo': doc_only_repo,
        'c_release_only_repo': release_only_repo,
    }
    mock_get_cached_distribution.return_value = (mock_dist, mock_cache)

    generate_distribution_cache(mock_index, 'rolling', include_source=True)

    crawled_repos = [call.args[0] for call in mock_dist.get_source_repo_resources.call_args_list]
    assert crawled_repos == ['a_src_repo', 'b_doc_only_repo']

    # When fallback_src_to_doc=False, only repositories with a 'source' entry are crawled
    mock_dist.get_source_repo_resources.reset_mock()
    generate_distribution_cache(
        mock_index, 'rolling', include_source=True, config={'fallback_src_to_doc': False}
    )
    crawled_repos_no_fallback = [call.args[0] for call in mock_dist.get_source_repo_resources.call_args_list]
    assert crawled_repos_no_fallback == ['a_src_repo']



@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_update_distribution_doc_only_repo(mock_git):
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'soar_ros': {
                'doc': {
                    'type': 'git',
                    'url': 'https://example.com/soar_ros.git',
                    'version': 'main',
                },
            },
        },
    }
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('soar_ros', '', '<package><name>soar_ros</name></package>', 'package.xml')
    expired_time = datetime.datetime.now() - datetime.timedelta(hours=2)
    repo_cache.set_last_update_time(expired_time)
    cache.source_repo_resources['soar_ros'] = repo_cache

    # 1. Matching remote commit hash on doc_repository keeps the cache entry and refreshes TTL
    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'deadbeef\trefs/heads/main',
    }
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://example.com/soar_ros.git', 'main'
    )
    assert 'soar_ros' in cache.source_repo_resources
    assert 'soar_ros' in cache.distribution_file.source_packages
    assert cache.source_repo_resources['soar_ros'].last_update_time() > expired_time

    # 2. Different remote commit hash on doc_repository invalidates the cache entry once TTL expires again
    cache.source_repo_resources['soar_ros'].set_last_update_time(expired_time)
    mock_git.return_value.command.reset_mock()
    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'cafebabe\trefs/heads/main',
    }
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://example.com/soar_ros.git', 'main'
    )
    assert 'soar_ros' not in cache.source_repo_resources

    # 3. Disabling fallback_src_to_doc drops doc-only entries on apply_config and update_distribution
    cache.source_repo_resources['soar_ros'] = repo_cache
    cache.apply_config({'source_package_resources': {'package.xml': True}, 'fallback_src_to_doc': False})
    assert 'soar_ros' not in cache.source_repo_resources

    cache.source_repo_resources['soar_ros'] = repo_cache
    mock_git.return_value.command.reset_mock()
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_not_called()
    assert 'soar_ros' not in cache.source_repo_resources


def _make_pkg_xml(name, version='1.0.0'):
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


def _write_multi_distro_index(tmp_path, distros):
    index_data = {
        'type': 'index',
        'version': 4,
        'distributions': {},
    }
    for dist_name, pkg_name in distros.items():
        dist_dir = tmp_path / dist_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / 'distribution.yaml').write_text(yaml.safe_dump({
            'type': 'distribution',
            'version': 2,
            'repositories': {
                f'{pkg_name}_repo': {
                    'source': {
                        'type': 'git',
                        'url': f'https://github.com/org/{pkg_name}.git',
                        'version': 'main',
                    },
                    'release': {
                        'packages': [pkg_name],
                        'tags': {'release': f'release/{dist_name}/{{package}}/{{version}}'},
                        'url': f'https://github.com/org/{pkg_name}-release.git',
                        'version': '1.0.0-1',
                    },
                },
            },
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


def _make_source_cache_for_pkg(pkg_name, sha):
    rc = SourceRepositoryCache.from_ref(sha)
    rc.set_stars(5)
    rc.set_description(f'{pkg_name} repo')
    rc.set_tags(['ros2'])
    rc.set_last_commit_time('2026-04-01T12:00:00Z')
    rc.set_contributing('# Contributing')
    rc.set_readme('# Root README')
    rc.add(pkg_name, pkg_name, _make_pkg_xml(pkg_name), 'package.xml')
    rc.add(pkg_name, pkg_name, f'# {pkg_name}', 'README.md')
    rc.add(pkg_name, pkg_name, '1.0.0\n* init', 'CHANGELOG.rst')
    rc.add(pkg_name, pkg_name, [], 'launch_files')
    rc.add(pkg_name, pkg_name, [], 'msg_files')
    rc.add(pkg_name, pkg_name, [], 'srv_files')
    rc.add(pkg_name, pkg_name, [], 'action_files')
    return rc


def test_generate_distribution_caches_writes_to_output_dir_per_distro_and_reuses_on_restart(tmp_path, monkeypatch):
    from rosdistro import distribution_cache_generator as dcg

    monkeypatch.chdir(tmp_path)
    index_path = _write_multi_distro_index(
        tmp_path, {'distro_a': 'pkg_a', 'distro_b': 'pkg_b', 'distro_c': 'pkg_c'}
    )

    # 1. Default output_dir=None does not write any files to disk
    def fake_gen_only(index, dist_name, **kwargs):
        dist_data = yaml.safe_load((tmp_path / dist_name / 'distribution.yaml').read_text())
        return DistributionCache(dist_name, distribution_file_data=dist_data)

    with patch('rosdistro.distribution_cache_generator.generate_distribution_cache', side_effect=fake_gen_only):
        caches = generate_distribution_caches(index_path, dist_names=['distro_a'])
    assert list(caches.keys()) == ['distro_a']
    assert not (tmp_path / 'distro_a-cache.yaml').exists()

    # 2. Explicit output_dir writes each distro before moving to the next
    out_dir = tmp_path / 'cache_out'
    events = []
    real_write = dcg.write_distribution_cache

    def fake_gen_with_order(index, dist_name, **kwargs):
        events.append(('generate', dist_name))
        dist_data = yaml.safe_load((tmp_path / dist_name / 'distribution.yaml').read_text())
        return DistributionCache(dist_name, distribution_file_data=dist_data)

    def tracking_write(dist_name, cache, **kwargs):
        events.append(('write', dist_name))
        return real_write(dist_name, cache, **kwargs)

    with patch('rosdistro.distribution_cache_generator.generate_distribution_cache', side_effect=fake_gen_with_order), \
         patch('rosdistro.distribution_cache_generator.write_distribution_cache', side_effect=tracking_write):
        caches = generate_distribution_caches(
            index_path,
            dist_names=['distro_a', 'distro_b', 'distro_c'],
            config='extended',
            output_dir=str(out_dir),
        )

    assert list(caches.keys()) == ['distro_a', 'distro_b', 'distro_c']
    assert events == [
        ('generate', 'distro_a'),
        ('write', 'distro_a'),
        ('generate', 'distro_b'),
        ('write', 'distro_b'),
        ('generate', 'distro_c'),
        ('write', 'distro_c'),
    ]
    for dist_name in ('distro_a', 'distro_b', 'distro_c'):
        assert (out_dir / f'{dist_name}-cache.yaml').exists()
        assert (out_dir / f'{dist_name}-extended-cache.yaml').exists()
        assert not (tmp_path / f'{dist_name}-cache.yaml').exists()

    # 3. Restarting with the same custom output_dir reuses cached files from output_dir
    out_dir_2 = tmp_path / 'cache_out_restart'
    rel_calls = []
    src_calls = []

    def fake_rel(dist_name, repo, pkg_name, filepath='package.xml'):
        rel_calls.append((dist_name, pkg_name, filepath))
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def fake_src(repo, filepaths=None, metadata=None):
        pkg_name = repo.name[:-len('_repo')]
        src_calls.append(pkg_name)
        return _make_source_cache_for_pkg(pkg_name, 'a' * 40)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        generate_distribution_caches(
            index_path, dist_names=['distro_a'], config='extended', output_dir=str(out_dir_2)
        )
    assert len(rel_calls) == 3
    assert src_calls == ['pkg_a']

    rel_calls.clear()
    src_calls.clear()
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'], config='extended', output_dir=str(out_dir_2)
        )
    assert [c[0] for c in rel_calls] == ['distro_b', 'distro_b', 'distro_b']
    assert src_calls == ['pkg_b']
    mock_git.return_value.command.assert_not_called()
    assert (out_dir_2 / 'distro_b-extended-cache.yaml').exists()


def test_cli_build_cache_writes_each_distro_before_starting_next(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main
    from rosdistro import distribution_cache_generator as dcg

    monkeypatch.chdir(tmp_path)
    index_path = _write_multi_distro_index(tmp_path, {'distro_a': 'pkg_a', 'distro_b': 'pkg_b'})
    distro_a_files_seen_when_b_started = {}
    written_calls = []
    real_write = dcg.write_distribution_cache

    def tracking_write(dist_name, cache, **kwargs):
        written_calls.append(dist_name)
        return real_write(dist_name, cache, **kwargs)

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        if dist_name == 'distro_b' and not distro_a_files_seen_when_b_started:
            for fname in (
                'distro_a-cache.yaml',
                'distro_a-cache.yaml.gz',
                'distro_a-extended-cache.yaml',
                'distro_a-extended-cache.yaml.gz',
            ):
                p = tmp_path / fname
                distro_a_files_seen_when_b_started[fname] = p.exists()
            # Verify the already-written files are complete and readable before distro_b finishes
            loaded = yaml.safe_load((tmp_path / 'distro_a-extended-cache.yaml').read_text())
            assert loaded['name'] == 'distro_a'
            assert 'pkg_a' in loaded['release_resources']
            with gzip.open(tmp_path / 'distro_a-cache.yaml.gz', 'rb') as gz:
                gz_loaded = yaml.safe_load(gz.read())
            assert gz_loaded['name'] == 'distro_a'
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def fake_source_mp(repo, filepaths=None, metadata=None):
        pkg_name = repo.name[:-len('_repo')]
        return _make_source_cache_for_pkg(pkg_name, 'a' * 40 if pkg_name == 'pkg_a' else 'b' * 40)

    with patch('rosdistro.distribution_cache_generator.write_distribution_cache', side_effect=tracking_write), \
         patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]):
        rc = main([index_path, 'distro_a', 'distro_b', '--extended'])

    assert not rc
    assert distro_a_files_seen_when_b_started == {
        'distro_a-cache.yaml': True,
        'distro_a-cache.yaml.gz': True,
        'distro_a-extended-cache.yaml': True,
        'distro_a-extended-cache.yaml.gz': True,
    }
    assert written_calls == ['distro_a', 'distro_b']


def test_cli_build_cache_preserves_completed_distro_on_crash_and_reuses_on_restart(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main

    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    index_path = _write_multi_distro_index(tmp_path, {'distro_a': 'pkg_a', 'distro_b': 'pkg_b'})

    # Run 1 (cold start): distro_a completes, then process crashes while indexing distro_b
    run1_rel_calls = []
    run1_src_calls = []

    def run1_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        run1_rel_calls.append((dist_name, pkg_name, filepath))
        if dist_name == 'distro_b':
            raise KeyboardInterrupt('simulated crash during distro_b')
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def run1_source_mp(repo, filepaths=None, metadata=None):
        pkg_name = repo.name[:-len('_repo')]
        run1_src_calls.append(pkg_name)
        return _make_source_cache_for_pkg(pkg_name, '1' * 40)

    with patch('rosdistro.Distribution.default_manifest_providers', [run1_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [run1_source_mp]):
        with pytest.raises(KeyboardInterrupt, match='simulated crash during distro_b'):
            main([index_path, 'distro_a', 'distro_b', '--extended'])

    # distro_a must already be persisted to disk; distro_b must not exist
    for fname in (
        'distro_a-cache.yaml',
        'distro_a-cache.yaml.gz',
        'distro_a-extended-cache.yaml',
        'distro_a-extended-cache.yaml.gz',
    ):
        assert (tmp_path / fname).exists()
    for fname in (
        'distro_b-cache.yaml',
        'distro_b-cache.yaml.gz',
        'distro_b-extended-cache.yaml',
        'distro_b-extended-cache.yaml.gz',
    ):
        assert not (tmp_path / fname).exists()

    ext_a = yaml.safe_load((tmp_path / 'distro_a-extended-cache.yaml').read_text())
    validate(instance=ext_a, schema=schema)
    assert 'pkg_a' in ext_a['release_resources']
    assert 'pkg_a_repo' in ext_a['source_repo_resources']

    # Run 2 (restart with same command): distro_a is reused from cache with ZERO remote fetches,
    # and distro_b now completes and is written out.
    run2_rel_calls = []
    run2_src_calls = []

    def run2_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        run2_rel_calls.append((dist_name, pkg_name, filepath))
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def run2_source_mp(repo, filepaths=None, metadata=None):
        pkg_name = repo.name[:-len('_repo')]
        run2_src_calls.append(pkg_name)
        return _make_source_cache_for_pkg(pkg_name, '2' * 40)

    with patch('rosdistro.Distribution.default_manifest_providers', [run2_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [run2_source_mp]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        rc = main([index_path, 'distro_a', 'distro_b', '--extended'])

    assert not rc
    # distro_a was loaded from the locally cached files written in Run 1 -> zero network calls!
    assert [c[0] for c in run2_rel_calls] == ['distro_b', 'distro_b', 'distro_b']
    assert run2_src_calls == ['pkg_b']
    mock_git.return_value.command.assert_not_called()

    ext_b = yaml.safe_load((tmp_path / 'distro_b-extended-cache.yaml').read_text())
    validate(instance=ext_b, schema=schema)
    assert 'pkg_b' in ext_b['release_resources']
    assert 'pkg_b_repo' in ext_b['source_repo_resources']


def test_cli_build_cache_writes_completed_distros_when_another_distro_errors(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main

    monkeypatch.chdir(tmp_path)
    index_path = _write_multi_distro_index(
        tmp_path, {'distro_a': 'pkg_a', 'distro_b': 'pkg_b', 'distro_c': 'pkg_c'}
    )

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        if dist_name == 'distro_b':
            # Missing package.xml triggers RuntimeError for distro_b in --strict mode
            return None
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def fake_source_mp(repo, filepaths=None, metadata=None):
        pkg_name = repo.name[:-len('_repo')]
        return _make_source_cache_for_pkg(pkg_name, '3' * 40)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]):
        rc = main([index_path, 'distro_a', 'distro_b', 'distro_c', '--extended', '--strict'])

    assert rc == 1
    for ok_dist in ('distro_a', 'distro_c'):
        assert (tmp_path / f'{ok_dist}-cache.yaml').exists()
        assert (tmp_path / f'{ok_dist}-cache.yaml.gz').exists()
        assert (tmp_path / f'{ok_dist}-extended-cache.yaml').exists()
        assert (tmp_path / f'{ok_dist}-extended-cache.yaml.gz').exists()
    assert not (tmp_path / 'distro_b-cache.yaml').exists()
    assert not (tmp_path / 'distro_b-extended-cache.yaml').exists()


def test_cli_build_cache_tolerates_release_package_errors_by_default(tmp_path, monkeypatch, caplog):
    import logging
    from rosdistro.cli.rosdistro_build_cache import main

    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    dist_dir = tmp_path / 'hydro'
    dist_dir.mkdir(parents=True, exist_ok=True)
    dist_yaml = dist_dir / 'distribution.yaml'
    dist_yaml.write_text(yaml.safe_dump({
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'repo_ok': {
                'release': {
                    'packages': ['pkg_ok'],
                    'tags': {'release': 'release/hydro/{package}/{version}'},
                    'url': 'https://example.com/repo_ok-release.git',
                    'version': '1.0.0-1',
                },
                'source': {
                    'type': 'git',
                    'url': 'https://example.com/repo_ok.git',
                    'version': 'main',
                },
            },
            'repo_missing': {
                'release': {
                    'packages': ['pkg_missing'],
                    'tags': {'release': 'release/hydro/{package}/{version}'},
                    'url': 'https://example.com/repo_missing-release.git',
                    'version': '1.0.0-1',
                },
            },
            'repo_invalid': {
                'release': {
                    'packages': ['pkg_invalid'],
                    'tags': {'release': 'release/hydro/{package}/{version}'},
                    'url': 'https://example.com/repo_invalid-release.git',
                    'version': '1.0.0-1',
                },
            },
            'repo_mismatch': {
                'release': {
                    'packages': ['pkg_mismatch'],
                    'tags': {'release': 'release/hydro/{package}/{version}'},
                    'url': 'https://example.com/repo_mismatch-release.git',
                    'version': '1.0.0-1',
                },
            },
        },
    }))
    index_path = tmp_path / 'index.yaml'
    index_path.write_text(yaml.safe_dump({
        'type': 'index',
        'version': 4,
        'distributions': {
            'hydro': {'distribution': ['hydro/distribution.yaml']},
        },
    }))

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        if pkg_name == 'pkg_missing':
            return None
        if pkg_name == 'pkg_invalid' and filepath == 'package.xml':
            return '<package format="2"><name>pkg_invalid</name></package>'
        if pkg_name == 'pkg_mismatch' and filepath == 'package.xml':
            return _make_pkg_xml('pkg_mismatch', version='2.0.0')
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name, version='1.0.0')
        return f'# {pkg_name} {filepath}'

    def fake_source_mp(repo, filepaths=None, metadata=None):
        return _make_source_cache_for_pkg('pkg_ok', '4' * 40)

    with caplog.at_level(logging.WARNING, logger='rosdistro'), \
         patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]):
        rc = main([str(index_path), 'hydro', '--extended'])

    # Default mode logs release package errors as non-fatal, writes the cache, and exits 0
    assert not rc
    for fname in (
        'hydro-cache.yaml',
        'hydro-cache.yaml.gz',
        'hydro-extended-cache.yaml',
        'hydro-extended-cache.yaml.gz',
    ):
        assert (tmp_path / fname).exists()

    ext = yaml.safe_load((tmp_path / 'hydro-extended-cache.yaml').read_text())
    validate(instance=ext, schema=schema)
    assert 'pkg_ok' in ext['release_resources']
    assert 'pkg_mismatch' in ext['release_resources']
    # Failed missing/invalid packages should not leave broken or empty version-only stubs
    assert 'pkg_missing' not in ext['release_resources']
    assert 'pkg_invalid' not in ext['release_resources']
    assert 'repo_ok' in ext['source_repo_resources']

    legacy = yaml.safe_load((tmp_path / 'hydro-cache.yaml').read_text())
    assert 'pkg_ok' in legacy['release_package_xmls']
    assert 'pkg_missing' not in legacy['release_package_xmls']
    assert 'pkg_invalid' not in legacy['release_package_xmls']

    log_text = caplog.text
    assert 'missing package.xml file for package "pkg_missing"' in log_text
    assert 'invalid package.xml file for package "pkg_invalid"' in log_text
    assert 'different version in package.xml (2.0.0) for package "pkg_mismatch"' in log_text


def test_cli_build_cache_fatal_distro_error_still_fails_in_default_mode(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main

    monkeypatch.chdir(tmp_path)
    index_path = _write_multi_distro_index(tmp_path, {'distro_a': 'pkg_a'})

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def fake_source_mp(repo, filepaths=None, metadata=None):
        return _make_source_cache_for_pkg('pkg_a', '5' * 40)

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]):
        rc = main([index_path, 'distro_a', 'nonexistent_distro', '--extended'])

    # Unbuildable distro (unknown distribution) still returns exit code 1, while distro_a is written
    assert rc == 1
    assert (tmp_path / 'distro_a-cache.yaml').exists()
    assert (tmp_path / 'distro_a-extended-cache.yaml').exists()
    assert not (tmp_path / 'nonexistent_distro-cache.yaml').exists()


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_refreshes_missing_known_ref_and_preserves_pinned(mock_git):
    dist_file_data = _sample_dist_file_data()
    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'deadbeef\trefs/heads/main',
    }

    # 1. Missing _last_update_time (e.g. migrated from Format 2) gets populated after ls-remote
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add(
        'my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml',
        increment_update_time=False,
    )
    assert repo_cache.last_update_time() is None
    cache.source_repo_resources['my_repo'] = repo_cache

    before = datetime.datetime.now()
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once()
    assert cache.source_repo_resources['my_repo'].last_update_time() >= before

    # Subsequent update now takes ttl_skip
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once()

    # 2. Verification via known_remote_refs also refreshes _last_update_time
    mock_git.reset_mock()
    cache2 = DistributionCache('rolling', distribution_file_data=dist_file_data)
    repo_cache2 = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache2.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    expired = datetime.datetime.now() - datetime.timedelta(hours=5)
    repo_cache2.set_last_update_time(expired)
    cache2.source_repo_resources['my_repo'] = repo_cache2

    known_refs = {('https://example.com/my_repo.git', 'main'): 'deadbeef'}
    before2 = datetime.datetime.now()
    cache2.update_distribution([dist_file_data], known_remote_refs=known_refs)
    mock_git.return_value.command.assert_not_called()
    assert cache2.source_repo_resources['my_repo'].last_update_time() >= before2

    # 3. Pinned 40-char commit hash does not touch _last_update_time
    pinned_sha = 'a' * 40
    pinned_dist = _sample_dist_file_data()
    pinned_dist['repositories']['my_repo']['source']['version'] = pinned_sha
    cache3 = DistributionCache('rolling', distribution_file_data=pinned_dist)
    repo_cache3 = SourceRepositoryCache.from_ref(pinned_sha)
    repo_cache3.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    repo_cache3.set_last_update_time(expired)
    cache3.source_repo_resources['my_repo'] = repo_cache3

    cache3.update_distribution([pinned_dist])
    mock_git.return_value.command.assert_not_called()
    assert cache3.source_repo_resources['my_repo'].last_update_time() == expired


@patch('rosdistro.distribution_cache.Git')
def test_distribution_cache_tz_aware_timestamps_and_zero_min_update_delta(mock_git):
    dist_file_data = _sample_dist_file_data()
    mock_git.return_value.command.return_value = {
        'returncode': 0,
        'output': 'deadbeef\trefs/heads/main',
    }

    # 1. Fresh timezone-aware UTC timestamp takes ttl_skip without raising TypeError
    cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    recent_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    repo_cache.set_last_update_time(recent_utc)
    cache.source_repo_resources['my_repo'] = repo_cache

    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_not_called()
    assert cache.source_repo_resources['my_repo'].last_update_time() == recent_utc

    # 2. Expired timezone-aware UTC timestamp runs ls-remote and preserves tzinfo on refresh
    expired_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    cache.source_repo_resources['my_repo'].set_last_update_time(expired_utc)
    before_utc = datetime.datetime.now(datetime.timezone.utc)
    cache.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once()
    refreshed_utc = cache.source_repo_resources['my_repo'].last_update_time()
    assert refreshed_utc.tzinfo == datetime.timezone.utc
    assert refreshed_utc >= before_utc

    # 3. Timezone-aware failed_at timestamps work with is_source_repo_failed and pruning
    cache.record_source_repo_failure(
        'my_repo',
        'https://example.com/my_repo.git',
        'main',
        error='temporary error',
        failed_at=recent_utc,
    )
    cache._session_failed_source_repos.clear()
    assert cache.is_source_repo_failed('my_repo', 'https://example.com/my_repo.git', 'main') is True
    assert cache.is_source_repo_failed(
        'my_repo', 'https://example.com/my_repo.git', 'main', now=datetime.datetime.now()
    ) is True
    del cache.source_repo_resources['my_repo']
    cache.record_source_repo_failure(
        'my_repo',
        'https://example.com/my_repo.git',
        'main',
        error='old error',
        failed_at=expired_utc,
    )
    cache._session_failed_source_repos.clear()
    cache.update_distribution([dist_file_data])
    assert 'my_repo' not in cache.failed_source_repos

    # 4. min_update_delta=0 forces ls-remote even when timestamp is fresh or slightly in the future
    mock_git.reset_mock()
    cache_zero = DistributionCache(
        'rolling', distribution_file_data=dist_file_data, config={'min_update_delta': 0}
    )
    future_repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    future_repo_cache.add('my_pkg', '', '<package><name>my_pkg</name></package>', 'package.xml')
    future_repo_cache.set_last_update_time(datetime.datetime.now() + datetime.timedelta(minutes=5))
    cache_zero.source_repo_resources['my_repo'] = future_repo_cache
    cache_zero.update_distribution([dist_file_data])
    mock_git.return_value.command.assert_called_once()


def test_cli_build_cache_min_update_delta_override(tmp_path, monkeypatch):
    from rosdistro.cli.rosdistro_build_cache import main, parse_args

    # Negative values are rejected by argparse
    with pytest.raises(SystemExit):
        parse_args(['index.yaml', '--min-update-delta', '-1'])

    monkeypatch.chdir(tmp_path)
    schema = _load_schema()
    index_path = _write_multi_distro_index(tmp_path, {'distro_a': 'pkg_a'})
    head_sha = 'a' * 40

    def fake_release_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        if filepath == 'package.xml':
            return _make_pkg_xml(pkg_name)
        return f'# {pkg_name} {filepath}'

    def fake_source_mp(repo, filepaths=None, metadata=None):
        return _make_source_cache_for_pkg('pkg_a', head_sha)

    # Run 1: populate extended cache on disk
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]):
        rc = main([index_path, 'distro_a', '--extended'])
    assert not rc

    # Backdate _last_update_time by 2 hours in the on-disk extended cache
    ext_path = tmp_path / 'distro_a-extended-cache.yaml'
    ext_data = yaml.safe_load(ext_path.read_text())
    two_hours_ago = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()
    ext_data['source_repo_resources']['pkg_a_repo']['_last_update_time'] = two_hours_ago
    ext_path.write_text(yaml.safe_dump(ext_data))

    ls_remote_result = {'returncode': 0, 'output': '%s\trefs/heads/main' % head_sha}

    # Run 2: --min-update-delta 86400 treats 2-hour-old entry as fresh -> skips ls-remote
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = ls_remote_result
        rc = main([index_path, 'distro_a', '--extended', '--min-update-delta', '86400'])
    assert not rc
    mock_git.return_value.command.assert_not_called()
    reloaded_after_2 = yaml.safe_load(ext_path.read_text())
    validate(instance=reloaded_after_2, schema=schema)
    assert reloaded_after_2['config']['min_update_delta'] == 86400
    assert reloaded_after_2['source_repo_resources']['pkg_a_repo']['_last_update_time'] == two_hours_ago

    # Run 3: --min-update-delta 0 forces ls-remote and refreshes _last_update_time on disk
    before_run3 = datetime.datetime.now()
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = ls_remote_result
        rc = main([index_path, 'distro_a', '--extended', '--min-update-delta', '0'])
    assert not rc
    mock_git.return_value.command.assert_called_once()
    reloaded_after_3 = yaml.safe_load(ext_path.read_text())
    assert reloaded_after_3['config']['min_update_delta'] == 0
    refreshed_ts = datetime.datetime.fromisoformat(
        reloaded_after_3['source_repo_resources']['pkg_a_repo']['_last_update_time']
    )
    assert refreshed_ts >= before_run3

    # Run 4: immediate hot rerun with default TTL (3600s) skips ls-remote because Run 3 refreshed timestamp
    with patch('rosdistro.Distribution.default_manifest_providers', [fake_release_mp]), \
         patch('rosdistro.Distribution.default_source_manifest_providers', [fake_source_mp]), \
         patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = ls_remote_result
        rc = main([index_path, 'distro_a', '--extended'])
    assert not rc
    mock_git.return_value.command.assert_not_called()



