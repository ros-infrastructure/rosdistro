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

from .release_file import ReleaseFile


def _create_release_file(name, data):
    if not isinstance(data, list):
        return _create_single_release_file(name, data)
    combined_rel_file = None
    for d in data:
        rel_file = _create_single_release_file(name, d)
        if combined_rel_file is None:
            combined_rel_file = rel_file
        else:
            _merge_release_files(combined_rel_file, rel_file)
    return combined_rel_file


def _create_single_release_file(name, data):
    if isinstance(data, dict) and (
        ('repositories' in data and data['repositories'] is None) or
        ('release_platforms' in data and data['release_platforms'] is None)
    ):
        data = dict(data)
        if data.get('repositories') is None:
            data['repositories'] = {}
        if data.get('release_platforms') is None:
            data['release_platforms'] = {}
    return ReleaseFile(name, data)


def _merge_release_files(rel_file, other_rel_file):
    assert rel_file.name == other_rel_file.name
    assert rel_file.version == other_rel_file.version
    for os_name, os_code_names in other_rel_file.platforms.items():
        assert os_name in rel_file.platforms.keys()
        for os_code_name in os_code_names:
            assert os_code_name in rel_file.platforms[os_name]
    rel_file.platforms = dict(other_rel_file.platforms)

    for repo_name, other_repo in other_rel_file.repositories.items():
        if repo_name in rel_file.repositories:
            self_repo = rel_file.repositories[repo_name]
            for pkg_name in (self_repo.package_names or [repo_name]):
                if pkg_name in rel_file.packages:
                    del rel_file.packages[pkg_name]
        rel_file.repositories[repo_name] = other_repo
        for pkg_name in (other_repo.package_names or [repo_name]):
            rel_file.packages[pkg_name] = other_rel_file.packages[pkg_name]


class ReleaseCache(object):

    _type = 'cache'

    def __init__(self, name, data=None, distribution_file_data=None, rel_file_data=None):
        if distribution_file_data is None and rel_file_data is not None:
            distribution_file_data = rel_file_data
        assert data or distribution_file_data
        if data:
            assert 'type' in data, "Expected file type is '%s'" % ReleaseCache._type
            assert data['type'] == ReleaseCache._type, "Expected file type is '%s', not '%s'" % (ReleaseCache._type, data['type'])

            assert 'version' in data, "Release cache file for '%s' lacks required version information" % name
            self.version = int(data['version'])
            assert self.version > 1, "Unable to handle '%s' format version '%d' anymore, please update your '%s' file to version '2'" % (ReleaseCache._type, self.version, ReleaseCache._type)
            assert self.version <= 3, "Unable to handle '%s' format version '%d', please update rosdistro (e.g. on Ubuntu/Debian use: sudo apt-get update && sudo apt-get install --only-upgrade python-rosdistro)" % (ReleaseCache._type, self.version)

            assert 'name' in data, "Release cache file for '%s' lacks required name information" % name
            assert data['name'] == name, "Release cache file for '%s' does not match the name '%s'" % (name, data['name'])
        else:
            self.version = 2

        self._distribution_file_data = data['distribution_file'] if data else distribution_file_data
        self.release_file = _create_release_file(name, self._distribution_file_data)

        self.release_resources = {}
        self.package_xmls = {}
        if data:
            if 'release_resources' in data and data['release_resources']:
                for pkg_name, info in data['release_resources'].items():
                    if isinstance(info, dict):
                        self.release_resources[pkg_name] = dict(info)
                        if 'package.xml' in info:
                            self.package_xmls[pkg_name] = info['package.xml']
                    else:
                        self.release_resources[pkg_name] = info
            for key in ('release_package_xmls', 'package_xmls'):
                if key in data and data[key]:
                    for pkg_name, pkg_xml in data[key].items():
                        self.package_xmls[pkg_name] = pkg_xml
                        if pkg_name not in self.release_resources or not isinstance(self.release_resources[pkg_name], dict):
                            self.release_resources[pkg_name] = {}
                        self.release_resources[pkg_name]['package.xml'] = pkg_xml
        self._synced_package_xmls = dict(self.package_xmls)

    # for backward compatibility only
    def __getattr__(self, name):
        if name == 'release_package_xmls':
            return self.package_xmls
        raise AttributeError

    def _sync_resources(self):
        for pkg_name, pkg_xml in list(self.package_xmls.items()):
            if self._synced_package_xmls.get(pkg_name) != pkg_xml or pkg_name not in self.release_resources:
                if pkg_name not in self.release_resources or not isinstance(self.release_resources[pkg_name], dict):
                    self.release_resources[pkg_name] = {}
                self.release_resources[pkg_name]['package.xml'] = pkg_xml
        for pkg_name in list(self._synced_package_xmls.keys()):
            if pkg_name not in self.package_xmls:
                if pkg_name in self.release_resources and isinstance(self.release_resources[pkg_name], dict):
                    self.release_resources[pkg_name].pop('package.xml', None)
                    if not self.release_resources[pkg_name]:
                        del self.release_resources[pkg_name]
        for pkg_name, info in list(self.release_resources.items()):
            if isinstance(info, dict) and 'package.xml' in info:
                if self._synced_package_xmls.get(pkg_name) != info['package.xml'] or pkg_name not in self.package_xmls:
                    self.package_xmls[pkg_name] = info['package.xml']
        for pkg_name in list(self._synced_package_xmls.keys()):
            if pkg_name in self.package_xmls:
                info = self.release_resources.get(pkg_name)
                if not isinstance(info, dict) or 'package.xml' not in info:
                    del self.package_xmls[pkg_name]
        self._synced_package_xmls = dict(self.package_xmls)

    def get_data(self):
        self._sync_resources()
        data = {}
        data['type'] = 'cache'
        data['version'] = 2
        data['name'] = self.release_file.name
        data['distribution_file'] = self._distribution_file_data
        data['package_xmls'] = self.package_xmls  # backwards compatibility
        data['release_resources'] = self.release_resources
        return data

    def update_distribution(self, distribution_file_data):
        self._sync_resources()
        # remove packages which are not in the old distribution file
        self._remove_obsolete_entries()

        self._distribution_file_data = distribution_file_data
        rel_file = _create_release_file(self.release_file.name, self._distribution_file_data)

        # remove all package xmls if repository information has changed
        for pkg_name in sorted(rel_file.packages.keys()):
            if pkg_name not in self.release_file.packages:
                continue
            if self._get_repo_info(rel_file, pkg_name) != self._get_repo_info(self.release_file, pkg_name):
                if pkg_name in self.package_xmls:
                    del self.package_xmls[pkg_name]
                if pkg_name in self.release_resources:
                    del self.release_resources[pkg_name]

        self.release_file = rel_file
        # remove packages which are not in the new distribution file
        self._remove_obsolete_entries()
        self._synced_package_xmls = dict(self.package_xmls)

    def _get_repo_info(self, dist_file, pkg_name):
        pkg = dist_file.packages[pkg_name]
        repo = dist_file.repositories[pkg.repository_name]
        return (repo.version, repo.url)

    def _remove_obsolete_entries(self):
        for pkg_name in list(self.package_xmls.keys()):
            if pkg_name not in self.release_file.packages:
                print('- REMOVE', pkg_name)
                del self.package_xmls[pkg_name]
        for pkg_name in list(self.release_resources.keys()):
            if pkg_name not in self.release_file.packages:
                if pkg_name not in self._synced_package_xmls:
                    print('- REMOVE', pkg_name)
                del self.release_resources[pkg_name]
        self._synced_package_xmls = dict(self.package_xmls)
