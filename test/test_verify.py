import os

from rosdistro import get_distribution_file, get_index
from rosdistro.verify import _check_file_identical, _to_yaml, _yaml_header_lines, REP_URL_TEMPLATES, verify_files_identical, verify_files_parsable

from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_verify_files_parsable():
    index_url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    assert verify_files_parsable(index_url)


def test_verify_files_identical():
    # The fixtures deliberately mix REP URLs: 'foo/distribution.yaml' uses the
    # current one while the build files still use the legacy one, so this
    # covers both being accepted in a single index.
    index_url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    assert verify_files_identical(index_url)


def test_yaml_header_lines_uses_current_rep_url():
    lines = _yaml_header_lines('distribution', 1)
    assert lines[2] == '# see REP 141: https://reps.openrobotics.org/rep-0141/'


def test_check_file_identical_accepts_every_rep_url(tmp_path):
    index_url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    dist_file = get_distribution_file(get_index(index_url), 'foo')
    body = _to_yaml(dist_file.get_data())

    for i, rep_url_template in enumerate(REP_URL_TEMPLATES):
        header = _yaml_header_lines(
            'distribution', dist_file.version, rep_url_template)
        path = tmp_path / ('distribution_%d.yaml' % i)
        path.write_text('\n'.join(header) + '\n' + body)
        assert _check_file_identical(
            dist_file, path_to_url(str(path)), 'distribution'), \
            'header with %r was not accepted' % rep_url_template


def test_check_file_identical_rejects_unknown_rep_url(tmp_path):
    index_url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    dist_file = get_distribution_file(get_index(index_url), 'foo')

    header = _yaml_header_lines('distribution', dist_file.version)
    header[2] = '# see REP 141: https://example.com/rep-0141/'
    path = tmp_path / 'distribution.yaml'
    path.write_text('\n'.join(header) + '\n' + _to_yaml(dist_file.get_data()))
    assert not _check_file_identical(
        dist_file, path_to_url(str(path)), 'distribution')
