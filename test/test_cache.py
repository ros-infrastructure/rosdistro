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
    assert extended.is_source_metadata_enabled('stars') is True
    assert extended.is_source_metadata_enabled('description') is True
    assert extended.is_source_metadata_enabled('tags') is True
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
    assert cfg.yaml_alias_min_length == 200
    assert cfg.limit == 5

    # Unknown top-level config key raises ValueError in strict mode, but allowed in from_cache_data
    with pytest.raises(ValueError, match='Unknown cache configuration keys'):
        CacheConfig({'unknown_top_level_key': True})
    forward_cfg = CacheConfig.from_cache_data({'config': {'future_key': 123, 'limit': 10}})
    assert forward_cfg.limit == 10


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
    repo_cache.add('bar_repo', 'src/bar_repo', bar_xml, 'package.xml')
    repo_cache.add('bar_repo', 'src/bar_repo', '# bar_repo source', 'README.md')
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
    assert repo_cache['bar_repo'][1] == bar_xml
    assert cache.source_repo_package_xmls['bar_repo'].stars() == 12
    assert cache.source_repo_package_xmls['bar_repo'].description() == 'Bar repository'
    assert cache.source_repo_package_xmls['bar_repo'].tags() == ['ros', 'bar']

    # Export v3 (extended)
    v3_data = cache.get_data(version=3)
    assert v3_data['version'] == 3
    assert v3_data['config'] == CacheConfig.extended().to_dict()
    assert v3_data['release_resources']['bar_repo']['README.md'] == '# bar_repo'
    assert v3_data['release_resources']['bar_repo']['CHANGELOG.rst'] is None
    assert v3_data['source_repo_resources']['bar_repo']['_stars'] == 12

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


