import os

from rosdistro import get_doc_build_files, get_index
from rosdistro.doc_build_file import DocBuildFile
from rosdistro.loader import load_url

import yaml

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_doc_build_file():
    url = path_to_url(os.path.join(FILES_DIR, 'foo', 'doc-build.yaml'))
    yaml_str = load_url(url)
    data = yaml.safe_load(yaml_str)
    DocBuildFile('foo', data)


def test_get_doc_build_files():
    url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    i = get_index(url)
    files = get_doc_build_files(i, 'foo')
    assert len(files) == 1
    build_file = files[0]
    assert build_file.jenkins_job_timeout == 23
