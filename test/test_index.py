import os

from rosdistro import get_distribution_file
from rosdistro import get_distribution_files
from rosdistro import get_index
from rosdistro import get_index_url
from rosdistro import get_package_condition_context

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


def test_get_index_from_http_with_query_parameters():
    import subprocess
    import sys
    import time
    url = 'http://localhost:9876/index_v3.yaml?raw&at=master'
    # start a http server and wait
    if sys.version_info < (3, 0, 0):
        proc = subprocess.Popen([sys.executable, '-m', 'SimpleHTTPServer', '9876'],
                                cwd=FILES_DIR)
    else:
        proc = subprocess.Popen([sys.executable, '-m', 'http.server', '9876'],
                                cwd=FILES_DIR)
    time.sleep(0.5)
    try:
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
