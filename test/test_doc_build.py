import os
import yaml

from rosdistro import get_doc_build_files, get_index
from rosdistro.doc_build_file import DocBuildFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_doc_build_file():
    url = 'file://' + FILES_DIR + '/foo/doc-build.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    DocBuildFile('foo', data)


def test_get_doc_build_files():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    files = get_doc_build_files(i, 'foo')
    assert len(files) == 1
    build_file = files[0]
    assert build_file.jenkins_job_timeout == 23
