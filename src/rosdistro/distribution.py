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

from .distribution_cache import ResourceNotCrawledError
from .manifest_provider.bitbucket import bitbucket_manifest_provider
from .manifest_provider.git import git_manifest_provider, git_source_manifest_provider
from .manifest_provider.github import github_manifest_provider, github_source_manifest_provider
from .manifest_provider.gitlab import gitlab_manifest_provider, gitlab_source_manifest_provider
from .manifest_provider.tar import tar_manifest_provider, tar_source_manifest_provider
from .package import Package


class Distribution(object):

    default_manifest_providers = [github_manifest_provider, gitlab_manifest_provider, bitbucket_manifest_provider, git_manifest_provider, tar_manifest_provider]
    default_source_manifest_providers = [github_source_manifest_provider, gitlab_source_manifest_provider, git_source_manifest_provider, tar_source_manifest_provider]

    def __init__(self, distribution_file, manifest_providers=None, source_manifest_providers=None):
        self._distribution_file = distribution_file

        # Use default
        self._manifest_providers = Distribution.default_manifest_providers
        self._source_manifest_providers = Distribution.default_source_manifest_providers

        # Override default if given
        if manifest_providers is not None:
            self._manifest_providers = manifest_providers
        if source_manifest_providers is not None:
            self._source_manifest_providers = source_manifest_providers

        self._release_resources = {}
        self._source_repo_resources = {}

        self._cache = None
        for mp in (self._manifest_providers or []) + (self._source_manifest_providers or []):
            if hasattr(mp, '_distribution_cache'):
                self._cache = mp._distribution_cache
                break

    def __getattr__(self, name):
        return getattr(self._distribution_file, name)

    @property
    def cache(self):
        return self._cache

    @property
    def cache_config(self):
        return getattr(self._cache, 'config', None)

    def _has_lazy_release_providers(self):
        if not self._manifest_providers:
            return False
        for mp in self._manifest_providers:
            if hasattr(mp, '_manifest_providers'):
                if mp._manifest_providers:
                    return True
            else:
                return True
        return False

    def _has_lazy_source_providers(self):
        if not self._source_manifest_providers:
            return False
        for mp in self._source_manifest_providers:
            if hasattr(mp, '_source_manifest_providers'):
                if mp._source_manifest_providers:
                    return True
            else:
                return True
        return False

    def is_release_resource_crawled(self, filepath, pkg_name=None):
        if self._cache is not None and hasattr(self._cache, 'is_release_resource_crawled'):
            if self._cache.is_release_resource_crawled(filepath, pkg_name=pkg_name):
                return True
            if not self._has_lazy_release_providers():
                return False
        if pkg_name is not None:
            return filepath in self._release_resources.get(pkg_name, {})
        return any(filepath in pkg_data for pkg_data in self._release_resources.values())

    def is_source_resource_crawled(self, filepath, pkg_name=None):
        if self._cache is not None and hasattr(self._cache, 'is_source_resource_crawled'):
            if self._cache.is_source_resource_crawled(filepath, pkg_name=pkg_name):
                return True
            if not self._has_lazy_source_providers():
                return False
        if pkg_name is not None:
            for repo_cache in self._source_repo_resources.values():
                if repo_cache and pkg_name in repo_cache and filepath in repo_cache[pkg_name]:
                    return True
            return False
        return any(
            filepath in pkg_data
            for repo_cache in self._source_repo_resources.values() if repo_cache
            for _, pkg_data in repo_cache.items()
        )

    def is_source_metadata_crawled(self, key, repo_name=None):
        if self._cache is not None and hasattr(self._cache, 'is_source_metadata_crawled'):
            if self._cache.is_source_metadata_crawled(key, repo_name=repo_name):
                return True
            if not self._has_lazy_source_providers():
                return False
        clean_key = key[1:] if key.startswith('_') else key
        meta_key = '_' + clean_key
        if repo_name is not None:
            repo_cache = self._source_repo_resources.get(repo_name)
            return bool(repo_cache and meta_key in repo_cache._data)
        return any(
            meta_key in repo_cache._data
            for repo_cache in self._source_repo_resources.values() if repo_cache
        )

    def get_release_resource(self, pkg_name, filepath='package.xml', require_crawled=False):
        if require_crawled and not self._has_lazy_release_providers():
            if not self.is_release_resource_crawled(filepath, pkg_name=pkg_name):
                raise ResourceNotCrawledError(
                    "Release resource '%s' was not crawled in the cache for '%s'" % (filepath, self._distribution_file.name)
                )
        if pkg_name not in self._release_resources:
            self._release_resources[pkg_name] = {}
        if filepath not in self._release_resources[pkg_name]:
            if pkg_name not in self._distribution_file.release_packages:
                return None
            pkg = self._distribution_file.release_packages[pkg_name]
            repo_name = pkg.repository_name
            repo = self._distribution_file.repositories[repo_name]
            if repo.release_repository is None:
                return None
            repo = repo.release_repository
            if repo.version is None:
                return None
            content = None
            for mp in self._manifest_providers:
                content = mp(self._distribution_file.name, repo, pkg_name, filepath)
                if content is not None:
                    break
            self._release_resources[pkg_name][filepath] = content
        return self._release_resources.get(pkg_name, {}).get(filepath, None)

    def get_release_package_xml(self, pkg_name):
        return self.get_release_resource(pkg_name, 'package.xml')

    def get_release_readme(self, pkg_name, require_crawled=False):
        return self.get_release_resource(pkg_name, 'README.md', require_crawled=require_crawled)

    def get_release_changelog(self, pkg_name, require_crawled=False):
        return self.get_release_resource(pkg_name, 'CHANGELOG.rst', require_crawled=require_crawled)

    def get_source_resource(self, pkg_name, filepath='package.xml', require_crawled=False):
        if require_crawled and not self._has_lazy_source_providers():
            if not self.is_source_resource_crawled(filepath, pkg_name=pkg_name):
                raise ResourceNotCrawledError(
                    "Source resource '%s' was not crawled in the cache for '%s'" % (filepath, self._distribution_file.name)
                )
        if pkg_name not in self._distribution_file.source_packages and self._has_lazy_source_providers():
            inferred_repo = None
            if pkg_name in self._distribution_file.release_packages:
                inferred_repo = self._distribution_file.release_packages[pkg_name].repository_name
            elif pkg_name in self._distribution_file.repositories:
                inferred_repo = pkg_name
            if inferred_repo is not None:
                self.get_source_repo_resources(inferred_repo)
        if pkg_name not in self._distribution_file.source_packages:
            return None
        repo_name = self._distribution_file.source_packages[pkg_name].repository_name
        repo_cache = self.get_source_repo_resources(repo_name)
        if repo_cache and pkg_name in repo_cache:
            return repo_cache[pkg_name].get(filepath)
        return None

    def get_source_package_xml(self, pkg_name, require_crawled=False):
        return self.get_source_resource(pkg_name, 'package.xml', require_crawled=require_crawled)

    def get_source_readme(self, pkg_name, require_crawled=False):
        return self.get_source_resource(pkg_name, 'README.md', require_crawled=require_crawled)

    def get_source_changelog(self, pkg_name, require_crawled=False):
        return self.get_source_resource(pkg_name, 'CHANGELOG.rst', require_crawled=require_crawled)

    def get_source_repo_package_xmls(self, repo_name):
        return self.get_source_repo_resources(repo_name)

    def get_source_repo_metadata(self, repo_name, key=None, require_crawled=False):
        if repo_name not in self._distribution_file.repositories:
            if repo_name in self._distribution_file.source_packages:
                repo_name = self._distribution_file.source_packages[repo_name].repository_name
            elif repo_name in self._distribution_file.release_packages:
                repo_name = self._distribution_file.release_packages[repo_name].repository_name
        if require_crawled and not self._has_lazy_source_providers():
            if key is not None:
                if not self.is_source_metadata_crawled(key, repo_name=repo_name):
                    raise ResourceNotCrawledError(
                        "Source repository metadata '%s' was not crawled in the cache for '%s'" % (key, self._distribution_file.name)
                    )
            elif self.cache_config is not None and not self.cache_config.enabled_source_metadata():
                raise ResourceNotCrawledError(
                    "Source repository metadata was not crawled in the cache for '%s'" % self._distribution_file.name
                )
        repo_cache = self.get_source_repo_resources(repo_name)
        if repo_cache is None:
            return None
        if key is None:
            return repo_cache.get_all_metadata()
        return repo_cache.get_metadata(key)

    def get_source_repo_resources(self, repo_name):
        if repo_name not in self._source_repo_resources:
            if repo_name not in self._distribution_file.repositories:
                return None
            repo = self._distribution_file.repositories[repo_name].source_repository
            if repo is None:
                return None
            for mp in self._source_manifest_providers:
                repo_cache = mp(repo)
                if repo_cache is not None:
                    # Update map of package XMLs, and also list of known package names.
                    self._source_repo_resources[repo_name] = repo_cache
                    for pkg_name in repo_cache:
                        if pkg_name[0] != '_':
                            self._distribution_file.source_packages[pkg_name] = Package(pkg_name, repo_name)
                    return self._source_repo_resources[repo_name]
        return self._source_repo_resources.get(repo_name, None)
