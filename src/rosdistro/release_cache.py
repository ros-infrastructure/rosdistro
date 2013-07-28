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


class ReleaseCache(object):

    _type = 'cache'

    def __init__(self, name, data=None, rel_file_data=None):
        assert data or rel_file_data
        if data:
            assert 'type' in data, "Expected file type is '%s'" % ReleaseCache._type
            assert data['type'] == ReleaseCache._type, "Expected file type is '%s', not '%s'" % (ReleaseCache._type, data['type'])

            assert 'version' in data, "Release cache file for '%s' lacks required version information" % name
            self.version = int(data['version'])

            assert 'name' in data, "Release cache file for '%s' lacks required name information" % name
            assert data['name'] == name, "Release cache file for '%s' does not match the name '%s'" % (name, data['name'])
        else:
            self.version = rel_file_data['version']
        assert int(self.version) == 1, "Unable to handle '%s' format version '%d', please update rosdistro" % (ReleaseCache._type, int(self.version))

        self._rel_file_data = data['release_file'] if data else rel_file_data
        self.release_file = ReleaseFile(name, self._rel_file_data)
        self.package_xmls = data['package_xmls'] if data else {}

    def get_data(self):
        data = {}
        data['type'] = 'cache'
        data['version'] = '1'
        data['name'] = self.release_file.name
        data['release_file'] = self._rel_file_data
        data['package_xmls'] = self.package_xmls
        return data

    def update_distribution(self, rel_file_data):
        # remove packages which are not in the old distribution file
        self._remove_obsolete_entries()

        self._rel_file_data = rel_file_data
        rel_file = ReleaseFile(self.release_file.name, self._rel_file_data)

        # remove all package xmls if repository information has changed
        for pkg_name in sorted(rel_file.packages.keys()):
            if pkg_name not in self.release_file.packages:
                continue
            if pkg_name in self.package_xmls and self._get_repo_info(rel_file, pkg_name) != self._get_repo_info(self.release_file, pkg_name):
                del self.package_xmls[pkg_name]

        self.release_file = rel_file
        # remove packages which are not in the new distribution file
        self._remove_obsolete_entries()

    def _get_repo_info(self, dist_file, pkg_name):
        pkg = dist_file.packages[pkg_name]
        repo = dist_file.repositories[pkg.repository_name]
        return (repo.version, repo.url)

    def _remove_obsolete_entries(self):
        for pkg_name in self.package_xmls.keys():
            if pkg_name not in self.release_file.packages:
                print('- REMOVE', pkg_name)
                del self.package_xmls[pkg_name]
