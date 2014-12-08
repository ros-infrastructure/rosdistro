import os

from rosdistro.verify import verify_files_identical, verify_files_parsable

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_verify_files_parsable():
    index_url = 'file://' + FILES_DIR + '/index_v2.yaml'
    assert verify_files_parsable(index_url)


def test_verify_files_identical():
    index_url = 'file://' + FILES_DIR + '/index_v2.yaml'
    assert verify_files_identical(index_url)
