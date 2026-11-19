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
import time

from rosdistro import logger
from rosdistro.cache_profiler import trace_operation
from rosdistro.fetch_reporter import get_active_reporter, report_fetch
from rosdistro.manifest_provider.chain import (
    REJECT,
    call_release_provider,
    call_source_provider,
    format_attempts,
    run_provider_chain,
)
from rosdistro.manifest_provider.sanitize import (  # noqa: F401
    sanitize_and_truncate_docs,
    sanitize_xml,
)
from rosdistro.manifest_provider.tree import (  # noqa: F401
    DEFAULT_SOURCE_FILEPATHS,
    IGNORE_MARKERS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    is_launch_file,
    list_local_repo_files,
    partition_repo_tree,
)


class _CachedProviderBase(object):

    def __init__(self, distribution_cache):
        self._distribution_cache = distribution_cache
        self.last_fetch_info = None

    def _get_max_lines(self, filepath):
        return 100

    def _sanitize_resource(self, filepath, content):
        if content is None:
            return None
        if isinstance(content, list):
            return list(content)
        if filepath == 'package.xml':
            with trace_operation('sanitize:xml', target=filepath):
                return sanitize_xml(content)
        with trace_operation('sanitize:docs', target=filepath):
            return sanitize_and_truncate_docs(content, max_length=self._get_max_lines(filepath))

    def _record_and_report(
        self,
        resource_type,
        target,
        fetch_type,
        status,
        start_s,
        provider=None,
        dist_name=None,
        detail=None,
        emit_report=True,
    ):
        self.last_fetch_info = {
            'fetch_type': fetch_type,
            'status': status,
            'provider': provider,
        }
        if not emit_report:
            return
        reporter = get_active_reporter()
        if reporter is not None and reporter.has_enclosing_fetch():
            return
        report_fetch(
            resource_type,
            target,
            fetch_type,
            status=status,
            duration_s=time.perf_counter() - start_s,
            dist_name=dist_name,
            detail=detail,
            level='top',
        )


