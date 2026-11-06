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
from rosdistro.manifest_provider.cache import (
    CachedManifestProvider,
    CachedSourceManifestProvider,
    sanitize_and_truncate_docs,
    sanitize_xml,
)
from rosdistro.manifest_provider.git import git_manifest_provider, git_source_manifest_provider
from rosdistro.manifest_provider.gitlab import gitlab_manifest_provider, gitlab_source_manifest_provider
from rosdistro.manifest_provider.tar import tar_manifest_provider, tar_source_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification
from rosdistro.distribution_cache import CacheConfig


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
        # Raw provider returns raw content without truncating at 100 lines
        repo_cache = gitlab_source_manifest_provider(_tracetools_analysis_source_repo())
        assert repo_cache.ref() == 'cd30853005ef3a591cb8594b4aa49f9ef400d30f'
        assert repo_cache.stars() == 17
        assert repo_cache.description() == 'Tools for analysing trace data'
        assert repo_cache.tags() == ['ros2', 'tracing']
        assert list(repo_cache.keys()) == ['ros2trace_analysis']
        pkg_entry = repo_cache['ros2trace_analysis']
        assert pkg_entry['package_path'] == 'ros2trace_analysis'
        assert '<version>1.0.3</version>' in pkg_entry['package.xml']
        assert pkg_entry['CHANGELOG.rst'] == long_changelog
        assert pkg_entry['README.md'] == readme_text

        # CachedSourceManifestProvider truncates according to CacheConfig (default max_lines=100)
        class FakeCache(object):
            def __init__(self, config):
                self.config = config
                self.release_resources = {}
                self.source_repo_resources = {}

        fc_100 = FakeCache(CacheConfig.extended())
        cached_mp_100 = CachedSourceManifestProvider(fc_100, [gitlab_source_manifest_provider])
        cached_repo_100 = cached_mp_100(_tracetools_analysis_source_repo())
        assert 'Truncated content at 100 of 120 lines' in cached_repo_100['ros2trace_analysis']['CHANGELOG.rst']
        assert cached_repo_100['ros2trace_analysis']['README.md'] == '# ros2trace_analysis\nSome documentation'

        # With max_lines=150 (> 100) or max_lines=None, all 120 lines are preserved!
        fc_150 = FakeCache(CacheConfig({'source_package_resources': {'CHANGELOG.rst': {'max_lines': 150}}}))
        cached_mp_150 = CachedSourceManifestProvider(fc_150, [gitlab_source_manifest_provider])
        cached_repo_150 = cached_mp_150(_tracetools_analysis_source_repo())
        assert 'Truncated content' not in cached_repo_150['ros2trace_analysis']['CHANGELOG.rst']
        assert len(cached_repo_150['ros2trace_analysis']['CHANGELOG.rst'].splitlines()) == 120

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

        # Missing optional file returns None
        assert tar_manifest_provider(
            'melodic', _genmsg_release_tarball(), 'genmsg', filepath='CHANGELOG.rst'
        ) is None

        # Missing package.xml raises RuntimeError
        with pytest.raises(RuntimeError, match='Could not find'):
            tar_manifest_provider(
                'melodic', _genmsg_release_tarball(), 'nonexistent_pkg', filepath='package.xml')


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
        # Raw provider returns raw content
        repo_cache = tar_source_manifest_provider(_genmsg_source_tarball())
        assert repo_cache.ref() is None
        pkg_entry = repo_cache['genmsg']
        assert pkg_entry['package_path'] == 'genmsg-0.5.16'
        assert '<version>0.5.16</version>' in pkg_entry['package.xml']
        assert pkg_entry['CHANGELOG.rst'] == long_changelog
        assert pkg_entry['README.md'] == readme_text

        # CachedSourceManifestProvider truncates according to CacheConfig
        class FakeCache(object):
            def __init__(self):
                self.config = CacheConfig.extended()
                self.release_resources = {}
                self.source_repo_resources = {}

        cached_repo = CachedSourceManifestProvider(
            FakeCache(), [tar_source_manifest_provider]
        )(_genmsg_source_tarball())
        assert 'Truncated content at 100 of 120 lines' in cached_repo['genmsg']['CHANGELOG.rst']
        assert cached_repo['genmsg']['README.md'] == '# genmsg\nMessage generation package'

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


