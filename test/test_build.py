import os
import yaml

from rosdistro import get_index, get_release, get_release_build_files, get_release_builds
from rosdistro.build_file import BuildFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'files'))


def test_build_file():
    url = 'file://' + FILES_DIR + '/foo-build.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    BuildFile('foo', data)


def test_get_release_build_files():
    url = 'file://' + FILES_DIR + '/index.yaml'
    i = get_index(url)
    get_release_build_files(i, 'foo')


def test_get_release_builds():
    url = 'file://' + FILES_DIR + '/index.yaml'
    i = get_index(url)
    d = get_release(i, 'foo')
    builds = get_release_builds(i, d)
    assert len(builds) == 1