class CachedManifestProvider(_CachedProviderBase):

    def __init__(self, distribution_cache, manifest_providers=None):
        super(CachedManifestProvider, self).__init__(distribution_cache)
        self._manifest_providers = manifest_providers
        self._failed_requests = set()
        self._failed_tags = set()

    @staticmethod
    def _get_release_failure_key(repo, pkg_name):
        try:
            tag = repo.get_release_tag(pkg_name)
        except Exception:
            tag = (repo.version, pkg_name)
        return (repo.url, tag)

    def _get_max_lines(self, filepath):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_release_resource_max_lines'):
            return config.get_release_resource_max_lines(filepath)
        return 100

    def _log_release_failure(self, filepath, pkg_name, repo_url, attempts):
        details = format_attempts(attempts, multiline=True)
        if details:
            logger.error(
                'Failed to fetch %s for package "%s" from "%s":\n%s' %
                (filepath, pkg_name, repo_url, details))
        else:
            logger.error(
                'Failed to fetch %s for package "%s" from "%s"' %
                (filepath, pkg_name, repo_url))

    def __call__(self, dist_name, repo, pkg_name, filepath='package.xml'):
        assert repo.version
        start_s = time.perf_counter()
        self.last_fetch_info = None
        resource_type = 'release:%s' % filepath

        # Backwards compatibility for legacy ReleaseCache without release_resources
        if not hasattr(self._distribution_cache, 'release_resources'):
            if filepath != 'package.xml':
                self._record_and_report(
                    resource_type, pkg_name, 'unsupported', 'not_found', start_s,
                    dist_name=dist_name, emit_report=False,
                )
                return None
            package_xml = self._distribution_cache.release_package_xmls.get(pkg_name, None)
            if package_xml:
                package_xml = self._sanitize_resource(filepath, package_xml)
                self._distribution_cache.release_package_xmls[pkg_name] = package_xml
                self._record_and_report(
                    resource_type, pkg_name, 'cache', 'ok', start_s, dist_name=dist_name,
                )
                return package_xml
            if not self._manifest_providers:
                self._record_and_report(
                    resource_type, pkg_name, 'cache_miss', 'not_found', start_s,
                    dist_name=dist_name, emit_report=False,
                )
                return None
            tag_key = self._get_release_failure_key(repo, pkg_name)
            req_key = (tag_key[0], tag_key[1], filepath)
            if tag_key in self._failed_tags or req_key in self._failed_requests:
                self._record_and_report(
                    resource_type, pkg_name, 'cached_failure', 'skipped', start_s,
                    dist_name=dist_name,
                )
                return None

            outcome = run_provider_chain(
                self._manifest_providers,
                lambda mp: call_release_provider(mp, dist_name, repo, pkg_name, 'package.xml'),
                target=pkg_name,
                dist_name=dist_name,
                accept=lambda raw: REJECT if raw is None else self._sanitize_resource('package.xml', raw),
            )
            if not outcome.succeeded:
                self._failed_requests.add(req_key)
                self._failed_tags.add(tag_key)
                self._log_release_failure('package.xml', pkg_name, repo.url, outcome.attempts)
                self._record_and_report(
                    resource_type, pkg_name, 'failed', 'failed', start_s, dist_name=dist_name,
                )
                return None
            self._distribution_cache.release_package_xmls[pkg_name] = outcome.result
            self._record_and_report(
                resource_type, pkg_name, outcome.provider_name, 'ok', start_s,
                provider=outcome.provider_name, dist_name=dist_name,
            )
            return outcome.result

        pkg_resources = self._distribution_cache.release_resources.get(pkg_name, {})
        if filepath in pkg_resources:
            manifest_content = self._sanitize_resource(filepath, pkg_resources[filepath])
            if manifest_content is not None:
                self._distribution_cache.release_resources[pkg_name][filepath] = manifest_content
            status = 'ok' if manifest_content is not None else 'not_found'
            self._record_and_report(
                resource_type, pkg_name, 'cache', status, start_s, dist_name=dist_name,
            )
            return manifest_content

        if not self._manifest_providers:
            self._record_and_report(
                resource_type, pkg_name, 'cache_miss', 'not_found', start_s,
                dist_name=dist_name, emit_report=False,
            )
            return None

        tag_key = self._get_release_failure_key(repo, pkg_name)
        req_key = (tag_key[0], tag_key[1], filepath)
        if tag_key in self._failed_tags or req_key in self._failed_requests:
            self._record_and_report(
                resource_type, pkg_name, 'cached_failure', 'skipped', start_s, dist_name=dist_name,
            )
            return None

        outcome = run_provider_chain(
            self._manifest_providers,
            lambda mp: call_release_provider(mp, dist_name, repo, pkg_name, filepath),
            target=pkg_name,
            dist_name=dist_name,
            accept=lambda raw: self._sanitize_resource(filepath, raw),
        )

        if not outcome.succeeded or (filepath == 'package.xml' and outcome.result is None):
            self._failed_requests.add(req_key)
            if outcome.permanent or filepath == 'package.xml':
                self._failed_tags.add(tag_key)
            self._log_release_failure(filepath, pkg_name, repo.url, outcome.attempts)
            self._record_and_report(
                resource_type, pkg_name, 'failed', 'failed', start_s, dist_name=dist_name,
            )
            return None

        # Populate the cache (including None for optional resources confirmed missing)
        if pkg_name not in self._distribution_cache.release_resources:
            self._distribution_cache.release_resources[pkg_name] = {}
        self._distribution_cache.release_resources[pkg_name][filepath] = outcome.result
        status = 'ok' if outcome.result is not None else 'not_found'
        self._record_and_report(
            resource_type, pkg_name, outcome.provider_name, status, start_s,
            provider=outcome.provider_name, dist_name=dist_name,
        )
        return outcome.result