def test_github_manifest_provider_404_and_cached_manifest_provider_no_git_fallback():
    repo = _genmsg_release_repo()

    def fake_get_url(url):
        raise HTTPError(url, 404, 'Not Found', {}, None)

    with patch.object(repo, 'has_remote_tag', return_value=True), \
         patch('rosdistro.manifest_provider.github._get_url_contents', side_effect=fake_get_url):
        res = rosdistro.manifest_provider.github.github_manifest_provider(
            'melodic', repo, 'genmsg', filepath='README.md'
        )
        assert res is None

    # Verify CachedManifestProvider records None on 404 without falling back to git_manifest_provider
    class FakeDistributionCache(object):
        def __init__(self):
            self.release_resources = {'genmsg': {'package.xml': '<package/>'}}

    dc = FakeDistributionCache()
    git_mock = MagicMock(side_effect=AssertionError('Should not fall back to git_manifest_provider'))
    github_calls = []

    def fake_github_mp(dist_name, repo, pkg_name, filepath='package.xml'):
        github_calls.append((pkg_name, filepath))
        return None

    cached_mp = CachedManifestProvider(dc, [fake_github_mp, git_mock])
    assert cached_mp('melodic', repo, 'genmsg', 'README.md') is None
    assert dc.release_resources['genmsg']['README.md'] is None
    assert len(github_calls) == 1

    # Second call should be a cache hit and not call fake_github_mp again
    assert cached_mp('melodic', repo, 'genmsg', 'README.md') is None
    assert len(github_calls) == 1


def test_github_source_metadata_assets_and_ignore_markers():
    long_contributing = '\n'.join(['Contributing line %d' % i for i in range(1, 121)])
    root_readme = '# Multi-pkg Repository Root README   \n'
    pkg_a_xml = (
        '<?xml version="1.0"?><package format="2">'
        '<name>pkg_a</name><version>1.0.0</version>'
        '<description>pkg_a</description><maintainer email="a@example.com">a</maintainer>'
        '<license>BSD</license></package>'
    )
    tree_payload = {
        'sha': '1111222233334444555566667777888899990000',
        'truncated': False,
        'tree': [
            {'path': 'README.md', 'type': 'blob'},
            {'path': 'CONTRIBUTING.rst', 'type': 'blob'},
            {'path': 'pkg_a/package.xml', 'type': 'blob'},
            {'path': 'pkg_a/msg/Alpha.msg', 'type': 'blob'},
            {'path': 'pkg_a/msg/sub/Beta.msg', 'type': 'blob'},
            {'path': 'pkg_a/srv/Query.srv', 'type': 'blob'},
            {'path': 'pkg_a/launch/start.launch.py', 'type': 'blob'},
            {'path': 'pkg_a/launch/legacy.xml', 'type': 'blob'},
            {'path': 'pkg_a/config/other.yaml', 'type': 'blob'},
            # Nested package inside pkg_a: its package.xml and assets must not be indexed under pkg_a
            {'path': 'pkg_a/test/fixture_pkg/package.xml', 'type': 'blob'},
            {'path': 'pkg_a/test/fixture_pkg/msg/Fixture.msg', 'type': 'blob'},
            {'path': 'pkg_a/test/fixture_pkg/launch/fixture.launch.py', 'type': 'blob'},
            # Ignored subdirectory inside pkg_a
            {'path': 'pkg_a/experimental/CATKIN_IGNORE', 'type': 'blob'},
            {'path': 'pkg_a/experimental/msg/IgnoredSub.msg', 'type': 'blob'},
            # Ignored sibling packages using different ignore markers
            {'path': 'ignored_colcon/COLCON_IGNORE', 'type': 'blob'},
            {'path': 'ignored_colcon/package.xml', 'type': 'blob'},
            {'path': 'ignored_ament/AMENT_IGNORE', 'type': 'blob'},
            {'path': 'ignored_ament/package.xml', 'type': 'blob'},
            {'path': 'ignored_rosindex/.rosindex_ignore', 'type': 'blob'},
            {'path': 'ignored_rosindex/sub/package.xml', 'type': 'blob'},
        ],
    }
    requested_urls = []

    def fake_get_url(req):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        requested_urls.append(url)
        if '/git/trees/' in url:
            return json.dumps(tree_payload)
        if url.endswith('/repos/ros/genmsg'):
            return json.dumps({
                'stargazers_count': 88,
                'description': 'Multi-package repo',
                'topics': ['ros2', 'msgs'],
                'pushed_at': '2026-03-15T08:30:00Z',
            })
        if url.endswith('/CONTRIBUTING.rst'):
            return long_contributing
        if url.endswith('/README.md') and '/pkg_a/' not in url:
            return root_readme
        if url.endswith('/pkg_a/package.xml'):
            return pkg_a_xml
        if url.endswith('/pkg_a/CHANGELOG.rst') or url.endswith('/pkg_a/README.md'):
            raise HTTPError(url, 404, 'Not Found', {}, None)
        raise AssertionError('Unexpected GitHub URL: %s' % url)

    with patch('rosdistro.manifest_provider.github._get_url_contents', side_effect=fake_get_url):
        repo_cache = rosdistro.manifest_provider.github.github_source_manifest_provider(
            _genmsg_source_repo()
        )
        assert list(repo_cache.keys()) == ['pkg_a']
        assert repo_cache.stars() == 88
        assert repo_cache.description() == 'Multi-package repo'
        assert repo_cache.tags() == ['ros2', 'msgs']
        assert repo_cache.last_commit_time() == '2026-03-15T08:30:00Z'
        assert repo_cache.readme() == '# Multi-pkg Repository Root README'
        assert 'Truncated content at 100 of 120 lines' in repo_cache.contributing()
        # Verify CONTRIBUTING.md was NOT requested because it was not in root_files
        assert not any(u.endswith('/CONTRIBUTING.md') for u in requested_urls)

        assert repo_cache.msg_files('pkg_a') == ['msg/Alpha.msg', 'msg/sub/Beta.msg']
        assert repo_cache.srv_files('pkg_a') == ['srv/Query.srv']
        assert repo_cache.launch_files('pkg_a') == ['launch/legacy.xml', 'launch/start.launch.py']

    # Repository root ignore marker skips indexing all packages in the repository
    root_ignored_tree = {
        'sha': '1111222233334444555566667777888899990000',
        'truncated': False,
        'tree': [
            {'path': '.rosindex_ignore', 'type': 'blob'},
            {'path': 'pkg_a/package.xml', 'type': 'blob'},
        ],
    }
    with patch(
        'rosdistro.manifest_provider.github._get_url_contents',
        side_effect=lambda req: json.dumps(root_ignored_tree)
        if '/git/trees/' in (req.get_full_url() if hasattr(req, 'get_full_url') else str(req))
        else '{}',
    ):
        ignored_cache = rosdistro.manifest_provider.github.github_source_manifest_provider(
            _genmsg_source_repo()
        )
        assert list(ignored_cache.keys()) == []


