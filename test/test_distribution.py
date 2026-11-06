import os

from rosdistro import get_cached_distribution, get_distribution_file, get_distribution_files, get_index
from rosdistro.dependency_walker import DependencyWalker, SourceDependencyWalker
from rosdistro.distribution import Distribution
from rosdistro.distribution_cache import DistributionCache
from rosdistro.distribution_file import DistributionFile
from rosdistro.loader import load_url
from rosdistro.package import Package
from rosdistro.source_repository_cache import SourceRepositoryCache

import yaml

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def _load_foo_dist_file():
    url = path_to_url(os.path.join(FILES_DIR, 'foo', 'distribution.yaml'))
    yaml_str = load_url(url)
    data = yaml.safe_load(yaml_str)
    return DistributionFile('foo', data), data


def test_distribution_file():
    dist_file, _ = _load_foo_dist_file()
    _validate_dist_file(dist_file)


def test_get_distribution_file():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    i = get_index(url)
    dist_file = get_distribution_file(i, 'foo')
    _validate_dist_file(dist_file)

    dist_files = get_distribution_files(i, 'foo')
    assert len(dist_files) == 1


def _validate_dist_file(dist_file):
    assert('bar_repo' in dist_file.repositories)
    repo = dist_file.repositories['bar_repo']
    assert repo.release_repository.package_names == ['bar_repo']
    assert 'bar_repo' in dist_file.release_packages

    assert'baz-repo' in dist_file.repositories
    repo = dist_file.repositories['baz-repo']
    assert set(repo.release_repository.package_names) == set(['baz_pkg1', 'baz_pkg2'])
    assert 'baz_pkg1' in dist_file.release_packages
    assert 'baz_pkg2' in dist_file.release_packages


def test_distribution_release_resources():
    dist_file, _ = _load_foo_dist_file()
    calls = []

    def provider_none(dist_name, repo, pkg_name, filepath='package.xml'):
        calls.append(('none', dist_name, pkg_name, filepath))
        return None

    def provider_mock(dist_name, repo, pkg_name, filepath='package.xml'):
        calls.append(('mock', dist_name, pkg_name, filepath))
        resources = {
            ('bar_repo', 'package.xml'): '<package><name>bar_repo</name></package>',
            ('bar_repo', 'README.md'): '# bar_repo README',
            ('bar_repo', 'CHANGELOG.rst'): '0.1.2 changelog',
        }
        return resources.get((pkg_name, filepath))

    dist = Distribution(dist_file, manifest_providers=[provider_none, provider_mock])

    # Attribute delegation to DistributionFile
    assert dist.name == 'foo'
    assert 'bar_repo' in dist.repositories

    # Test get_release_package_xml, get_release_readme, get_release_changelog, get_release_resource
    assert dist.get_release_package_xml('bar_repo') == '<package><name>bar_repo</name></package>'
    assert dist.get_release_readme('bar_repo') == '# bar_repo README'
    assert dist.get_release_changelog('bar_repo') == '0.1.2 changelog'
    assert dist.get_release_resource('bar_repo', 'package.xml') == '<package><name>bar_repo</name></package>'

    # Subsequent calls should hit the in-memory cache and not re-invoke providers
    call_count = len(calls)
    assert dist.get_release_package_xml('bar_repo') == '<package><name>bar_repo</name></package>'
    assert dist.get_release_readme('bar_repo') == '# bar_repo README'
    assert dist.get_release_changelog('bar_repo') == '0.1.2 changelog'
    assert len(calls) == call_count

    # Missing resource for existing package returns None and caches None
    assert dist.get_release_resource('baz_pkg1', 'README.md') is None
    call_count_after_miss = len(calls)
    assert dist.get_release_resource('baz_pkg1', 'README.md') is None
    assert len(calls) == call_count_after_miss

    # Unknown package returns None
    assert dist.get_release_package_xml('nonexistent_pkg') is None

    # Repo with version=None or release_repository=None returns None
    dist_file.repositories['baz-repo'].release_repository.version = None
    assert dist.get_release_package_xml('baz_pkg2') is None
    dist_file.repositories['baz-repo'].release_repository = None
    assert dist.get_release_package_xml('baz_pkg2') is None

    # Empty manifest_providers list returns None cleanly
    dist_empty_mp = Distribution(dist_file, manifest_providers=[])
    assert dist_empty_mp.get_release_package_xml('bar_repo') is None