class CachedSourceManifestProvider(_CachedProviderBase):

    def __init__(self, distribution_cache, source_manifest_providers=None):
        super(CachedSourceManifestProvider, self).__init__(distribution_cache)
        self._source_manifest_providers = source_manifest_providers
        self._failed_source_keys = set()

    def _get_source_query_params(self):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and config.has_any_source_enabled():
            filepaths = [fp for fp in config.enabled_source_resources() if fp != 'package.xml']
            metadata = config.enabled_source_metadata()
            return filepaths, metadata
        return list(DEFAULT_SOURCE_FILEPATHS), None

    def _get_max_lines(self, filepath):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_source_resource_max_lines'):
            return config.get_source_resource_max_lines(filepath)
        return 100

    def _get_metadata_max_lines(self, key):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_source_metadata_max_lines'):
            return config.get_source_metadata_max_lines(key)
        return 100

    def __call__(self, repo):
        assert repo.url
        start_s = time.perf_counter()
        self.last_fetch_info = None
        fetched_provider = 'cache'

        repo_cache = self._distribution_cache.source_repo_resources.get(repo.name, None)
        if repo_cache is None:
            if not self._source_manifest_providers:
                self._record_and_report(
                    'source:repo', repo.name, 'cache_miss', 'not_found', start_s, emit_report=False,
                )
                return None
            source_key = (repo.url, repo.version)
            is_cached_failure = (
                source_key in self._failed_source_keys or (
                    hasattr(self._distribution_cache, 'is_source_repo_failed') and
                    self._distribution_cache.is_source_repo_failed(repo.name, repo.url, repo.version)
                )
            )
            if is_cached_failure:
                self._record_and_report(
                    'source:repo', repo.name, 'cached_failure', 'skipped', start_s,
                )
                return None

            filepaths, metadata = self._get_source_query_params()
            outcome = run_provider_chain(
                self._source_manifest_providers,
                lambda mp: call_source_provider(mp, repo, filepaths=filepaths, metadata=metadata),
                target=repo.name,
                accept=lambda raw: REJECT if raw is None else raw,
            )
            if outcome.succeeded:
                repo_cache = outcome.result
                fetched_provider = outcome.provider_name
                if hasattr(repo_cache, 'last_update_time') and repo_cache.last_update_time() is None:
                    repo_cache.set_last_update_time(datetime.datetime.now())
                self._distribution_cache.source_repo_resources[repo.name] = repo_cache
                if hasattr(self._distribution_cache, 'clear_source_repo_failure'):
                    self._distribution_cache.clear_source_repo_failure(
                        repo.name, repo.url, repo.version
                    )
            else:
                details = format_attempts(outcome.attempts, multiline=True)
                if details:
                    logger.error(
                        'Failed to fetch source package.xml files for repo "%s" from "%s":\n%s' %
                        (repo.name, repo.url, details))
                else:
                    logger.error(
                        'Failed to fetch source package.xml files for repo "%s" from "%s"' %
                        (repo.name, repo.url))
                self._failed_source_keys.add(source_key)
                if hasattr(self._distribution_cache, 'record_source_repo_failure'):
                    summary = format_attempts(outcome.attempts, multiline=False) or None
                    self._distribution_cache.record_source_repo_failure(
                        repo.name,
                        repo.url,
                        repo.version,
                        error=summary,
                        permanent=outcome.permanent,
                    )
                self._record_and_report('source:repo', repo.name, 'failed', 'failed', start_s)
                return None

        # Sanitize/truncate and de-duplicate with the release resources so the YAML writer uses aliases
        if repo_cache is not None:
            with trace_operation('source:sanitize', fetch_type=fetched_provider, target=repo.name):
                for meta_doc in ('contributing', 'readme'):
                    meta_val = repo_cache.get_metadata(meta_doc)
                    if isinstance(meta_val, str):
                        with trace_operation('sanitize:docs', target='metadata:%s' % meta_doc):
                            sanitized_meta = sanitize_and_truncate_docs(
                                meta_val, max_length=self._get_metadata_max_lines(meta_doc)
                            )
                        repo_cache.set_metadata(meta_doc, sanitized_meta)
                for package_name, pkg_entries in repo_cache._data.items():
                    if package_name.startswith('_') or not isinstance(pkg_entries, dict):
                        continue
                    for resource_type in list(pkg_entries.keys()):
                        if resource_type == 'package_path' or pkg_entries[resource_type] is None:
                            continue
                        content = self._sanitize_resource(resource_type, pkg_entries[resource_type])
                        if isinstance(content, str):
                            release_content = self._distribution_cache.release_resources.get(
                                package_name, {}
                            ).get(resource_type, None)
                            if content == release_content and release_content is not None:
                                logger.debug(
                                    f'{package_name} Linking {resource_type} of source cache entry for compaction. '
                                    f'Lines saved: {len(content.splitlines())}'
                                )
                                content = release_content
                        repo_cache.add(
                            package_name, pkg_entries['package_path'],
                            content, resource_type, increment_update_time=False
                        )
            self._record_and_report(
                'source:repo',
                repo.name,
                fetched_provider,
                'ok',
                start_s,
                provider=fetched_provider if fetched_provider != 'cache' else None,
                detail='packages=%d' % len(repo_cache),
            )
        return repo_cache