def test_gitlab_source_metadata_assets_and_ignore_markers():
    def fake_urlopen(req):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if '/repository/commits?' in url:
            return _make_mock_http_response(json.dumps([{
                'id': 'cd30853005ef3a591cb8594b4aa49f9ef400d30f',
                'committed_date': '2026-02-20T14:00:00Z',
            }]))
        if '/repository/tree?' in url:
            return _make_mock_http_response(json.dumps([
                {'path': 'README.rst', 'type': 'blob'},
                {'path': 'CONTRIBUTING.md', 'type': 'blob'},
                {'path': 'ros2trace_analysis/package.xml', 'type': 'blob'},
                {'path': 'ros2trace_analysis/msg/TraceEvent.msg', 'type': 'blob'},
                {'path': 'ros2trace_analysis/srv/Analyze.srv', 'type': 'blob'},
                {'path': 'ros2trace_analysis/launch/trace.launch.xml', 'type': 'blob'},
                {'path': 'ignored_dir/COLCON_IGNORE', 'type': 'blob'},
                {'path': 'ignored_dir/package.xml', 'type': 'blob'},
            ]))
        if url.endswith('/projects/ros-tracing%2Ftracetools_analysis/'):
            return _make_mock_http_response(json.dumps({
                'star_count': 17,
                'description': 'Tools for analysing trace data',
                'tag_list': ['ros2', 'tracing'],
                'last_activity_at': '2026-02-21T00:00:00Z',
            }))
        if 'repository/files/CONTRIBUTING.md/raw' in url:
            return _make_mock_http_response('# GitLab Contributing\n')
        if 'repository/files/README.rst/raw' in url:
            return _make_mock_http_response('GitLab Root README\n')
        if 'ros2trace_analysis%2Fpackage.xml/raw' in url:
            return _make_mock_http_response(_SAMPLE_GITLAB_PACKAGE_XML)
        if 'ros2trace_analysis%2FCHANGELOG.rst/raw' in url or 'ros2trace_analysis%2FREADME.md/raw' in url:
            raise HTTPError(url, 404, 'Not Found', {}, None)
        raise AssertionError('Unexpected GitLab URL: %s' % url)

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=fake_urlopen):
        repo_cache = gitlab_source_manifest_provider(_tracetools_analysis_source_repo())
        assert list(repo_cache.keys()) == ['ros2trace_analysis']
        assert repo_cache.last_commit_time() == '2026-02-20T14:00:00Z'
        assert repo_cache.contributing() == '# GitLab Contributing'
        assert repo_cache.readme() == 'GitLab Root README'
        assert repo_cache.msg_files('ros2trace_analysis') == ['msg/TraceEvent.msg']
        assert repo_cache.srv_files('ros2trace_analysis') == ['srv/Analyze.srv']
        assert repo_cache.launch_files('ros2trace_analysis') == ['launch/trace.launch.xml']


