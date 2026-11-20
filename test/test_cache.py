import copy
import os
import tempfile

import pytest
import yaml

from rosdistro import (
    CacheConfig,
    DEFAULT_EXTENDED_CONFIG,
    DistributionCache,
    MINIMAL_CACHE_CONFIG,
    ResourceNotCrawledError,
    get_cached_distribution,
    get_distribution_cache,
    get_index,
    get_release_cache,
)
from rosdistro.loader import load_url
from rosdistro.release_cache import ReleaseCache
from rosdistro.source_repository_cache import SourceRepositoryCache

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def _load_foo_distribution_data():
    url = path_to_url(os.path.join(FILES_DIR, 'foo', 'distribution.yaml'))
    return yaml.safe_load(load_url(url))


def test_get_release_cache():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    i = get_index(url)
    dist_cache = get_distribution_cache(i, 'foo')
    assert isinstance(dist_cache, DistributionCache)
    assert 'catkin' in dist_cache.release_package_xmls
    assert 'catkin' in dist_cache.release_resources

    rel_cache = get_release_cache(i, 'foo')
    assert isinstance(rel_cache, ReleaseCache)
    assert 'catkin' in rel_cache.package_xmls
    assert 'catkin' in rel_cache.release_package_xmls


def test_cache_config_normalization_and_presets():
    minimal = CacheConfig.minimal()
    assert minimal == MINIMAL_CACHE_CONFIG
    assert minimal.is_release_resource_enabled('package.xml') is True
    assert minimal.is_release_resource_enabled('README.md') is False
    assert minimal.is_source_resource_enabled('package.xml') is False
    assert minimal.is_source_metadata_enabled('stars') is False
    assert minimal.has_any_source_enabled() is False
    assert minimal.fallback_src_to_doc is True

    extended = CacheConfig.extended()
    assert extended == DEFAULT_EXTENDED_CONFIG
    assert extended.is_release_resource_enabled('package.xml') is True
    assert extended.is_release_resource_enabled('README.md') is True
    assert extended.get_release_resource_max_lines('README.md') == 100
    assert extended.is_source_resource_enabled('package.xml') is True
    assert extended.is_source_resource_enabled('CHANGELOG.rst') is True
    assert extended.is_source_resource_enabled('launch_files') is True
    assert extended.is_source_resource_enabled('msg_files') is True
    assert extended.is_source_resource_enabled('srv_files') is True
    assert extended.is_source_resource_enabled('action_files') is True
    assert extended.get_source_resource_max_lines('launch_files') is None
    assert extended.get_source_resource_max_lines('msg_files') is None
    assert extended.get_source_resource_max_lines('srv_files') is None
    assert extended.get_source_resource_max_lines('action_files') is None
    assert extended.is_source_metadata_enabled('stars') is True
    assert extended.is_source_metadata_enabled('description') is True
    assert extended.is_source_metadata_enabled('tags') is True
    assert extended.is_source_metadata_enabled('last_commit_time') is True
    assert extended.is_source_metadata_enabled('contributing') is True
    assert extended.is_source_metadata_enabled('readme') is True
    assert extended.has_any_source_enabled() is True
    assert extended.fallback_src_to_doc is True

    # Custom config with shorthand booleans, ints, and custom files/metadata
    custom_dict = {
        'release_package_resources': {
            'README.md': {'max_lines': 40},
            'LICENSE': True,
        },
        'source_package_resources': {
            'README.md': 25,
        },
        'source_repo_metadata': {
            'stars': True,
            'custom_metric': {'enabled': True},
        },
        'fallback_src_to_doc': False,
        'min_update_delta': 1800,
        'failure_retry_delta': 7200,
        'yaml_alias_min_length': 200,
        'limit': 5,
    }
    with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as tf:
        yaml.safe_dump(custom_dict, tf)
        cfg_path = tf.name
    try:
        cfg = CacheConfig.from_input(cfg_path)
    finally:
        os.unlink(cfg_path)

    assert cfg.is_release_resource_enabled('package.xml') is True
    assert cfg.is_release_resource_enabled('README.md') is True
    assert cfg.get_release_resource_max_lines('README.md') == 40
    assert cfg.is_release_resource_enabled('LICENSE') is True
    assert cfg.is_release_resource_enabled('CHANGELOG.rst') is False
    assert cfg.is_source_resource_enabled('package.xml') is True
    assert cfg.is_source_resource_enabled('README.md') is True
    assert cfg.get_source_resource_max_lines('README.md') == 25
    assert cfg.is_source_metadata_enabled('stars') is True
    assert cfg.is_source_metadata_enabled('custom_metric') is True
    assert cfg.is_source_metadata_enabled('description') is False
    assert cfg.fallback_src_to_doc is False
    assert cfg.min_update_delta == 1800
    assert cfg.failure_retry_delta == 7200
    assert cfg.yaml_alias_min_length == 200
    assert cfg.limit == 5
    # When failure_retry_delta is omitted, it defaults to min_update_delta
    assert CacheConfig({'min_update_delta': 900}).failure_retry_delta == 900

    # Unknown top-level config key raises ValueError in strict mode, but allowed in from_cache_data
    with pytest.raises(ValueError, match='Unknown cache configuration keys'):
        CacheConfig({'unknown_top_level_key': True})
    forward_cfg = CacheConfig.from_cache_data({'config': {'future_key': 123, 'limit': 10}})
    assert forward_cfg.limit == 10

    # min_update_delta override and validation
    assert CacheConfig.from_input('extended', min_update_delta=86400).min_update_delta == 86400
    assert CacheConfig.from_input({'min_update_delta': 1800}, min_update_delta=0).min_update_delta == 0
    assert CacheConfig.from_input({'min_update_delta': 1800}, min_update_delta=0).failure_retry_delta == 0
    with pytest.raises(ValueError, match='min_update_delta must be >= 0'):
        CacheConfig({'min_update_delta': -1})
    with pytest.raises(ValueError, match='min_update_delta must be >= 0'):
        CacheConfig.from_input('extended', min_update_delta=-5)


