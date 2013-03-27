import os

from rosdistro.verify import verify_files_parsable

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'files'))


def test_verify_files():
    index_url = 'file://' + FILES_DIR + '/index.yaml'
    verify_files_parsable(index_url)
