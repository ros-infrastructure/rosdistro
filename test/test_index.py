import os

from rosdistro import (
    get_distribution_cache,
    get_distribution_file,
    get_distribution_files,
    get_extended_cached_distribution,
    get_extended_distribution_cache,
    get_extended_distribution_cache_string,
    get_index,
    get_index_url,
    get_package_condition_context,
)
from rosdistro.index import Index

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_get_index_url():
    get_index_url()


def test_get_index_v2():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    i = get_index(url)
    assert len(i.distributions.keys()) == 1
    assert 'foo' in i.distributions.keys()

    assert 'distribution_status' not in i.distributions['foo']
    assert 'distribution_type' not in i.distributions['foo']


def test_get_index_v3():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v3.yaml'))
    i = get_index(url)
    assert len(i.distributions.keys()) == 1
    assert 'foo' in i.distributions.keys()

    assert 'distribution_status' not in i.distributions['foo']
    assert 'distribution_type' not in i.distributions['foo']

    dist_files = get_distribution_files(i, 'foo')
    assert len(dist_files) == 2
    get_distribution_file(i, 'foo')


def test_get_index_v3_invalid():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v3_invalid.yaml'))
    i = get_index(url)

    dist_files = get_distribution_files(i, 'foo')
    assert len(dist_files) == 2
    try:
        get_distribution_file(i, 'foo')
        assert False
    except AssertionError:
        pass


def test_get_index_v4():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v4.yaml'))
    i = get_index(url)
    assert len(i.distributions.keys()) == 1
    assert 'foo' in i.distributions.keys()

    assert i.distributions['foo']['distribution_status'] == 'active'
    assert i.distributions['foo']['distribution_type'] == 'ros1'

    dist_files = get_distribution_files(i, 'foo')
    assert len(dist_files) == 2
    get_distribution_file(i, 'foo')


def _wait_for_server():
    import socket
    import time
    i = 1
    while True:
        try:
            with socket.create_connection(('127.0.0.1', 9876), timeout=5):
                pass
        except OSError:
            if i >= 20:
                raise
            time.sleep(0.1)
        else:
            print(f'Server is available after {i} tries')
            return


def test_get_index_from_http_with_query_parameters():
    import subprocess
    import sys
    url = 'http://localhost:9876/index_v3.yaml?raw&at=master'
    # start a http server and wait
    if sys.version_info < (3, 0, 0):
        proc = subprocess.Popen([sys.executable, '-m', 'SimpleHTTPServer', '9876'],
                                cwd=FILES_DIR)
    else:
        proc = subprocess.Popen([sys.executable, '-m', 'http.server', '9876'],
                                cwd=FILES_DIR)
    try:
        _wait_for_server()
        i = get_index(url)
        assert len(i.distributions.keys()) == 1
        assert 'foo' in i.distributions.keys()

        # test if every url has the same queries
        for key, dist_urls in i.distributions['foo'].items():
            if key in ('distribution_status', 'distribution_type'):
                continue
            if not isinstance(dist_urls, list):
                dist_urls = [dist_urls]
            for dist_url in dist_urls:
                assert dist_url.endswith('?raw&at=master')
        dist_files = get_distribution_files(i, 'foo')
        assert len(dist_files) == 2
        get_distribution_file(i, 'foo')
    finally:
        proc.terminate()


def test_get_condition_context():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v4.yaml'))
    i = get_index(url)
    condition_context = get_package_condition_context(i, 'foo')

    assert condition_context == {
        'ROS_DISTRO': 'foo',
        'ROS_PYTHON_VERSION': '3',
        'ROS_VERSION': '1',
    }


def test_index_extended_distribution_cache_urls():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v4.yaml'))
    i = get_index(url)
    assert i.distributions['foo']['distribution_cache'].endswith('foo/release-cache.yaml')
    assert i.distributions['foo']['extended_distribution_cache'].endswith('foo/release-extended-cache.yaml')

    # Explicit extended_distribution_cache in index data
    explicit_index = Index(
        {
            'type': 'index',
            'version': 4,
            'distributions': {
                'foo': {
                    'distribution': ['foo/distribution.yaml'],
                    'distribution_cache': 'foo/release-cache.yaml',
                    'extended_distribution_cache': 'foo/release-cache.yaml',
                }
            },
        },
        base_url=path_to_url(FILES_DIR),
    )
    assert explicit_index.distributions['foo']['extended_distribution_cache'].endswith('foo/release-cache.yaml')
    cache_default = get_distribution_cache(explicit_index, 'foo', extended=False)
    cache_ext = get_extended_distribution_cache(explicit_index, 'foo')
    cache_ext_str = get_extended_distribution_cache_string(explicit_index, 'foo')
    dist_ext = get_extended_cached_distribution(explicit_index, 'foo')
    assert cache_default.distribution_file.name == 'foo'
    assert cache_ext.distribution_file.name == 'foo'
    assert 'release_package_xmls' in cache_ext_str
    assert dist_ext.name == 'foo'
