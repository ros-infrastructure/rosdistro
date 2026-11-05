import copy
import os
from unittest.mock import patch

import yaml

from rosdistro import get_distribution_cache, get_index, get_release_cache
from rosdistro.loader import load_url
from rosdistro.release import Release
from rosdistro.release_cache import ReleaseCache
from rosdistro.release_cache_generator import generate_release_cache

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def _load_foo_distribution_data():
    url = path_to_url(os.path.join(FILES_DIR, 'foo', 'distribution.yaml'))
    return yaml.safe_load(load_url(url))


def test_get_release_cache():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    i = get_index(url)
    get_distribution_cache(i, 'foo')

    rel_cache = get_release_cache(i, 'foo')
    assert isinstance(rel_cache, ReleaseCache)
    assert 'catkin' in rel_cache.package_xmls
    assert 'catkin' in rel_cache.release_package_xmls
    assert 'catkin' in rel_cache.release_resources
    assert rel_cache.release_resources['catkin']['package.xml'] == rel_cache.package_xmls['catkin']


def test_release_cache_init_with_distribution_file_data_only():
    dist_data = _load_foo_distribution_data()
    cache = ReleaseCache('foo', distribution_file_data=dist_data)

    assert cache.version == 2
    assert cache.release_file.name == 'foo'
    assert 'bar_repo' in cache.release_file.packages
    assert 'baz_pkg1' in cache.release_file.packages
    assert 'baz_pkg2' in cache.release_file.packages
    assert cache.package_xmls == {}
    assert cache.release_package_xmls == {}
    assert cache.release_resources == {}

    data = cache.get_data()
    assert data['type'] == 'cache'
    assert data['version'] == 2
    assert data['name'] == 'foo'
    assert data['distribution_file'] == dist_data
    assert data['package_xmls'] == {}
    assert data['release_resources'] == {}


def test_release_cache_init_with_v2_release_package_xmls():
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    data = {
        'type': 'cache',
        'version': 2,
        'name': 'foo',
        'distribution_file': dist_data,
        'release_package_xmls': {
            'bar_repo': bar_xml,
        },
    }
    cache = ReleaseCache('foo', data=data)
    assert cache.package_xmls == {'bar_repo': bar_xml}
    assert cache.release_package_xmls == {'bar_repo': bar_xml}
    assert cache.release_resources == {'bar_repo': {'package.xml': bar_xml}}

    exported = cache.get_data()
    assert exported['package_xmls'] == {'bar_repo': bar_xml}
    assert exported['release_resources'] == {'bar_repo': {'package.xml': bar_xml}}


def test_release_cache_init_with_v2_package_xmls():
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    data = {
        'type': 'cache',
        'version': 2,
        'name': 'foo',
        'distribution_file': dist_data,
        'package_xmls': {
            'bar_repo': bar_xml,
        },
    }
    cache = ReleaseCache('foo', data=data)
    assert cache.package_xmls == {'bar_repo': bar_xml}
    assert cache.release_package_xmls == {'bar_repo': bar_xml}
    assert cache.release_resources == {'bar_repo': {'package.xml': bar_xml}}

    # Round-trip through get_data()
    roundtrip = ReleaseCache('foo', data=cache.get_data())
    assert roundtrip.package_xmls == {'bar_repo': bar_xml}
    assert roundtrip.release_resources == {'bar_repo': {'package.xml': bar_xml}}


def test_release_cache_init_with_v3_release_resources():
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    data = {
        'type': 'cache',
        'version': 3,
        'name': 'foo',
        'distribution_file': dist_data,
        'release_resources': {
            'bar_repo': {
                'package.xml': bar_xml,
                'README.md': '# bar_repo',
                'CHANGELOG.rst': '0.1.2 changelog',
                'version': '0.1.2',
            },
            'baz_pkg1': {
                'README.md': '# baz_pkg1 without package.xml',
            },
        },
    }
    cache = ReleaseCache('foo', data=data)
    assert cache.package_xmls == {'bar_repo': bar_xml}
    assert cache.release_package_xmls == {'bar_repo': bar_xml}
    assert cache.release_resources['bar_repo']['package.xml'] == bar_xml
    assert cache.release_resources['bar_repo']['README.md'] == '# bar_repo'
    assert cache.release_resources['baz_pkg1'] == {'README.md': '# baz_pkg1 without package.xml'}

    exported = cache.get_data()
    assert exported['type'] == 'cache'
    assert exported['version'] == 2
    assert exported['name'] == 'foo'
    assert exported['package_xmls'] == {'bar_repo': bar_xml}
    assert exported['release_resources']['bar_repo']['README.md'] == '# bar_repo'
    assert exported['release_resources']['baz_pkg1'] == {'README.md': '# baz_pkg1 without package.xml'}


