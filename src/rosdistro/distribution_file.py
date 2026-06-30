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

from .package import Package
from .repository import Repository


class DistributionFile(object):

    _type = 'distribution'

    def __init__(self, name, data):
        self.name = name

        assert 'type' in data, "Expected file type is '%s'" % DistributionFile._type
        assert data['type'] == DistributionFile._type, "Expected file type is '%s', not '%s'" % (DistributionFile._type, data['type'])

        assert 'version' in data, "Source file for '%s' lacks required version information" % self.name
        assert int(data['version']) in [1, 2, 3], "Unable to handle '%s' format version '%d', please update rosdistro (e.g. on Ubuntu/Debian use: sudo apt-get update && sudo apt-get install --only-upgrade python-rosdistro)" % (DistributionFile._type, int(data['version']))
        self.version = int(data['version'])

        self.repositories = {}
        self.release_packages = {}
        self.source_packages = {}
        if 'repositories' in data and data['repositories']:
            for repo_name in sorted(data['repositories'].keys()):
                repo_data = data['repositories'][repo_name]
                repo = Repository(repo_name, repo_data.get('doc', None), repo_data.get('release', None), repo_data.get('source', None), repo_data)
                repo.origin_distro = self.name
                repo.extension_method = None
                if repo.release_repository:
                    repo.release_repository.origin_distro = self.name
                    repo.release_repository.extension_method = None
                self.repositories[repo_name] = repo

                if repo.release_repository:
                    for pkg_name in repo.release_repository.package_names:
                        self._add_package(pkg_name, repo)

                if repo.doc_repository:
                    for dep in repo.doc_repository.depends:
                        assert dep in data['repositories'].keys(), "Doc repository '%s' depends on non-existing repository '%s'" % (repo_name, dep)

        self.release_platforms = {}
        if 'release_platforms' in data and data['release_platforms']:
            for os_name in data['release_platforms'].keys():
                self.release_platforms[os_name] = []
                for os_code_name in data['release_platforms'][os_name]:
                    assert os_code_name not in self.release_platforms[os_name], "Distribution '%s' specifies the os_code_name '%s' multiple times for the os_name '%s'" % (self.name, os_code_name, os_name)
                    self.release_platforms[os_name].append(os_code_name)

        self.tags = []
        if 'tags' in data and data['tags']:
            for tag in data['tags']:
                self.tags.append(tag)

        self.extends = []
        if 'extends' in data and data['extends']:
            assert self.version >= 3, "'extends' element is only supported in distribution version >= 3"
            for ext in data['extends']:
                assert 'distro_name' in ext, "Extends element must have 'distro_name'"
                assert 'extension_method' in ext, "Extends element must have 'extension_method'"
                assert ext['extension_method'] in ('binary_import', 'source_rebuild'), "Extension method must be 'binary_import' or 'source_rebuild'"
                self.extends.append({
                    'distro_name': ext['distro_name'],
                    'index_url': ext.get('index_url', None),
                    'extension_method': ext['extension_method']
                })

        self.dependencies = []
        if 'dependencies' in data and data['dependencies']:
            assert self.version >= 3, "'dependencies' element is only supported in distribution version >= 3"
            for dep in data['dependencies']:
                self.dependencies.append({
                    'rosdep_sources_list_urls': dep.get('rosdep_sources_list_urls', []),
                    'rosdep_minimum_target_platforms': dep.get('rosdep_minimum_target_platforms', [])
                })

    def merge(self, other_dist_file):
        assert self.name == other_dist_file.name
        assert self.version == other_dist_file.version
        # assert that the release platforms of the other dist file are a subset
        for os_name, os_code_names in \
                other_dist_file.release_platforms.items():
            assert os_name in self.release_platforms.keys()
            for os_code_name in os_code_names:
                assert os_code_name in self.release_platforms[os_name]
        self.release_platforms = dict(other_dist_file.release_platforms)

        for repo_name, other_repo in other_dist_file.repositories.items():
            # remove existing repo before adding other
            if repo_name in self.repositories:
                self_repo = self.repositories[repo_name]
                # remove corresponding release packages
                if self_repo.release_repository:
                    for pkg_name in self_repo.release_repository.package_names:
                        del self.release_packages[pkg_name]
            self.repositories[repo_name] = other_repo
            if other_repo.release_repository:
                for pkg_name in other_repo.release_repository.package_names:
                    # add corresponding release packages
                    self.release_packages[pkg_name] = \
                        other_dist_file.release_packages[pkg_name]
        for tag in other_dist_file.tags:
            if tag not in self.tags:
                self.tags.append(tag)

    def _add_package(self, pkg_name, repo):
        assert pkg_name not in self.release_packages, "Duplicate package name '%s' exists in repository '%s' as well as in repository '%s'" % (pkg_name, repo.name, self.release_packages[pkg_name].repository_name)
        self.release_packages[pkg_name] = Package(pkg_name, repo.name)

    def get_data(self):
        data = {}
        data['type'] = DistributionFile._type
        data['version'] = self.version
        data['repositories'] = {}
        for repo_name in sorted(self.repositories.keys()):
            repo = self.repositories[repo_name]
            data['repositories'][repo_name] = repo.get_data()
        data['release_platforms'] = self.release_platforms
        if self.tags:
            data['tags'] = self.tags
        if self.extends:
            data['extends'] = self.extends
        if self.dependencies:
            data['dependencies'] = self.dependencies
        return data

    def merge_extends(self, parent_dist_file, extension_method):
        # Validate target platform compatibility
        for os_name, os_code_names in self.release_platforms.items():
            if os_name not in parent_dist_file.release_platforms:
                for codename in os_code_names:
                    print("WARNING: Target platform '%s:%s' specified in derived distribution is not supported by base distribution." % (os_name, codename), flush=True)
            else:
                parent_codenames = parent_dist_file.release_platforms[os_name]
                for codename in os_code_names:
                    if codename not in parent_codenames:
                        print("WARNING: Target platform '%s:%s' specified in derived distribution is not supported by base distribution." % (os_name, codename), flush=True)

        # Merge repositories (child takes precedence over parent)
        for repo_name, parent_repo in parent_dist_file.repositories.items():
            if repo_name not in self.repositories:
                if not hasattr(parent_repo, 'origin_distro') or not parent_repo.origin_distro:
                    parent_repo.origin_distro = parent_dist_file.name
                parent_repo.extension_method = extension_method
                if parent_repo.release_repository:
                    if not hasattr(parent_repo.release_repository, 'origin_distro') or not parent_repo.release_repository.origin_distro:
                        parent_repo.release_repository.origin_distro = parent_repo.origin_distro
                    parent_repo.release_repository.extension_method = extension_method
                self.repositories[repo_name] = parent_repo
                if parent_repo.release_repository:
                    for pkg_name in parent_repo.release_repository.package_names:
                        if pkg_name not in self.release_packages:
                            self._add_package(pkg_name, parent_repo)


def create_distribution_file(dist_name, data):
    if not isinstance(data, list):
        return DistributionFile(dist_name, data)
    combined_dist_file = None
    for d in data:
        dist_file = DistributionFile(dist_name, d)
        if combined_dist_file is None:
            combined_dist_file = dist_file
        else:
            combined_dist_file.merge(dist_file)
    return combined_dist_file
