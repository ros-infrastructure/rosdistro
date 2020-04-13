# -*- coding: utf-8 -*-

import mock

import rosdistro.manifest_provider.github
import rosdistro.vcs

from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedManifestProvider, sanitize_xml
from rosdistro.manifest_provider.git import git_manifest_provider, git_source_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification


def test_bitbucket():
    assert '</package>' in bitbucket_manifest_provider('indigo', _rospeex_release_repo(), 'rospeex_msgs')


def test_cached():
    class FakeDistributionCache(object):
        def __init__(self):
            self.release_package_xmls = {}
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

    package_path, package_xml = repo_cache['genmsg']
    assert '' == package_path
    assert '<version>0.5.11</version>' in package_xml


# mock_get_url_contents is used to mock out the '_get_url_contents' method in
# the rosdistro.manifest_provider.github module.  Instead of going out to github
# to get data (which can get rate-limited in Travis), it instead pulls a canned
# example from some local files.
def mock_get_url_contents(req):
    import re

    # For python3, look for the 'str' type; for Python 2, the 'unicode' type
    try:
        text_type = unicode
    except NameError:
        text_type = str

    # The urlopen() function from urllib or urllib2 takes either a string or a
    # urllib.Request object in; determine the URL in either case.
    if isinstance(req, text_type):
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
    else:
        fname = 'test/github-tree-data.json'

    with open(fname, 'r') as infp:
        data = infp.read()

    return data


@mock.patch('rosdistro.manifest_provider.github._get_url_contents', mock_get_url_contents)
def test_github_source():
    repo_cache = rosdistro.manifest_provider.github.github_source_manifest_provider(_genmsg_source_repo())

    # This hash corresponds to the 0.5.7 tag.
    assert repo_cache.ref() == '81b66fe5eb00043c43894ddeee07e738d9b9712f'

    package_path, package_xml = repo_cache['genmsg']
    assert '' == package_path
    assert '<version>0.5.11</version>' in package_xml


def test_git_source_multi():
    repo_cache = git_source_manifest_provider(_ros_source_repo())
    assert repo_cache.ref()
    package_path, package_xml = repo_cache['roslib']
    assert package_path == 'core/roslib'


def test_tar_source():
    repo_cache = rosdistro.manifest_provider.tar.tar_source_manifest_provider(_genmsg_source_tarball())

    assert repo_cache.ref() is None

    package_path, package_xml = repo_cache['genmsg']
    assert 'genmsg-0.5.16' == package_path
    assert '<version>0.5.16</version>' in package_xml


def test_sanitize():
    assert '<a>abc</a>' in sanitize_xml('<a>ab<!-- comment -->c</a>')
    assert '<a><b/><c>ab c</c></a>' in sanitize_xml('<a><b> </b>  <c>  ab  c  </c></a>')

    # This unicode check should be valid on both Python 2 and 3.
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
