# -*- coding: utf-8 -*-

import io
import json
import os
import tarfile
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import rosdistro.manifest_provider.github
import rosdistro.manifest_provider.tar
import rosdistro.vcs

from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedManifestProvider, sanitize_xml
from rosdistro.manifest_provider.git import git_manifest_provider, git_source_manifest_provider
from rosdistro.manifest_provider.gitlab import gitlab_manifest_provider, gitlab_source_manifest_provider
from rosdistro.manifest_provider.tar import tar_manifest_provider, tar_source_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification


def test_bitbucket():
    assert '</package>' in bitbucket_manifest_provider('indigo', _rospeex_release_repo(), 'rospeex_msgs')


def test_gitlab():
    assert '</package>' in gitlab_manifest_provider('foxy', _tracetools_analysis_release_repo(), 'tracetools_analysis')


def test_cached():
    class FakeDistributionCache(object):
        def __init__(self):
            self.release_package_xmls = {}
            self.release_resources = {}
    dc = FakeDistributionCache()
    cache = CachedManifestProvider(dc, [rosdistro.manifest_provider.github.github_manifest_provider])
    assert '</package>' in cache('melodic', _genmsg_release_repo(), 'genmsg')


def test_git():
    assert '</package>' in git_manifest_provider('melodic', _genmsg_release_repo(), 'genmsg')


def test_git_legacy():
    rosdistro.vcs.Git._client_version = '1.7.0'
    assert '</package>' in git_manifest_provider('melodic', _genmsg_release_repo(), 'genmsg')
    rosdistro.vcs.Git._client_version = None


def test_github():
    assert '</package>' in rosdistro.manifest_provider.github.github_manifest_provider('melodic', _genmsg_release_repo(), 'genmsg')


def test_tar():
    assert '</package>' in rosdistro.manifest_provider.tar.tar_manifest_provider('melodic', _genmsg_release_tarball(), 'genmsg')


def test_git_source():
    repo_cache = git_source_manifest_provider(_genmsg_source_repo())

    # This hash corresponds to the 0.5.11 tag.
    assert repo_cache.ref() == 'a189fc78558e7276df59d2961cfe4f8b4de08a8b'

    assert '' == repo_cache['genmsg']['package_path']
    assert '<version>0.5.11</version>' in repo_cache['genmsg']['package.xml']


# mock_get_url_contents is used to mock out the '_get_url_contents' method in
# the rosdistro.manifest_provider.github module.  Instead of going out to github
# to get data (which can get rate-limited in Travis), it instead pulls a canned
# example from some local files.
def mock_get_url_contents(req):
    import re

    # The urlopen() function from urllib takes either a string or a
    # urllib.request.Request object in; determine the URL in either case.
    if isinstance(req, str):
        haystack = req
    else:
        haystack = req.get_full_url()

    # Now see if the URL ends with 'package.xml'.  If it does, open up the file
    # that has the canned package.xml data we have for genmsg.  Otherwise,
    # return the canned JSON tree data that github would have returned.
    # In both cases, store the file-like object as a module property so that we
    # can properly close it during 'unmock_urlopen'.
    if re.search('.*package.xml$', haystack) is not None:
        fname = 'test/github-genmsg-package.xml'
    elif re.search(r'.*/repos/[^/]+/[^/]+$', haystack) is not None:
        return '{"stargazers_count": 42}'
    else:
        fname = 'test/github-tree-data.json'

    with open(fname, 'r') as infp:
        data = infp.read()

    return data


@patch('rosdistro.manifest_provider.github._get_url_contents', mock_get_url_contents)
def test_github_source():
    repo_cache = rosdistro.manifest_provider.github.github_source_manifest_provider(_genmsg_source_repo())

    # This hash corresponds to the 0.5.7 tag.
    assert repo_cache.ref() == '81b66fe5eb00043c43894ddeee07e738d9b9712f'

    assert '' == repo_cache['genmsg']['package_path']
    assert '<version>0.5.11</version>' in repo_cache['genmsg']['package.xml']
    assert repo_cache._data.get('_stars') == 42


def test_gitlab_source():
    repo_cache = gitlab_source_manifest_provider(_tracetools_analysis_source_repo())

    # This hash corresponds to the 1.0.3 tag.
    assert repo_cache.ref() == 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'

    assert 'ros2trace_analysis' == repo_cache['ros2trace_analysis']['package_path']
    assert '<version>1.0.3</version>' in repo_cache['ros2trace_analysis']['package.xml']
    assert type(repo_cache._data.get('_stars')) is int


def test_git_source_multi():
    repo_cache = git_source_manifest_provider(_ros_source_repo())
    assert repo_cache.ref()
    assert repo_cache['roslib']['package_path'] == os.path.join('core', 'roslib')


