import datetime
import json
import os
from unittest.mock import Mock, patch

from jsonschema import validate
import pytest
import yaml

from rosdistro.distribution_cache import DistributionCache
from rosdistro.distribution_cache_generator import CacheYamlDumper, generate_distribution_cache
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

    cache.update_distribution([dist_file_data])

    mock_git.return_value.command.assert_called_once_with(
        'ls-remote', 'https://example.com/my_repo.git', 'main'
    )
    assert 'my_repo' in cache.source_repo_resources


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
    cache.source_repo_resources['my_repo'] = repo_cache

    tar_repo_cache = SourceRepositoryCache.from_ref(None)
    tar_repo_cache.add('tar_pkg', 'tar_pkg-1.0', '<package format="2"><name>tar_pkg</name></package>', 'package.xml')
    cache.source_repo_resources['tar_repo'] = tar_repo_cache

    # Validate in-memory cache.get_data() against schema
    in_memory_data = cache.get_data()
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
        # Updating distribution immediately after reload should skip ls-remote due to TTL
        reloaded_cache.update_distribution([dist_file_data])
        mock_git.return_value.command.assert_not_called()
        assert 'my_repo' in reloaded_cache.source_repo_resources
        assert 'tar_repo' not in reloaded_cache.source_repo_resources


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


@patch('rosdistro.cli.rosdistro_build_cache.generate_distribution_caches')
def test_cli_build_cache_writes_both_legacy_and_extended_files(mock_gen_caches, tmp_path, monkeypatch):
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
    mock_gen_caches.return_value = {'rolling': cache}

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

    # 1. Matching remote commit hash on doc_repository keeps the cache entry
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

    # 2. Different remote commit hash on doc_repository invalidates the cache entry
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