def test_distribution_cache_dual_v2_and_v3_export():
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    cache = DistributionCache('foo', distribution_file_data=dist_data, config=CacheConfig.extended())
    cache.release_resources['bar_repo'] = {
        'version': '0.1.2-0',
        'package.xml': bar_xml,
        'README.md': '# bar_repo',
        'CHANGELOG.rst': None,
    }
    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.set_stars(12)
    repo_cache.set_description('Bar repository')
    repo_cache.set_tags(['ros', 'bar'])
    repo_cache.set_last_commit_time('2026-03-01T12:00:00Z')
    repo_cache.set_contributing('# Contributing')
    repo_cache.set_readme('# Repo Root README')
    repo_cache.add('bar_repo', 'src/bar_repo', bar_xml, 'package.xml')
    repo_cache.add('bar_repo', 'src/bar_repo', '# bar_repo source', 'README.md')
    repo_cache.add('bar_repo', 'src/bar_repo', ['launch/demo.launch.py'], 'launch_files')
    repo_cache.add('bar_repo', 'src/bar_repo', ['msg/Bar.msg'], 'msg_files')
    repo_cache.add('bar_repo', 'src/bar_repo', ['srv/GetBar.srv'], 'srv_files')
    repo_cache.add('bar_repo', 'src/bar_repo', ['action/DoBar.action'], 'action_files')
    cache.source_repo_resources['bar_repo'] = repo_cache

    # Backwards compatible release_package_xmls live view and v2 tuple indexing on SourceRepositoryCache
    assert isinstance(cache.release_package_xmls, dict)
    assert cache.release_package_xmls == {'bar_repo': bar_xml}
    assert yaml.safe_load(yaml.safe_dump(cache.release_package_xmls)) == {'bar_repo': bar_xml}
    view = cache.release_package_xmls
    view.update({'baz_pkg1': '<package><name>baz_pkg1</name></package>'})
    assert cache.release_resources['baz_pkg1']['package.xml'] == '<package><name>baz_pkg1</name></package>'
    assert view.pop('baz_pkg1') == '<package><name>baz_pkg1</name></package>'
    assert 'baz_pkg1' not in cache.release_resources
    assert repo_cache['bar_repo'][0] == 'src/bar_repo'
    assert repo_cache['bar_repo']['package_path'] == 'src/bar_repo'
    assert repo_cache['bar_repo'][1] == bar_xml
    assert cache.source_repo_package_xmls['bar_repo'].stars() == 12
    assert cache.source_repo_package_xmls['bar_repo'].description() == 'Bar repository'
    assert cache.source_repo_package_xmls['bar_repo'].tags() == ['ros', 'bar']
    assert cache.source_repo_package_xmls['bar_repo'].last_commit_time() == '2026-03-01T12:00:00Z'
    assert cache.source_repo_package_xmls['bar_repo'].contributing() == '# Contributing'
    assert cache.source_repo_package_xmls['bar_repo'].readme() == '# Repo Root README'
    assert cache.source_repo_package_xmls['bar_repo'].launch_files('bar_repo') == ['launch/demo.launch.py']
    assert cache.source_repo_package_xmls['bar_repo'].msg_files('bar_repo') == ['msg/Bar.msg']
    assert cache.source_repo_package_xmls['bar_repo'].srv_files('bar_repo') == ['srv/GetBar.srv']
    assert cache.source_repo_package_xmls['bar_repo'].action_files('bar_repo') == ['action/DoBar.action']

    # Export v3 (extended)
    v3_data = cache.get_data(version=3)
    assert v3_data['version'] == 3
    assert v3_data['config'] == CacheConfig.extended().to_dict()
    assert v3_data['release_resources']['bar_repo']['README.md'] == '# bar_repo'
    assert v3_data['release_resources']['bar_repo']['CHANGELOG.rst'] is None
    assert v3_data['source_repo_resources']['bar_repo']['_stars'] == 12
    assert v3_data['source_repo_resources']['bar_repo']['_last_commit_time'] == '2026-03-01T12:00:00Z'
    assert v3_data['source_repo_resources']['bar_repo']['_contributing'] == '# Contributing'
    assert v3_data['source_repo_resources']['bar_repo']['_readme'] == '# Repo Root README'
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['package_path'] == 'src/bar_repo'
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['launch_files'] == ['launch/demo.launch.py']
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['msg_files'] == ['msg/Bar.msg']
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['srv_files'] == ['srv/GetBar.srv']
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['action_files'] == ['action/DoBar.action']

    # Export v2 (minimal legacy)
    v2_data = cache.get_legacy_data()
    assert v2_data['version'] == 2
    assert 'config' not in v2_data
    assert 'release_resources' not in v2_data
    assert v2_data['release_package_xmls'] == {'bar_repo': bar_xml}
    assert v2_data['source_repo_package_xmls'] == {}

    # Export v2 with source included
    v2_with_src = cache.get_legacy_data(include_source=True)
    assert v2_with_src['source_repo_package_xmls']['bar_repo'] == {
        '_ref': 'deadbeef',
        'bar_repo': ['src/bar_repo', bar_xml],
    }


