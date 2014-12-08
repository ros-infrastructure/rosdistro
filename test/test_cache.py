import os

from rosdistro import get_index, get_distribution_cache

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_get_release_cache():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    get_distribution_cache(i, 'foo')