def test_distribution_source_resources():
    dist_file, _ = _load_foo_dist_file()
    source_calls = []

    repo_cache = SourceRepositoryCache.from_ref('deadbeef1234567890')
    repo_cache.add('bar_repo', 'bar_repo', '<package><name>bar_repo</name><version>0.1.2</version></package>', 'package.xml')
    repo_cache.add('bar_repo', 'bar_repo', '# Source README', 'README.md')
    repo_cache.add('bar_repo', 'bar_repo', '0.1.2 source changelog', 'CHANGELOG.rst')
    repo_cache.set_stars(10)
    repo_cache.set_description('Bar repo description')
    repo_cache.set_tags(['ros', 'bar'])

    def source_provider_none(repo):
        source_calls.append(('none', repo.name))
        return None

    def source_provider_mock(repo):
        source_calls.append(('mock', repo.name))
        if repo.name == 'bar_repo':
            return repo_cache
        return None

    dist = Distribution(
        dist_file,
        source_manifest_providers=[source_provider_none, source_provider_mock]
    )

    # Calling get_source_package_xml directly before get_source_repo_resources lazy-loads the repo!
    assert dist.get_source_package_xml('bar_repo') == '<package><name>bar_repo</name><version>0.1.2</version></package>'

    # Fetch source repo resources and verify source_packages is populated (excluding '_'-prefixed keys)
    fetched_cache = dist.get_source_repo_resources('bar_repo')
    assert fetched_cache is repo_cache
    assert 'bar_repo' in dist.source_packages
    assert '_ref' not in dist.source_packages
    assert '_stars' not in dist.source_packages

    # Deprecated alias get_source_repo_package_xmls returns the same cached object without re-fetching
    call_count = len(source_calls)
    assert dist.get_source_repo_package_xmls('bar_repo') is repo_cache
    assert len(source_calls) == call_count

    # Access source package.xml, README.md, CHANGELOG.rst, and generic resource
    assert dist.get_source_package_xml('bar_repo') == '<package><name>bar_repo</name><version>0.1.2</version></package>'
    assert dist.get_source_readme('bar_repo') == '# Source README'
    assert dist.get_source_changelog('bar_repo') == '0.1.2 source changelog'
    assert dist.get_source_resource('bar_repo', 'package.xml') == '<package><name>bar_repo</name><version>0.1.2</version></package>'
    assert dist.get_source_resource('bar_repo', 'NONEXISTENT.txt') is None

    # Package registered in source_packages but missing from repo_cache returns None safely
    dist.source_packages['missing_in_cache'] = Package('missing_in_cache', 'bar_repo')
    assert dist.get_source_package_xml('missing_in_cache') is None

    # Package registered in source_packages whose repo provider returns None
    dist.source_packages['baz_pkg1'] = Package('baz_pkg1', 'baz-repo')
    assert dist.get_source_package_xml('baz_pkg1') is None

    # Unknown package not in source_packages returns None safely
    assert dist.get_source_package_xml('unknown_pkg') is None

    # Repository without source_repository returns None safely
    assert dist.get_source_repo_resources('release_only_repo') is None
    assert dist.get_source_repo_resources('unknown_repo') is None


def test_get_cached_distribution_resources():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    index = get_index(url)
    _, dist_file_data = _load_foo_dist_file()

    valid_pkg_xml = (
        '<?xml version="1.0" ?>'
        '<package format="2">'
        '<name>bar_repo</name>'
        '<version>0.1.2</version>'
        '<description>bar_repo desc</description>'
        '<maintainer email="test@example.com">Test</maintainer>'
        '<license>BSD</license>'
        '<depend>baz_pkg1</depend>'
        '</package>'
    )
    cache_data = {
        'type': 'cache',
        'version': 3,
        'name': 'foo',
        'distribution_file': dist_file_data,
        'release_resources': {
            'bar_repo': {
                'package.xml': valid_pkg_xml,
                'README.md': '# Cached Release README',
                'CHANGELOG.rst': 'Cached Release Changelog',
            }
        },
        'source_repo_resources': {
            'bar_repo': {
                '_ref': 'abcdef1234567890',
                'bar_repo': {
                    'package_path': '',
                    'package.xml': valid_pkg_xml,
                    'README.md': '# Cached Source README',
                    'CHANGELOG.rst': 'Cached Source Changelog',
                }
            }
        }
    }
    cache = DistributionCache('foo', data=cache_data)
    dist = get_cached_distribution(index, 'foo', cache=cache, allow_lazy_load=False)

    assert dist.get_release_package_xml('bar_repo') == valid_pkg_xml
    assert dist.get_release_readme('bar_repo') == '# Cached Release README'
    assert dist.get_release_changelog('bar_repo') == 'Cached Release Changelog'
    assert dist.get_source_package_xml('bar_repo') == valid_pkg_xml
    assert dist.get_source_readme('bar_repo') == '# Cached Source README'
    assert dist.get_source_changelog('bar_repo') == 'Cached Source Changelog'
    assert dist.get_source_repo_resources('bar_repo').ref() == 'abcdef1234567890'

    walker = DependencyWalker(dist)
    assert walker.get_depends('bar_repo', 'build') == set(['baz_pkg1'])

    source_walker = SourceDependencyWalker(dist)
    assert source_walker.get_depends('bar_repo', 'build') == set(['baz_pkg1'])

    # Test get_source_repo_metadata resolution by package name and release-only/unknown repo handling
    baz_cache = SourceRepositoryCache.from_ref('1234567890abcdef')
    baz_cache.set_stars(77)
    baz_cache.add('baz_pkg1', 'baz_pkg1', valid_pkg_xml, 'package.xml')
    cache.source_repo_resources['baz-repo'] = baz_cache
    cache.distribution_file.source_packages = cache.get_source_packages()
    cache.config['source_repo_metadata']['stars'] = {'enabled': True}

    assert dist.get_source_repo_metadata('baz-repo', 'stars', require_crawled=True) == 77
    assert dist.get_source_repo_metadata('baz_pkg1', 'stars', require_crawled=True) == 77
    assert dist.get_source_repo_metadata('release_only_repo', 'stars', require_crawled=True) is None
    assert dist.get_source_repo_metadata('unknown_repo', 'stars', require_crawled=True) is None
    assert dist.get_release_readme('unknown_pkg', require_crawled=True) is None


