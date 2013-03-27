# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Open Source Robotics Foundation, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Open Source Robotics Foundation, Inc. nor
#    the names of its contributors may be used to endorse or promote
#    products derived from this software without specific prior
#    written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# legacy imports
import common
from rosdistro import walks
from rosdistro import RosDistro
from develdistro import DevelDistro
from aptdistro import AptDistro

import logging
import os
import yaml

logger = logging.getLogger('rosdistro')

from build import Build
from build_file import BuildFile
from index import Index
from loader import load_url
from manifest_provider.cache import CachedManifestProvider
from release import Release
from release_cache import ReleaseCache
from release_file import ReleaseFile
from test_file import TestFile


def get_index(url):
    logger.debug('Load index from "%s"' % url)
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    base_url = os.path.dirname(url)
    return Index(data, base_url)


def get_cached_release(index, dist_name, cache=None, allow_lazy_load=False):
    if cache is None:
        try:
            cache = get_release_cache(index, dist_name)
        except Exception:
            if not allow_lazy_load:
                raise
            # create empty cache instance
            rel_file_data = get_release_file_data(index, dist_name, 'release')
            cache = ReleaseCache(dist_name, rel_file_data=rel_file_data)
    rel = Release(
        cache.release_file,
        [CachedManifestProvider(cache, Release.default_manifest_providers if allow_lazy_load else None)])
    assert cache.release_file.name == dist_name
    return rel


def get_release(index, dist_name):
    rel_file = get_release_file(index, dist_name)
    return Release(rel_file)


def get_release_file(index, dist_name):
    data = get_release_file_data(index, dist_name, 'release')
    return ReleaseFile(dist_name, data)


def get_release_cache(index, dist_name):
    if dist_name not in index.distributions.keys():
        raise RuntimeError("Unknown release: '{0}'".format(dist_name))
    dist = index.distributions[dist_name]
    if 'release_cache' not in dist.keys():
        raise RuntimeError("Release has no cache: '{0}'".format(dist_name))
    url = dist['release_cache']

    logger.debug('Load cache from "%s"' % url)
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    return ReleaseCache(dist_name, data)


def get_test_file(index, dist_name):
    data = get_release_file_data(index, dist_name, 'test')
    return TestFile(dist_name, data)


def get_release_build_files(index, dist_name):
    return _get_build_files(index, dist_name, 'release_build')


def get_test_build_files(index, dist_name):
    return _get_build_files(index, dist_name, 'test_build')


def _get_build_files(index, dist_name, type_):
    data = get_release_file_data(index, dist_name, type_)
    build_files = []
    for d in data:
        build_files.append(BuildFile(dist_name, d))
    return build_files


def get_release_builds(index, dist):
    build_files = get_release_build_files(index, dist.name)
    return _get_builds(dist, build_files)


def get_test_builds(index, dist):
    build_files = get_test_build_files(index, dist.name)
    return _get_builds(dist, build_files)


def _get_builds(dist, build_files):
    builds = []
    for build_file in build_files:
        builds.append(Build(dist, build_file))
    return builds


def get_release_file_data(index, dist_name, type_):
    if dist_name not in index.distributions.keys():
        raise RuntimeError("Unknown release: '{0}'".format(dist_name))
    dist = index.distributions[dist_name]
    if type_ not in dist.keys():
        raise RuntimeError('unknown release type "%s"' % type_)
    url = dist[type_]

    def _load_yaml_data(url):
        logger.debug('Load file from "%s"' % url)
        yaml_str = load_url(url)
        return yaml.load(yaml_str)

    if not isinstance(url, list):
        data = _load_yaml_data(url)
    else:
        data = []
        for u in url:
            data.append(_load_yaml_data(u))
    return data