def test_tar_source():
    repo_cache = rosdistro.manifest_provider.tar.tar_source_manifest_provider(_genmsg_source_tarball())

    assert repo_cache.ref() is None

    assert 'genmsg-0.5.16' == repo_cache['genmsg']['package_path']
    assert '<version>0.5.16</version>' in repo_cache['genmsg']['package.xml']


def _make_mock_http_response(body, headers=None):
    headers = headers or {}
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.getheader.side_effect = lambda k, default=None: headers.get(k, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _build_tar_bytes(files_dict):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, content in files_dict.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.getvalue()


_SAMPLE_GITLAB_PACKAGE_XML = """<?xml version="1.0"?>
<package format="2">
  <name>ros2trace_analysis</name>
  <version>1.0.3</version>
  <description>Trace analysis package</description>
  <maintainer email="test@example.com">Maintainer</maintainer>
  <license>Apache-2.0</license>
</package>
"""

_SAMPLE_TAR_PACKAGE_XML = """<?xml version="1.0"?>
<package format="2">
  <name>genmsg</name>
  <version>0.5.16</version>
  <description>genmsg package</description>
  <maintainer email="test@example.com">Maintainer</maintainer>
  <license>BSD</license>
</package>
"""


def test_gitlab_manifest_provider_mocked():
    called_urls = []

    def fake_urlopen(req):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        called_urls.append(url)
        return _make_mock_http_response(_SAMPLE_GITLAB_PACKAGE_XML)

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_urlopen), \
         patch('rosdistro.manifest_provider.gitlab.logger') as mock_logger:
        xml = gitlab_manifest_provider(
            'foxy', _tracetools_analysis_release_repo(), 'tracetools_analysis')
        assert '<version>1.0.3</version>' in xml
        assert any('repository/files/package.xml/raw' in u for u in called_urls)
        mock_logger.debug.assert_called()
        mock_logger.warn.assert_not_called()

        called_urls.clear()
        doc = gitlab_manifest_provider(
            'foxy', _tracetools_analysis_release_repo(), 'tracetools_analysis',
            filepath='docs/README.md')
        assert any('repository/files/docs%2FREADME.md/raw' in u for u in called_urls)
        assert doc == _SAMPLE_GITLAB_PACKAGE_XML

    with pytest.raises(RuntimeError):
        gitlab_manifest_provider('melodic', _genmsg_release_repo(), 'genmsg')


