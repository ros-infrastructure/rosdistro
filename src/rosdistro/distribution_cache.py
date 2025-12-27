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

import datetime
import sys
import time

from . import logger
from .distribution_file import create_distribution_file
from .package import Package
from .source_repository_cache import SourceRepositoryCache
from .vcs import Git, ref_is_hash


class DistributionCache(object):

    _type = 'cache'

    def __init__(self, name, data=None, distribution_file_data=None):
        assert data or distribution_file_data
        
        # default value
        inbound_version = 0
        if data:
            assert 'type' in data, "Expected file type is '%s'" % DistributionCache._type
            assert data['type'] == DistributionCache._type, "Expected file type is '%s', not '%s'" % (DistributionCache._type, data['type'])

            assert 'version' in data, "Distribution cache file for '%s' lacks required version information" % name
            inbound_version = int(data['version'])
            assert inbound_version > 1, "Unable to handle '%s' format version '%d' anymore, please update your '%s' file to version '2'" % (DistributionCache._type, inbound_version, DistributionCache._type)
            assert inbound_version <= 3, "Unable to handle '%s' format version '%d', please update rosdistro (e.g. on Ubuntu/Debian use: sudo apt-get update && sudo apt-get install --only-upgrade python-rosdistro)" % (DistributionCache._type, inbound_version)

            assert 'name' in data, "Distribution cache file for '%s' lacks required name information" % name
            assert data['name'] == name, "Distribution cache file for '%s' does not match the name '%s'" % (name, data['name'])

        # All data will be migrated forward on import, any rexport will be in version 3
        self.version = 3

        self._distribution_file_data = data['distribution_file'] if data else distribution_file_data
        self.distribution_file = create_distribution_file(name, self._distribution_file_data)

        # self.release_package_xmls = data['release_package_xmls'] if data and 'release_package_xmls' in data else {}
        # self.release_readmes = data['release_readmes'] if data and 'release_readmes' in data else {}
        # self.release_changelogs = data['release_changelogs'] if data and 'release_changelogs' in data else {}
        self.release_resources = data['release_resources'] if data and 'release_resources' in data else {}

        # Format 2 backards compatability
        # Convert release_package_xml from flat dict at the root to be an instance of a resource loaded
        if inbound_version == 2 and 'release_package_xmls' in data:
            for pkg_name, pkg_xml in data['release_package_xmls']:
                if not pkg_name in data['release_resources']:
                    data['release_resources'][pkg_name] = {}
                data['release_resources'][pkg_name]['package.xml'] = pkg_xml

        self.source_repo_resources = {}
        if data and 'source_repo_resources' in data:
            for repo_name, repo_data in data['source_repo_resources'].items():
                self.source_repo_resources[repo_name] = SourceRepositoryCache(repo_data)
        self.distribution_file.source_packages = self.get_source_packages()

    def get_data(self):
        data = {}
        data['type'] = 'cache'
        data['version'] = 3
        data['name'] = self.distribution_file.name
        data['distribution_file'] = self._distribution_file_data
        data['release_resources'] = self.release_resources
        data['source_repo_resources'] = dict([(repo_name, repo_cache.get_data())
            for repo_name, repo_cache in self.source_repo_resources.items()])
        return data

    def update_distribution(self, distribution_file_data):
        # remove packages which are not in the old distribution file
        self._remove_obsolete_entries()

        # determine differences in doc and source entries
        if len(distribution_file_data) == len(self._distribution_file_data):
            for old_data, new_data in zip(self._distribution_file_data, distribution_file_data):
                if not new_data['repositories']:
                    continue
                for repo_name in sorted(new_data['repositories'].keys()):
                    repo = new_data['repositories'][repo_name]
                    for section in ['doc', 'source']:
                        if section not in repo:
                            continue
                        if repo_name in (old_data['repositories'] or []) and \
                                section in old_data['repositories'][repo_name] and \
                                old_data['repositories'][repo_name][section] == repo[section]:
                            continue
                        # section is either different or does't exist before
                        print("  - updated '%s' entry for repository '%s'" % (section, repo_name))

        self._distribution_file_data = distribution_file_data
        dist_file = create_distribution_file(self.distribution_file.name, self._distribution_file_data)

        # remove all release package xmls where the package version has changed.
        print(f"- checking [{len(dist_file.release_packages.keys())}] release package cache entries for different versions")
        dropped_count = 0
        skipped_count = 0
        for pkg_name in sorted(dist_file.release_packages.keys()):
            if pkg_name not in self.distribution_file.release_packages:
                logger.debug("Skipping %s because not in the distro." % pkg_name)
                skipped_count += 1
                continue
            if pkg_name in self.release_resources and self._get_repo_info(dist_file, pkg_name) != self._get_repo_info(self.distribution_file, pkg_name):
                logger.debug("Dropping release resources package cache for %s" % pkg_name)
                dropped_count += 1
                del self.release_resources[pkg_name]
                
    
        sys.stdout.write('\n')
        sys.stdout.write(f'Dropped {dropped_count} repositories\n')
        sys.stdout.write(f'Skippted {skipped_count} repositories\n')

        # Remove all source package xmls where the devel branch is pointing to a different commit than
        # the one we have associated with our cache. This requires calling git ls-remote on all affected repos.
        if self.source_repo_resources:
            start_time = time.perf_counter()
            dropped_count = 0
            skipped_count = 0
            print(f"- checking [{len(self.source_repo_resources.keys())}] source repo cache entries without source entries, requires ls-remote")
            for repo in sorted(self.source_repo_resources.keys()):
                sys.stdout.write('.')
                sys.stdout.flush()
                try:
                    source_repository = dist_file.repositories[repo].source_repository
                except (KeyError, AttributeError):
                    # The repo entry has been dropped, or the source stanza from it has been dropped,
                    # either way, remove the cache entries associated with this repository.
                    logger.debug('Unable to find source repository info for repo "%s".' % repo)
                    del self.source_repo_resources[repo]
                    continue

                min_update_delta =  1 * 60 * 60 # TOOD(tfoote) magic number make into a parameter
                if '_last_update_time' in self.source_repo_resources[repo]:
                    now = datetime.datetime.now()
                    entry_age = (now - self.source_repo_resources[repo]['_last_update_time']).total_seconds()
                    if entry_age < min_update_delta:
                        logger.debug(f'Skipping check of {repo} because it was last updated only {entry_age} seconds ago less than {min_update_delta}')
                        skipped_count += 1
                        continue

                if ref_is_hash(source_repository.version):
                    source_hash = source_repository.version
                else:
                    result = Git().command('ls-remote', source_repository.url, source_repository.version)
                    if result['returncode'] != 0 or not result['output']:
                        # Error checking remote, or unable to find remote reference. Drop the cache entry.
                        logger.debug("Unable to check hash for branch %s of %s, dropping cache entry." % (source_repository.version, source_repository.url))
                        del self.source_repo_resources[repo]
                        dropped_count += 1
                        continue
                    # Split by line first and take the last line, to squelch any preamble output, for example
                    # a known host key validation notice.
                    source_hash = result['output'].split('\n')[-1].split('\t')[0]

                cached_hash = self.source_repo_resources[repo].ref()
                if source_hash != cached_hash:
                    logger.debug('Repo "%s" has moved from %s to %s, dropping cache.' % (repo, cached_hash, source_hash))
                    del self.source_repo_resources[repo]
                    dropped_count += 1
            sys.stdout.write('\n')
            sys.stdout.write(f'Dropped {dropped_count} repositories\n')
            sys.stdout.write(f'Skippted {skipped_count} repositories\n')
            end_time = time.perf_counter()
            logger.debug(f'Check of invalid source repo cache entries took {(end_time - start_time):.1f} seconds')

        self.distribution_file = dist_file
        self.distribution_file.source_packages = self.get_source_packages()

        # remove packages which are not in the new distribution file
        self._remove_obsolete_entries()

    def get_source_packages(self):
        """ Returns dictionary mapping source package names to Package() objects. """
        package_dict = {}
        for source_repo_name, source_repo in self.source_repo_resources.items():
            for pkg_name in source_repo:
                package_dict[pkg_name] = Package(pkg_name, source_repo_name)
        return package_dict

    def _get_repo_info(self, dist_file, pkg_name):
        pkg = dist_file.release_packages[pkg_name]
        repo = dist_file.repositories[pkg.repository_name].release_repository
        return (repo.version, repo.url)

    def _remove_obsolete_entries(self):
        for pkg_name in list(self.release_resources.keys()):
            if pkg_name not in self.distribution_file.release_packages:
                print('- REMOVE Release Resources for: ', pkg_name)
                del self.release_resources[pkg_name]