def test_distribution_doc_repository_fallback():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    index = get_index(url)
    _, dist_file_data = _load_foo_dist_file()

    # Add a doc-only repository (no 'source' section)
    dist_file_data['repositories']['soar_ros'] = {
        'doc': {
            'type': 'git',
            'url': 'https://github.com/example-test/soar_ros.git',
            'version': 'main',
        },
    }
    dist_file = DistributionFile('foo', dist_file_data)

    soar_xml = (
        '<?xml version="1.0" ?>'
        '<package format="2">'
        '<name>soar_ros</name>'
        '<version>0.2.0</version>'
        '<description>soar_ros desc</description>'
        '<maintainer email="test@example.com">Test</maintainer>'
        '<license>BSD</license>'
        '<depend>bar_repo</depend>'
        '</package>'
    )
    soar_cache = SourceRepositoryCache.from_ref('feedface1234567890')
    soar_cache.set_stars(25)
    soar_cache.set_description('SOAR ROS integration')
    soar_cache.set_tags(['ros2', 'soar'])
    soar_cache.add('soar_ros', '', soar_xml, 'package.xml')
    soar_cache.add('soar_ros', '', '# soar_ros README', 'README.md')
    soar_cache.add('soar_ros', '', '0.2.0 changelog', 'CHANGELOG.rst')

    provider_calls = []

    def doc_fallback_provider(repo):
        provider_calls.append((repo.name, repo.url, repo.version))
        if repo.name == 'soar_ros':
            return soar_cache
        return None

    dist = Distribution(dist_file, source_manifest_providers=[doc_fallback_provider])

    # Lazy-loading by package/repo name falls back to doc_repository when source_repository is None
    assert dist.get_source_package_xml('soar_ros') == soar_xml
    assert provider_calls == [('soar_ros', 'https://github.com/example-test/soar_ros.git', 'main')]
    assert 'soar_ros' in dist.source_packages
    assert dist.get_source_readme('soar_ros') == '# soar_ros README'
    assert dist.get_source_changelog('soar_ros') == '0.2.0 changelog'
    assert dist.get_source_repo_metadata('soar_ros', 'stars') == 25
    assert dist.get_source_repo_metadata('soar_ros', 'description') == 'SOAR ROS integration'
    assert dist.get_source_repo_metadata('soar_ros', 'tags') == ['ros2', 'soar']

    # Offline cached distribution (allow_lazy_load=False) also resolves doc-only repositories
    cache_data = {
        'type': 'cache',
        'version': 3,
        'name': 'foo',
        'distribution_file': dist_file_data,
        'release_resources': {},
        'source_repo_resources': {
            'soar_ros': soar_cache.get_data(version=3),
            'bar_repo': {
                '_ref': 'abcdef1234567890',
                'bar_repo': {
                    'package_path': '',
                    'package.xml': soar_xml.replace('<name>soar_ros</name>', '<name>bar_repo</name>').replace('<depend>bar_repo</depend>', ''),
                },
            },
        },
    }
    cache = DistributionCache('foo', data=cache_data)
    cached_dist = get_cached_distribution(index, 'foo', cache=cache, allow_lazy_load=False)

    assert cached_dist.is_source_resource_crawled('package.xml', pkg_name='soar_ros') is True
    assert cached_dist.is_source_resource_crawled('README.md', pkg_name='soar_ros') is True
    assert cached_dist.is_source_metadata_crawled('stars', repo_name='soar_ros') is True
    assert cached_dist.get_source_package_xml('soar_ros', require_crawled=True) == soar_xml
    assert cached_dist.get_source_readme('soar_ros', require_crawled=True) == '# soar_ros README'
    assert cached_dist.get_source_changelog('soar_ros', require_crawled=True) == '0.2.0 changelog'
    assert cached_dist.get_source_repo_metadata('soar_ros', 'stars', require_crawled=True) == 25

    source_walker = SourceDependencyWalker(cached_dist)
    assert source_walker.get_depends('soar_ros', 'build') == set(['bar_repo'])

    # With fallback_src_to_doc=False on cache config, get_source_repo_resources skips doc-only repos
    no_fallback_cache = DistributionCache(
        'foo',
        distribution_file_data=dist_file_data,
        config={'fallback_src_to_doc': False, 'source_package_resources': {'package.xml': True}},
    )
    no_fallback_dist = get_cached_distribution(index, 'foo', cache=no_fallback_cache, allow_lazy_load=True)
    assert no_fallback_dist.get_source_repo_resources('soar_ros') is None



