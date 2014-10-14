import os
import yaml

from rosdistro import get_index, get_release_file
from rosdistro.release_file import ReleaseFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_release_file():
    url = 'file://' + FILES_DIR + '/foo/distribution.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    rel_file = ReleaseFile('foo', data)
    _validate_rel_file(rel_file)


def test_get_release_file():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    rel_file = get_release_file(i, 'foo')
    _validate_rel_file(rel_file)


def _validate_rel_file(rel_file):
    assert('bar_repo' in rel_file.repositories)
    repo = rel_file.repositories['bar_repo']
    assert repo.package_names == ['bar_repo']
    assert 'bar_repo' in rel_file.packages

    assert'baz-repo' in rel_file.repositories
    repo = rel_file.repositories['baz-repo']
    assert set(repo.package_names) == set(['baz_pkg1', 'baz_pkg2'])
    assert 'baz_pkg1' in rel_file.packages
    assert 'baz_pkg2' in rel_file.packages
