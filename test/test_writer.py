import difflib
import os

from rosdistro import get_index, get_release_file

from rosdistro.writer import yaml_from_release_file

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def get_diff(expected, actual):
    udiff = difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                 fromfile='expected', tofile='actual')
    udiff_raw = ''
    for line in udiff:
        if line.startswith('@@'):
            udiff_raw += line
        if line.startswith('+'):
            if not line.startswith('+++'):
                line += '\n'
            udiff_raw += line
        if line.startswith('-'):
            if not line.startswith('---'):
                line += '\n'
            udiff_raw += line
        if line.startswith(' '):
            line += '\n'
            udiff_raw += line
    return '\n' + udiff_raw


def test_verify_files_parsable():
    url = 'file://' + FILES_DIR + '/index.yaml'
    index = get_index(url)
    release_file = get_release_file(index, 'foo')
    data = yaml_from_release_file(release_file)
    with open(os.path.join(FILES_DIR, 'foo', 'release.yaml'), 'r') as f:
        expected = f.read()
    assert data == expected, get_diff(expected, data)
