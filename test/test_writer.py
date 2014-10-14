import difflib
import os

from rosdistro import get_distribution_file, get_index

from rosdistro.writer import yaml_from_distribution_file

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
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    index = get_index(url)
    distribution_file = get_distribution_file(index, 'foo')
    data = yaml_from_distribution_file(distribution_file)
    with open(os.path.join(FILES_DIR, 'foo', 'distribution.yaml'), 'r') as f:
        expected = f.read()
    assert data == expected, get_diff(expected, data)
