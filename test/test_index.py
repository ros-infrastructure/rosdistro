import os

from rosdistro import get_index

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'files'))


def test_get_index():
    url = 'file://' + FILES_DIR + '/index.yaml'
    get_index(url)