def test_gitlab_source_mocked_with_docs():
    long_changelog = '\n'.join(['Changelog line %d' % i for i in range(1, 121)])
    readme_text = '# ros2trace_analysis\nSome documentation   \n'

    def fake_urlopen(req):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if '/repository/commits?' in url:
            return _make_mock_http_response(
                json.dumps([{'id': 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'}]))
        elif '/repository/tree?' in url:
            return _make_mock_http_response(json.dumps([
                {'path': 'ros2trace_analysis/package.xml'},
                {'path': 'ros2trace_analysis/test/nested_pkg/package.xml'},
                {'path': 'ros2trace_analysis/CHANGELOG.rst'},
                {'path': 'ros2trace_analysis/README.md'},
            ]))
        elif url.endswith('/projects/ros-tracing%2Ftracetools_analysis/'):
            return _make_mock_http_response(json.dumps({
                'star_count': 17,
                'description': 'Tools for analysing trace data',
                'tag_list': ['ros2', 'tracing'],
            }))
        elif 'ros2trace_analysis%2Fpackage.xml/raw' in url:
            return _make_mock_http_response(_SAMPLE_GITLAB_PACKAGE_XML)
        elif 'ros2trace_analysis%2FCHANGELOG.rst/raw' in url:
            return _make_mock_http_response(long_changelog)
        elif 'ros2trace_analysis%2FREADME.md/raw' in url:
            return _make_mock_http_response(readme_text)
        raise AssertionError('Unexpected GitLab URL: %s' % url)

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_urlopen):
        # Test with default args (which defaults to ['CHANGELOG.rst', 'README.md'])
        repo_cache = gitlab_source_manifest_provider(_tracetools_analysis_source_repo())
        assert repo_cache.ref() == 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'
        assert repo_cache._data.get('_stars') == 17
        assert repo_cache._data.get('_description') == 'Tools for analysing trace data'
        assert repo_cache._data.get('_tags') == ['ros2', 'tracing']
        assert list(repo_cache.keys()) == ['ros2trace_analysis']
        pkg_entry = repo_cache['ros2trace_analysis']
        assert pkg_entry['package_path'] == 'ros2trace_analysis'
        assert '<version>1.0.3</version>' in pkg_entry['package.xml']
        assert 'Changelog line 1' in pkg_entry['CHANGELOG.rst']
        assert 'Truncated content at 100 of 120 lines' in pkg_entry['CHANGELOG.rst']
        assert pkg_entry['README.md'] == '# ros2trace_analysis\nSome documentation'

        # Test with explicit filepaths=['CHANGELOG.rst', 'README.md']
        repo_cache_explicit = gitlab_source_manifest_provider(
            _tracetools_analysis_source_repo(), filepaths=['CHANGELOG.rst', 'README.md'])
        assert '<version>1.0.3</version>' in repo_cache_explicit['ros2trace_analysis']['package.xml']
        assert 'Truncated content at 100 of 120 lines' in repo_cache_explicit['ros2trace_analysis']['CHANGELOG.rst']
        assert repo_cache_explicit['ros2trace_analysis']['README.md'] == '# ros2trace_analysis\nSome documentation'

        # Test with filepaths=['package.xml']
        repo_cache_xml_only = gitlab_source_manifest_provider(
            _tracetools_analysis_source_repo(), filepaths=['package.xml'])
        assert '<version>1.0.3</version>' in repo_cache_xml_only['ros2trace_analysis']['package.xml']
        assert 'CHANGELOG.rst' not in repo_cache_xml_only['ros2trace_analysis']
        assert 'README.md' not in repo_cache_xml_only['ros2trace_analysis']


def test_gitlab_source_mocked_missing_docs():
    def fake_urlopen(req):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if '/repository/commits?' in url:
            return _make_mock_http_response(
                json.dumps([{'id': 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'}]))
        elif '/repository/tree?' in url:
            return _make_mock_http_response(json.dumps([
                {'path': 'ros2trace_analysis/package.xml'},
            ]))
        elif url.endswith('/projects/ros-tracing%2Ftracetools_analysis/'):
            return _make_mock_http_response(json.dumps({'star_count': 5}))
        elif 'ros2trace_analysis%2Fpackage.xml/raw' in url:
            return _make_mock_http_response(_SAMPLE_GITLAB_PACKAGE_XML)
        elif 'ros2trace_analysis%2FCHANGELOG.rst/raw' in url:
            raise HTTPError(url, 404, 'Not Found', {}, None)
        elif 'ros2trace_analysis%2FREADME.md/raw' in url:
            raise URLError('Not Found')
        raise AssertionError('Unexpected GitLab URL: %s' % url)

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_urlopen):
        repo_cache = gitlab_source_manifest_provider(
            _tracetools_analysis_source_repo(), filepaths=['CHANGELOG.rst', 'README.md'])
        assert repo_cache.ref() == 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'
        pkg_entry = repo_cache['ros2trace_analysis']
        assert pkg_entry['package_path'] == 'ros2trace_analysis'
        assert '<version>1.0.3</version>' in pkg_entry['package.xml']
        assert 'CHANGELOG.rst' not in pkg_entry
        assert 'README.md' not in pkg_entry


def test_tar_manifest_provider_mocked():
    release_subdir = 'genmsg-release-release-melodic-genmsg-0.5.16-1'
    tar_bytes = _build_tar_bytes({
        release_subdir + '/package.xml': _SAMPLE_TAR_PACKAGE_XML,
        release_subdir + '/README.md': '# genmsg release\n',
    })

    with patch('rosdistro.manifest_provider.tar.urlopen',
               side_effect=lambda req: _make_mock_http_response(tar_bytes)):
        xml = tar_manifest_provider('melodic', _genmsg_release_tarball(), 'genmsg')
        assert '<version>0.5.16</version>' in xml

        readme = tar_manifest_provider(
            'melodic', _genmsg_release_tarball(), 'genmsg', filepath='README.md')
        assert readme == '# genmsg release\n'

        with pytest.raises(RuntimeError, match='Could not find'):
            tar_manifest_provider(
                'melodic', _genmsg_release_tarball(), 'genmsg', filepath='CHANGELOG.rst')


def test_tar_source_mocked_with_docs():
    long_changelog = '\n'.join(['0.5.16 (2020-01-01)'] + ['* change %d' % i for i in range(1, 120)])
    readme_text = '# genmsg\nMessage generation package   \n'
    tar_bytes = _build_tar_bytes({
        'genmsg-0.5.16/package.xml': _SAMPLE_TAR_PACKAGE_XML,
        'genmsg-0.5.16/CHANGELOG.rst': long_changelog,
        'genmsg-0.5.16/README.md': readme_text,
    })

    with patch('rosdistro.manifest_provider.tar.urlopen',
               side_effect=lambda req: _make_mock_http_response(tar_bytes)):
        # Test with default args (which defaults to ['CHANGELOG.rst', 'README.md'])
        repo_cache = tar_source_manifest_provider(_genmsg_source_tarball())
        assert repo_cache.ref() is None
        pkg_entry = repo_cache['genmsg']
        assert pkg_entry['package_path'] == 'genmsg-0.5.16'
        assert '<version>0.5.16</version>' in pkg_entry['package.xml']
        assert pkg_entry['CHANGELOG.rst'] != pkg_entry['package.xml']
        assert '0.5.16 (2020-01-01)' in pkg_entry['CHANGELOG.rst']
        assert 'Truncated content at 100 of 120 lines' in pkg_entry['CHANGELOG.rst']
        assert pkg_entry['README.md'] == '# genmsg\nMessage generation package'

        # Test with explicit filepaths=['CHANGELOG.rst', 'README.md']
        repo_cache_explicit = tar_source_manifest_provider(
            _genmsg_source_tarball(), filepaths=['CHANGELOG.rst', 'README.md'])
        assert '<version>0.5.16</version>' in repo_cache_explicit['genmsg']['package.xml']
        assert '0.5.16 (2020-01-01)' in repo_cache_explicit['genmsg']['CHANGELOG.rst']
        assert repo_cache_explicit['genmsg']['README.md'] == '# genmsg\nMessage generation package'

        # Test with filepaths=['package.xml']
        repo_cache_xml_only = tar_source_manifest_provider(
            _genmsg_source_tarball(), filepaths=['package.xml'])
        assert '<version>0.5.16</version>' in repo_cache_xml_only['genmsg']['package.xml']
        assert 'CHANGELOG.rst' not in repo_cache_xml_only['genmsg']
        assert 'README.md' not in repo_cache_xml_only['genmsg']


def test_tar_source_mocked_missing_docs():
    tar_bytes = _build_tar_bytes({
        'package.xml': _SAMPLE_TAR_PACKAGE_XML,
    })

    with patch('rosdistro.manifest_provider.tar.urlopen',
               side_effect=lambda req: _make_mock_http_response(tar_bytes)):
        repo_cache = tar_source_manifest_provider(
            _genmsg_source_tarball(), filepaths=['CHANGELOG.rst', 'README.md'])
        assert repo_cache.ref() is None
        pkg_entry = repo_cache['genmsg']
        assert pkg_entry['package_path'] == ''
        assert '<version>0.5.16</version>' in pkg_entry['package.xml']
        assert 'CHANGELOG.rst' not in pkg_entry
        assert 'README.md' not in pkg_entry


def test_sanitize():
    assert '<a>abc</a>' in sanitize_xml('<a>ab<!-- comment -->c</a>')
    assert '<a><b/><c>ab c</c></a>' in sanitize_xml('<a><b> </b>  <c>  ab  c  </c></a>')

    # This unicode check should be valid.
    assert '<a>français</a>' in sanitize_xml('<a> français  </a>')

    # subsequent parse calls will collapse empty tags, therefore sanitize should do the same
    assert '<a><empty/></a>' in sanitize_xml('<a> <empty> <!-- comment --> </empty> </a>')


def _genmsg_release_repo():
    return ReleaseRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros-gbp/genmsg-release.git',
        'tags': {'release': 'release/melodic/{package}/{version}'},
        'version': '0.5.11-0'
    })


def _genmsg_release_tarball():
    return ReleaseRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros-gbp/genmsg-release/archive/release/melodic/genmsg/0.5.16-1.tar.gz',
        'tags': {'release': '{package}-release-release-melodic-{package}-{version}'},
        'version': '0.5.16-1',
        'type': 'tar'
    })


def _genmsg_source_repo():
    return SourceRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros/genmsg.git',
        'version': '0.5.11'
    })


def _genmsg_source_tarball():
    return SourceRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros/genmsg/archive/0.5.16.tar.gz',
        'type': 'tar'
    })


def _ros_source_repo():
    return SourceRepositorySpecification('ros', {
        'url': 'https://github.com/ros/ros.git',
        'version': 'kinetic-devel'
    })


def _rospeex_release_repo():
    return ReleaseRepositorySpecification('rospeex', {
        'packages': ['rospeex', 'rospeex_msgs'],
        'tags': {'release': 'release/indigo/{package}/{version}'},
        'url': 'https://bitbucket.org/rospeex/rospeex-release.git',
        'version': '2.14.7-0'
    })


def _tracetools_analysis_release_repo():
    return ReleaseRepositorySpecification('tracetools_analysis', {
        'packages': ['ros2trace_analysis', 'tracetools_analysis'],
        'tags': {'release': 'release/foxy/{package}/{version}'},
        'url': 'https://gitlab.com/ros-tracing/tracetools_analysis-release.git',
        'version': '1.0.3-1'
    })


def _tracetools_analysis_source_repo():
    return SourceRepositorySpecification('tracetools_analysis', {
        'url': 'https://gitlab.com/ros-tracing/tracetools_analysis.git',
        'version': '1.0.3'
    })