def test_release_cache_get_data_syncs_mutations():
    dist_data = _load_foo_distribution_data()
    cache = ReleaseCache('foo', distribution_file_data=dist_data)

    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    baz1_xml = '<package><name>baz_pkg1</name><version>7.7.7</version></package>'

    # Mutate package_xmls directly and mutate release_resources directly
    cache.package_xmls['bar_repo'] = bar_xml
    cache.release_resources['baz_pkg1'] = {'package.xml': baz1_xml, 'README.md': '# baz1'}

    exported = cache.get_data()
    assert exported['package_xmls'] == {'bar_repo': bar_xml, 'baz_pkg1': baz1_xml}
    assert exported['release_resources']['bar_repo'] == {'package.xml': bar_xml}
    assert exported['release_resources']['baz_pkg1'] == {'package.xml': baz1_xml, 'README.md': '# baz1'}


def test_release_cache_update_distribution():
    dist_data = _load_foo_distribution_data()
    bar_xml = '<package><name>bar_repo</name><version>0.1.2</version></package>'
    baz1_xml = '<package><name>baz_pkg1</name><version>7.7.7</version></package>'
    baz2_xml = '<package><name>baz_pkg2</name><version>7.7.7</version></package>'
    obsolete_xml = '<package><name>obsolete_pkg</name><version>1.0.0</version></package>'

    data = {
        'type': 'cache',
        'version': 2,
        'name': 'foo',
        'distribution_file': dist_data,
        'release_resources': {
            'bar_repo': {'package.xml': bar_xml, 'README.md': '# bar'},
            'baz_pkg1': {'package.xml': baz1_xml, 'README.md': '# baz1'},
            'baz_pkg2': {'package.xml': baz2_xml},
            'obsolete_pkg': {'package.xml': obsolete_xml},
        },
    }
    cache = ReleaseCache('foo', data=data)

    new_dist_data = copy.deepcopy(dist_data)
    # Bump bar_repo version (should invalidate bar_repo cache entry)
    new_dist_data['repositories']['bar_repo']['release']['version'] = '0.2.0'
    # Remove baz_pkg2 from baz-repo while keeping baz_pkg1 at same version/url
    new_dist_data['repositories']['baz-repo']['release']['packages'] = ['baz_pkg1']

    cache.update_distribution(new_dist_data)

    # obsolete_pkg (not in old dist), bar_repo (version changed), and baz_pkg2 (removed in new dist)
    # should all be removed; baz_pkg1 (unchanged) should remain.
    assert list(cache.package_xmls.keys()) == ['baz_pkg1']
    assert cache.package_xmls['baz_pkg1'] == baz1_xml
    assert list(cache.release_resources.keys()) == ['baz_pkg1']
    assert cache.release_resources['baz_pkg1'] == {'package.xml': baz1_xml, 'README.md': '# baz1'}

    # Also verify update_distribution accepts a list of distribution file dicts
    cache.update_distribution([new_dist_data])
    assert list(cache.package_xmls.keys()) == ['baz_pkg1']
    assert list(cache.release_resources.keys()) == ['baz_pkg1']


def test_generate_release_cache_offline():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))

    def fake_manifest_provider(dist_name, repo, pkg_name, filepath='package.xml'):
        version = repo.version.split('-')[0]
        return (
            '<package format="2">'
            f'<name>{pkg_name}</name>'
            f'<version>{version}</version>'
            '<description>desc</description>'
            '<maintainer email="test@example.com">Test</maintainer>'
            '<license>BSD</license>'
            '</package>'
        )

    with patch.object(Release, 'default_manifest_providers', [fake_manifest_provider]):
        cache = generate_release_cache(get_index(url), 'foo')

    assert isinstance(cache, ReleaseCache)
    expected_pkgs = {'bar_repo', 'baz_pkg1', 'baz_pkg2', 'release_only_repo'}
    assert set(cache.package_xmls.keys()) == expected_pkgs
    assert set(cache.release_resources.keys()) == expected_pkgs
    for pkg_name in expected_pkgs:
        assert pkg_name in cache.package_xmls[pkg_name]
        assert cache.release_resources[pkg_name]['package.xml'] == cache.package_xmls[pkg_name]