def test_git_and_tar_source_metadata_assets_and_ignore_markers(tmp_path):
    from contextlib import contextmanager

    repo_dir = tmp_path / 'local_repo'
    repo_dir.mkdir()
    (repo_dir / 'README.md').write_text('# Local Repo Root README\n')
    (repo_dir / 'CONTRIBUTING.md').write_text('# Local Contributing Guide\n')

    pkg_dir = repo_dir / 'my_pkg'
    (pkg_dir / 'msg').mkdir(parents=True)
    (pkg_dir / 'srv').mkdir(parents=True)
    (pkg_dir / 'launch').mkdir(parents=True)
    (pkg_dir / '.hidden').mkdir(parents=True)
    (pkg_dir / 'package.xml').write_text(_SAMPLE_TAR_PACKAGE_XML)
    (pkg_dir / 'msg' / 'State.msg').write_text('int32 state\n')
    (pkg_dir / 'srv' / 'Trigger.srv').write_text('---\nbool ok\n')
    (pkg_dir / 'launch' / 'bringup.launch.py').write_text('# launch\n')
    (pkg_dir / '.hidden' / 'Hidden.msg').write_text('int32 hidden\n')

    ignored_dir = repo_dir / 'ignored_pkg'
    ignored_dir.mkdir()
    (ignored_dir / 'CATKIN_IGNORE').write_text('')
    (ignored_dir / 'package.xml').write_text(_SAMPLE_GITLAB_PACKAGE_XML)

    @contextmanager
    def fake_temp_git_clone(url, ref):
        yield str(repo_dir)

    class FakeGit:
        def __init__(self, cwd=None):
            self.cwd = cwd

        def command(self, *args):
            if args == ('rev-parse', 'HEAD'):
                return {'returncode': 0, 'output': 'deadbeef1234567890'}
            if args == ('log', '-1', '--format=%cI', 'HEAD'):
                return {'returncode': 0, 'output': '2026-04-05T16:45:00+00:00\n'}
            raise AssertionError('Unexpected git command: %r' % (args,))

    with patch('rosdistro.manifest_provider.git._temp_git_clone', fake_temp_git_clone), \
         patch('rosdistro.manifest_provider.git.Git', FakeGit):
        git_cache = git_source_manifest_provider(_genmsg_source_repo())
        assert list(git_cache.keys()) == ['genmsg']
        assert git_cache.ref() == 'deadbeef1234567890'
        assert git_cache.last_commit_time() == '2026-04-05T16:45:00+00:00'
        assert git_cache.contributing() == '# Local Contributing Guide'
        assert git_cache.readme() == '# Local Repo Root README'
        assert git_cache.msg_files('genmsg') == ['msg/State.msg']
        assert git_cache.srv_files('genmsg') == ['srv/Trigger.srv']
        assert git_cache.launch_files('genmsg') == ['launch/bringup.launch.py']

    # Also verify tar_source_manifest_provider on the same structure
    tar_bytes = _build_tar_bytes({
        'README.md': '# Tar Root README\n',
        'CONTRIBUTING.md': '# Tar Contributing\n',
        'my_pkg/package.xml': _SAMPLE_TAR_PACKAGE_XML,
        'my_pkg/msg/State.msg': 'int32 state\n',
        'my_pkg/srv/Trigger.srv': '---\nbool ok\n',
        'my_pkg/launch/bringup.launch.py': '# launch\n',
        'ignored_pkg/CATKIN_IGNORE': '',
        'ignored_pkg/package.xml': _SAMPLE_GITLAB_PACKAGE_XML,
    })
    with patch('rosdistro.manifest_provider.tar.urlopen',
               side_effect=lambda req: _make_mock_http_response(tar_bytes)):
        tar_cache = tar_source_manifest_provider(_genmsg_source_tarball())
        assert list(tar_cache.keys()) == ['genmsg']
        assert tar_cache.contributing() == '# Tar Contributing'
        assert tar_cache.readme() == '# Tar Root README'
        assert tar_cache.msg_files('genmsg') == ['msg/State.msg']
        assert tar_cache.srv_files('genmsg') == ['srv/Trigger.srv']
        assert tar_cache.launch_files('genmsg') == ['launch/bringup.launch.py']

        # Verify CachedSourceManifestProvider preserves list-valued resources
        class FakeCache(object):
            def __init__(self):
                self.config = CacheConfig.extended()
                self.release_resources = {}
                self.source_repo_resources = {}

        cached_repo = CachedSourceManifestProvider(
            FakeCache(), [tar_source_manifest_provider]
        )(_genmsg_source_tarball())
        assert cached_repo.msg_files('genmsg') == ['msg/State.msg']
        assert cached_repo.srv_files('genmsg') == ['srv/Trigger.srv']
        assert cached_repo.launch_files('genmsg') == ['launch/bringup.launch.py']
        assert cached_repo.contributing() == '# Tar Contributing'
        assert cached_repo.readme() == '# Tar Root README'



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
