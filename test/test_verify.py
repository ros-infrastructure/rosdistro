import os
from pathlib import PureWindowsPath
import sys
from unittest.mock import patch

import pytest

from rosdistro import get_distribution_file, get_index
from rosdistro.common import path_to_url as common_path_to_url, url_to_path
from rosdistro.loader import load_url
from rosdistro.verify import (
    _check_file_identical,
    _to_yaml,
    _yaml_header_lines,
    REP_URL_TEMPLATES,
    reformat_files,
    verify_files_identical,
    verify_files_parsable,
)

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


def test_path_to_url_and_url_to_path(tmp_path):
    spaced_dir = tmp_path / 'dir with spaces'
    spaced_dir.mkdir()
    sample = spaced_dir / 'index.yaml'
    sample.write_text('hello: world\n', encoding='utf-8')

    url = common_path_to_url(str(sample))
    assert url.startswith('file:///')
    assert '\\' not in url
    assert load_url(url) == 'hello: world\n'
    assert url_to_path(url) == os.path.abspath(str(sample))

    # Verify Windows drive and UNC paths always yield RFC 8089 file:// URIs
    with patch('rosdistro.common.os.path.abspath', side_effect=lambda p: p), \
         patch('rosdistro.common.Path', PureWindowsPath):
        win_path = r'C:\Users\runneradmin\AppData\Local\Temp\my dir\index.yaml'
        assert common_path_to_url(win_path) == (
            'file:///C:/Users/runneradmin/AppData/Local/Temp/my%20dir/index.yaml'
        )
        unc_path = r'\\server\share\my dir\index.yaml'
        assert common_path_to_url(unc_path) == 'file://server/share/my%20dir/index.yaml'

    if sys.platform == 'win32':
        assert url_to_path('file:///C:/Users/runneradmin/my%20dir/index.yaml') == (
            r'C:\Users\runneradmin\my dir\index.yaml'
        )
        assert url_to_path('file://server/share/my%20dir/index.yaml') == (
            r'\\server\share\my dir\index.yaml'
        )
    else:
        assert url_to_path('file://localhost/tmp/my%20dir/index.yaml') == '/tmp/my dir/index.yaml'

    with pytest.raises(ValueError):
        url_to_path('https://example.com/index.yaml')


def test_reformat_files_with_spaced_local_path(tmp_path):
    index_url = path_to_url(os.path.join(FILES_DIR, 'index_v2.yaml'))
    dist_file = get_distribution_file(get_index(index_url), 'foo')

    spaced_dir = tmp_path / 'my distro'
    dist_dir = spaced_dir / 'foo'
    dist_dir.mkdir(parents=True)
    dist_path = dist_dir / 'distribution.yaml'
    # Write unformatted YAML (missing standard REP header) so reformat_files rewrites it
    dist_path.write_text(_to_yaml(dist_file.get_data()), encoding='utf-8')
    index_path = spaced_dir / 'index.yaml'
    index_path.write_text(
        'type: index\nversion: 4\ndistributions:\n'
        '  foo:\n    distribution:\n    - foo/distribution.yaml\n',
        encoding='utf-8',
    )

    local_index_url = common_path_to_url(str(index_path))
    assert not reformat_files(local_index_url)
    assert verify_files_identical(local_index_url)
