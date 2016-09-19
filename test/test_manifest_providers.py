import os

from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.cache import CachedManifestProvider
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
    rosdistro.vcs._git_client_version = '1.7.0'
    assert '</package>' in git_manifest_provider('kinetic', _genmsg_repo(), 'genmsg')
    rosdistro.vcs._git_client_version = None


def test_github():
    assert '</package>' in github_manifest_provider('kinetic', _genmsg_repo(), 'genmsg')


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
