# -*- coding: utf-8 -*-

import os

from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedManifestProvider, sanitize_xml
from rosdistro.manifest_provider.git import git_manifest_provider
from rosdistro.manifest_provider.github import github_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification

import rosdistro.vcs


def test_bitbucket():
    assert '</package>' in bitbucket_manifest_provider('indigo', _rospeex_repo(), 'rospeex_msgs')


def test_cached():
    class FakeDistributionCache(object):
        def __init__(self):
            self.release_package_xmls = {}
    dc = FakeDistributionCache()
    cache = CachedManifestProvider(dc, [github_manifest_provider])
    assert '</package>' in cache('kinetic', _genmsg_repo(), 'genmsg')


def test_git():
    assert '</package>' in git_manifest_provider('kinetic', _genmsg_repo(), 'genmsg')


def test_git_legacy():
    rosdistro.vcs.Git._client_version = '1.7.0'
    assert '</package>' in git_manifest_provider('kinetic', _genmsg_repo(), 'genmsg')
    rosdistro.vcs.Git._client_version = None


def test_github():
    assert '</package>' in github_manifest_provider('kinetic', _genmsg_repo(), 'genmsg')


def test_sanitize():
    assert '<a>abc</a>' in sanitize_xml('<a>ab<!-- comment -->c</a>')
    assert '<a><b></b><c>ab c</c></a>' in sanitize_xml('<a><b> </b>  <c>  ab  c  </c></a>')

    # This unicode check should be valid on both Python 2 and 3.
    assert '<a>français</a>' in sanitize_xml('<a> français  </a>')


def _genmsg_repo():
    return ReleaseRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros-gbp/genmsg-release.git',
        'tags': {'release': 'release/kinetic/{package}/{version}'},
        'version': '0.5.7-1'
    })


def _rospeex_repo():
    return ReleaseRepositorySpecification('rospeex', {
        'packages': ['rospeex', 'rospeex_msgs'],
        'tags': {'release': 'release/indigo/{package}/{version}'},
        'url': 'https://bitbucket.org/rospeex/rospeex-release.git',
        'version': '2.14.7-0'
    })
