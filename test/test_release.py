import os
import yaml

from rosdistro import get_index, get_release_file
from rosdistro.release_file import ReleaseFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'files'))


def test_release_file():
    url = 'file://' + FILES_DIR + '/foo.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    ReleaseFile('foo', data)


def test_get_release_file():
    url = 'file://' + FILES_DIR + '/index.yaml'
    i = get_index(url)
    get_release_file(i, 'foo')