def test_not_crawled_vs_not_found():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    index = get_index(url)
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package format="2"><name>bar_repo</name><version>0.1.2</version><description>d</description><maintainer email="a@b.c">a</maintainer><license>BSD</license></package>'

    # 1. Minimal cache: README.md and source resources were NOT crawled
    min_cache = DistributionCache('foo', distribution_file_data=dist_data, config=CacheConfig.minimal())
    min_cache.release_resources['bar_repo'] = {'package.xml': bar_xml}
    min_dist = get_cached_distribution(index, 'foo', cache=min_cache, allow_lazy_load=False)

    assert min_dist.is_release_resource_crawled('package.xml') is True
    assert min_dist.is_release_resource_crawled('README.md') is False
    assert min_dist.is_source_resource_crawled('package.xml') is False
    assert min_dist.is_source_metadata_crawled('stars') is False

    # Default require_crawled=False returns None
    assert min_dist.get_release_readme('bar_repo') is None
    assert min_dist.get_source_package_xml('bar_repo') is None
    assert min_dist.get_source_repo_metadata('bar_repo', 'stars') is None

    # With require_crawled=True, raises ResourceNotCrawledError
    with pytest.raises(ResourceNotCrawledError):
        min_dist.get_release_readme('bar_repo', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        min_dist.get_source_package_xml('bar_repo', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        min_dist.get_source_repo_metadata('bar_repo', 'stars', require_crawled=True)

    # 2. Extended cache: README.md and CHANGELOG.rst WERE crawled, but CHANGELOG.rst was not found (None)
    ext_cache = DistributionCache('foo', distribution_file_data=dist_data, config=CacheConfig.extended())
    ext_cache.release_resources['bar_repo'] = {
        'package.xml': bar_xml,
        'README.md': '# bar_repo',
        'CHANGELOG.rst': None,
    }
    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.set_stars(5)
    repo_cache.add('bar_repo', '', bar_xml, 'package.xml')
    ext_cache.source_repo_resources['bar_repo'] = repo_cache
    ext_dist = get_cached_distribution(index, 'foo', cache=ext_cache, allow_lazy_load=False)

    assert ext_dist.is_release_resource_crawled('CHANGELOG.rst') is True
    # Because CHANGELOG.rst WAS crawled, require_crawled=True returns None (not found) without raising!
    assert ext_dist.get_release_changelog('bar_repo', require_crawled=True) is None
    assert ext_dist.get_source_readme('bar_repo', require_crawled=True) is None
    assert ext_dist.get_source_repo_metadata('bar_repo', 'stars', require_crawled=True) == 5
    # Description was enabled in config (crawled), but repo had None -> returns None without raising
    assert ext_dist.get_source_repo_metadata('bar_repo', 'description', require_crawled=True) is None
    # Unconfigured resource/metadata still raises ResourceNotCrawledError
    with pytest.raises(ResourceNotCrawledError):
        ext_dist.get_release_resource('bar_repo', 'CMakeLists.txt', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        ext_dist.get_source_repo_metadata('bar_repo', 'unconfigured_key', require_crawled=True)

    # Package or repo skipped due to --limit (not present in release_resources / source_repo_resources)
    assert ext_dist.is_release_resource_crawled('README.md', pkg_name='baz_pkg1') is False
    assert ext_dist.is_source_resource_crawled('README.md', pkg_name='baz_pkg1') is False
    assert ext_dist.is_source_metadata_crawled('stars', repo_name='baz-repo') is False
    with pytest.raises(ResourceNotCrawledError):
        ext_dist.get_release_readme('baz_pkg1', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        ext_dist.get_source_readme('baz_pkg1', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        ext_dist.get_source_repo_metadata('baz-repo', 'stars', require_crawled=True)

    # Doc-only repository skipped due to --limit is also recognized as not crawled when fallback_src_to_doc=True
    dist_with_doc_only = copy.deepcopy(dist_data)
    dist_with_doc_only['repositories']['doc_only_repo'] = {
        'doc': {
            'type': 'git',
            'url': 'https://github.com/example-test/doc_only_repo.git',
            'version': 'main',
        },
    }
    doc_cache = DistributionCache('foo', distribution_file_data=dist_with_doc_only, config=CacheConfig.extended())
    doc_dist = get_cached_distribution(index, 'foo', cache=doc_cache, allow_lazy_load=False)
    assert doc_dist.is_source_resource_crawled('README.md', pkg_name='doc_only_repo') is False
    assert doc_dist.is_source_metadata_crawled('stars', repo_name='doc_only_repo') is False
    with pytest.raises(ResourceNotCrawledError):
        doc_dist.get_source_readme('doc_only_repo', require_crawled=True)
    with pytest.raises(ResourceNotCrawledError):
        doc_dist.get_source_repo_metadata('doc_only_repo', 'stars', require_crawled=True)

    # When fallback_src_to_doc=False, a doc-only repository is intentionally not crawled (treated like release-only)
    no_fallback_cfg = CacheConfig.extended().to_dict()
    no_fallback_cfg['fallback_src_to_doc'] = False
    doc_cache.apply_config(no_fallback_cfg)
    assert doc_dist.is_source_resource_crawled('README.md', pkg_name='doc_only_repo') is True
    assert doc_dist.is_source_metadata_crawled('stars', repo_name='doc_only_repo') is True
    assert doc_dist.get_source_readme('doc_only_repo', require_crawled=True) is None
    assert doc_dist.get_source_repo_metadata('doc_only_repo', 'stars', require_crawled=True) is None


def test_package_path_posix_normalization():
    from rosdistro.distribution_cache_generator import CacheYamlDumper

    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    index = get_index(url)
    dist_data = _load_foo_distribution_data()
    bar_xml = (
        '<?xml version="1.0" ?><package format="2"><name>bar_repo</name><version>0.1.2</version>'
        '<description>bar package</description><maintainer email="a@b.c">a</maintainer>'
        '<license>BSD</license></package>'
    )
    root_xml = (
        '<?xml version="1.0" ?><package format="2"><name>root_pkg</name><version>0.1.2</version>'
        '<description>root package</description><maintainer email="a@b.c">a</maintainer>'
        '<license>BSD</license></package>'
    )

    repo_cache = SourceRepositoryCache.from_ref('deadbeef')
    repo_cache.add('bar_repo', r'core\bar_repo', bar_xml, 'package.xml')
    repo_cache.add('bar_repo', r'core\bar_repo', [r'launch\sub\demo.launch.py'], 'launch_files')
    repo_cache.add('posix_pkg', 'nested/posix_pkg', bar_xml, 'package.xml')
    repo_cache.add('root_pkg', '', root_xml, 'package.xml')

    # Both backslash and forward-slash inputs are recorded and returned as POSIX '/'
    assert repo_cache['bar_repo']['package_path'] == 'core/bar_repo'
    assert repo_cache['bar_repo'].get('package_path') == 'core/bar_repo'
    assert repo_cache['bar_repo'][0] == 'core/bar_repo'
    assert repo_cache.launch_files('bar_repo') == ['launch/sub/demo.launch.py']
    assert repo_cache['posix_pkg']['package_path'] == 'nested/posix_pkg'
    assert repo_cache['root_pkg']['package_path'] == ''
    assert repo_cache['root_pkg'][0] == ''

    # Direct mutation and v2/v3 dict loading also normalize backslashes to POSIX '/'
    repo_cache['bar_repo']['package_path'] = r'updated\bar_repo'
    assert repo_cache['bar_repo']['package_path'] == 'updated/bar_repo'
    repo_cache['bar_repo'].update({'package_path': r'core\bar_repo'})
    assert repo_cache['bar_repo']['package_path'] == 'core/bar_repo'

    v2_loaded = SourceRepositoryCache({
        '_ref': 'deadbeef',
        'p_posix': ['a/b', bar_xml],
        'p_win': [r'c\d', bar_xml],
    })
    assert v2_loaded['p_posix']['package_path'] == 'a/b'
    assert v2_loaded['p_win']['package_path'] == 'c/d'
    assert v2_loaded['p_win'][0] == 'c/d'
    assert v2_loaded.get_data(version=2)['p_win'][0] == 'c/d'
    assert v2_loaded.get_data(version=3)['p_win']['package_path'] == 'c/d'

    cache = DistributionCache('foo', distribution_file_data=dist_data, config=CacheConfig.extended())
    cache.source_repo_resources['bar_repo'] = repo_cache
    cache.distribution_file.source_packages = cache.get_source_packages()

    dist = get_cached_distribution(index, 'foo', cache=cache, allow_lazy_load=False)
    assert dist.get_source_resource('bar_repo', 'package_path') == 'core/bar_repo'

    v3_data = cache.get_data(version=3)
    assert v3_data['source_repo_resources']['bar_repo']['bar_repo']['package_path'] == 'core/bar_repo'
    v2_data = cache.get_legacy_data(include_source=True)
    assert v2_data['source_repo_package_xmls']['bar_repo']['bar_repo'] == ['core/bar_repo', bar_xml]

    reloaded_v3 = yaml.safe_load(yaml.dump(v3_data, Dumper=CacheYamlDumper))
    assert reloaded_v3['source_repo_resources']['bar_repo']['bar_repo']['package_path'] == 'core/bar_repo'


def test_load_cache_yaml_legacy_python2_string_tags():
    from rosdistro import load_cache_yaml

    legacy_yaml = (
        'type: cache\n'
        'version: 2\n'
        'name: hydro\n'
        'release_package_xmls:\n'
        '  ar_sys: !!python/str "<?xml version=\\"1.0\\"?><package><name>ar_sys</name></package>"\n'
        '  uni_pkg: !!python/unicode "<?xml version=\\"1.0\\"?><package><name>uni_pkg</name>'
        '<maintainer email=\\"a@b.c\\">Ren\\u00e9</maintainer></package>"\n'
    )

    for payload in (legacy_yaml, legacy_yaml.encode('utf-8')):
        loaded = load_cache_yaml(payload)
        assert loaded['name'] == 'hydro'
        assert type(loaded['release_package_xmls']['ar_sys']) is str
        assert '<name>ar_sys</name>' in loaded['release_package_xmls']['ar_sys']
        assert type(loaded['release_package_xmls']['uni_pkg']) is str
        assert 'René' in loaded['release_package_xmls']['uni_pkg']

    # Unsafe Python object tags must still be strictly rejected
    for unsafe_yaml in (
        'bad: !!python/object/apply:os.system ["echo unsafe"]',
        'bad: !!python/name:os.system',
    ):
        with pytest.raises(yaml.constructor.ConstructorError):
            load_cache_yaml(unsafe_yaml)

    # PyYAML's global SafeLoader must remain unmodified
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load('x: !!python/str "should still fail"')


def test_legacy_python2_tagged_cache_loads_in_get_cache_and_build_cache(tmp_path, monkeypatch):
    import gzip
    import json
    from unittest.mock import patch

    from rosdistro.cli.rosdistro_build_cache import main as build_cache_main

    dist_dir = tmp_path / 'hydro'
    dist_dir.mkdir()
    dist_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'ar_sys': {
                'release': {
                    'packages': ['ar_sys'],
                    'tags': {'release': 'release/hydro/{package}/{version}'},
                    'url': 'https://github.com/Sahloul/arsys_release.git',
                    'version': '1.0.4-0',
                },
            },
        },
    }
    (dist_dir / 'distribution.yaml').write_text(yaml.safe_dump(dist_data))

    ar_sys_xml = (
        '<?xml version="1.0"?><package><name>ar_sys</name><version>1.0.4</version>'
        '<description>ar_sys</description><maintainer email="a@example.com">René</maintainer>'
        '<license>BSD</license></package>'
    )
    legacy_cache_yaml = (
        'type: cache\n'
        'version: 2\n'
        'name: hydro\n'
        'distribution_file:\n'
        '  type: distribution\n'
        '  version: 2\n'
        '  repositories:\n'
        '    ar_sys:\n'
        '      release:\n'
        '        packages: [ar_sys]\n'
        '        tags:\n'
        '          release: release/hydro/{package}/{version}\n'
        '        url: https://github.com/Sahloul/arsys_release.git\n'
        '        version: 1.0.4-0\n'
        'release_package_xmls:\n'
        '  ar_sys: !!python/str ' + json.dumps(ar_sys_xml, ensure_ascii=False) + '\n'
    )
    remote_cache_gz = tmp_path / 'remote_hydro-cache.yaml.gz'
    with gzip.open(remote_cache_gz, 'wb') as f:
        f.write(legacy_cache_yaml.encode('utf-8'))

    index_path = tmp_path / 'index.yaml'
    index_path.write_text(yaml.safe_dump({
        'type': 'index',
        'version': 4,
        'distributions': {
            'hydro': {
                'distribution': ['hydro/distribution.yaml'],
                'distribution_cache': path_to_url(str(remote_cache_gz)),
            },
        },
    }))

    index = get_index(path_to_url(str(index_path)))
    dist_cache = get_distribution_cache(index, 'hydro')
    assert 'ar_sys' in dist_cache.release_package_xmls
    assert '<name>ar_sys</name>' in dist_cache.release_package_xmls['ar_sys']

    # Also verify deprecated get_release_cache() loads legacy !!python/str caches (dist format v1)
    legacy_v1_cache_gz = tmp_path / 'remote_v1_hydro-cache.yaml.gz'
    with gzip.open(legacy_v1_cache_gz, 'wb') as f:
        f.write(legacy_cache_yaml.replace('  version: 2\n', '  version: 1\n', 1).encode('utf-8'))
    v1_index_path = tmp_path / 'index_v1.yaml'
    v1_index_path.write_text(yaml.safe_dump({
        'type': 'index',
        'version': 3,
        'distributions': {
            'hydro': {
                'distribution': ['hydro/distribution.yaml'],
                'distribution_cache': path_to_url(str(legacy_v1_cache_gz)),
            },
        },
    }))
    rel_cache = get_release_cache(get_index(path_to_url(str(v1_index_path))), 'hydro')
    assert isinstance(rel_cache, ReleaseCache)
    assert 'ar_sys' in rel_cache.release_package_xmls

    # Even if the upstream release repo has been deleted, rosdistro_build_cache should
    # reuse the historical package.xml from the legacy cache without fetching it, and
    # write clean Python 3 SafeDumper output loadable by plain yaml.safe_load().
    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    monkeypatch.chdir(out_dir)

    fetched_package_xml = []

    def dead_repo_provider(dist_name, repo, pkg_name, filepath='package.xml'):
        if filepath == 'package.xml':
            fetched_package_xml.append(pkg_name)
        raise RuntimeError('Repository not found')

    with patch('rosdistro.Distribution.default_manifest_providers', [dead_repo_provider]):
        rc = build_cache_main([str(index_path), 'hydro', '--strict'])

    assert not rc
    assert fetched_package_xml == []
    regenerated_text = (out_dir / 'hydro-cache.yaml').read_text()
    regenerated = yaml.safe_load(regenerated_text)
    assert 'ar_sys' in regenerated['release_package_xmls']
    assert '!!python/' not in regenerated_text

    # Also verify local legacy cache file loading via _load_local_cache_file()
    local_dir = tmp_path / 'local_out'
    local_dir.mkdir()
    with gzip.open(local_dir / 'hydro-cache.yaml.gz', 'wb') as f:
        f.write(legacy_cache_yaml.encode('utf-8'))
    monkeypatch.chdir(local_dir)
    with patch('rosdistro.Distribution.default_manifest_providers', [dead_repo_provider]):
        rc_local = build_cache_main([str(index_path), 'hydro', '--strict'])
    assert not rc_local
    assert fetched_package_xml == []

