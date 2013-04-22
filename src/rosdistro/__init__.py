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

from doc_build_file import DocBuildFile
from doc_file import DocFile
from external.appdirs import user_config_dir, site_config_dir
from index import Index
from loader import load_url
from manifest_provider.cache import CachedManifestProvider
from release import Release
from release_build import ReleaseBuild
from release_build_file import ReleaseBuildFile
from release_cache import ReleaseCache
from release_file import ReleaseFile
from source_build_file import SourceBuildFile
from source_file import SourceFile

### index information


#DEFAULT_INDEX_URL = 'https://raw.github.com/ros/rosdistro/rep137/index.yaml'
DEFAULT_INDEX_URL = 'file:///home/dthomas/wg/github/ros/rosdistro/index.yaml'


def get_index_url():
    # environment variable has precedence over configuration files
    if 'ROSDISTRO_INDEX_URL' in os.environ:
        return os.environ['ROSDISTRO_INDEX_URL']

    def read_cfg_index_url(fname):
        try:
            with open(fname) as f:
                return yaml.load(f.read())['index_url']
        except (IOError, KeyError, yaml.YAMLError):
            return None

    cfg_file = 'config.yaml'

    # first, look for the user configuration (usually ~/.config/rosdistro)
    user_cfg_path = os.path.join(user_config_dir('rosdistro'), cfg_file)
    index_url = read_cfg_index_url(user_cfg_path)
    if index_url is not None:
        return index_url

    # if not found, look for the global configuration *usually /etc/xdg/rosdistro)
    site_cfg_paths = os.path.join(site_config_dir('rosdistro', multipath=True), cfg_file).split(os.pathsep)
    for site_cfg_path in site_cfg_paths:
        index_url = read_cfg_index_url(site_cfg_path)
        if index_url is not None:
            return index_url

    # if nothing is found, use the default (provided by OSRF)
    return DEFAULT_INDEX_URL


def get_index(url):
    logger.debug('Load index from "%s"' % url)
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    base_url = os.path.dirname(url)
    return Index(data, base_url)


### release information


def get_cached_release(index, dist_name, cache=None, allow_lazy_load=False):
    if cache is None:
        try:
            cache = get_release_cache(index, dist_name)
        except Exception:
            if not allow_lazy_load:
                raise
            # create empty cache instance
            rel_file_data = _get_dist_file_data(index, dist_name, 'release')
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
    data = _get_dist_file_data(index, dist_name, 'release')
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


def get_release_builds(index, release_file):
    build_files = get_release_build_files(index, release_file.name)
    builds = []
    for build_file in build_files:
        builds.append(ReleaseBuild(release_file, build_file))
    return builds


def get_release_build_files(index, dist_name):
    data = _get_dist_file_data(index, dist_name, 'release_builds')
    build_files = []
    for d in data:
        build_files.append(ReleaseBuildFile(dist_name, d))
    return build_files


### source information


def get_source_file(index, dist_name):
    data = _get_dist_file_data(index, dist_name, 'source')
    return SourceFile(dist_name, data)


def get_source_build_files(index, dist_name):
    data = _get_dist_file_data(index, dist_name, 'source_builds')
    build_files = []
    for d in data:
        build_files.append(SourceBuildFile(dist_name, d))
    return build_files


### doc information


def get_doc_file(index, dist_name):
    data = _get_dist_file_data(index, dist_name, 'doc')
    return DocFile(dist_name, data)


def get_doc_build_files(index, dist_name):
    data = _get_dist_file_data(index, dist_name, 'doc_builds')
    build_files = []
    for d in data:
        build_files.append(DocBuildFile(dist_name, d))
    return build_files


### internal


def _get_dist_file_data(index, dist_name, type_):
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
