import os
import yaml

from rosdistro import get_index, get_source_file
from rosdistro.loader import load_url
from rosdistro.source_file import SourceFile

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_source_file():
    url = 'file://' + FILES_DIR + '/foo/distribution.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    src_file = SourceFile('foo', data)
    _validate_src_file(src_file)


def test_get_source_file():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    src_file = get_source_file(i, 'foo')
    _validate_src_file(src_file)


def _validate_src_file(src_file):
    assert(set(['bar_repo', 'baz-repo']) == set(src_file.repositories.keys()))

    repo = src_file.repositories['bar_repo']
    assert(repo.type == 'git')
    assert(repo.url == 'https://github.com/example-test/bar_repo.git')
    assert(repo.version == 'master')

    repo = src_file.repositories['baz-repo']
    assert(repo.type == 'hg')
    assert(repo.url == 'https://bitbucket.org/baz-test/baz-repo')
    assert(repo.version == 'default')
