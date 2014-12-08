import os
import yaml

from rosdistro import get_doc_file, get_index
from rosdistro.doc_file import DocFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_doc_file():
    url = 'file://' + FILES_DIR + '/foo/distribution.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    doc_file = DocFile('foo', data)
    _validate_doc_file(doc_file)


def test_get_doc_file():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    doc_file = get_doc_file(i, 'foo')
    _validate_doc_file(doc_file)


def _validate_doc_file(doc_file):
    assert(set(['bar_repo', 'baz-repo']) == set(doc_file.repositories.keys()))

    repo = doc_file.repositories['bar_repo']
    assert(repo.type == 'git')
    assert(repo.url == 'https://github.com/example-test/bar_repo.git')
    assert(repo.version == 'master')

    repo = doc_file.repositories['baz-repo']
    assert(repo.type == 'hg')
    assert(repo.url == 'https://bitbucket.org/baz-test/baz-repo')
    assert(repo.version == 'default')
