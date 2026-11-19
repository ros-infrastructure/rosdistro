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
from rosdistro.cache_profiler import trace_operation, trace_span
from rosdistro.fetch_reporter import get_active_reporter, get_provider_name, report_fetch
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
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


class CachedManifestProvider(object):

    def __init__(self, distribution_cache, manifest_providers=None):
        self._distribution_cache = distribution_cache
        self._manifest_providers = manifest_providers
        self._failed_requests = set()
        self._failed_tags = set()
        self.last_fetch_info = None

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

    def _sanitize_resource(self, filepath, content):
        if content is None:
            return None
        if filepath == 'package.xml':
            with trace_operation('sanitize:xml', target=filepath):
                return sanitize_xml(content)
        with trace_operation('sanitize:docs', target=filepath):
            return sanitize_and_truncate_docs(content, max_length=self._get_max_lines(filepath))

    def _emit_standalone_report(self, dist_name, pkg_name, filepath, fetch_type, status, duration_s):
        reporter = get_active_reporter()
        if reporter is not None and reporter.has_enclosing_fetch():
            return
        report_fetch(
            'release:%s' % filepath,
            pkg_name,
            fetch_type,
            status=status,
            duration_s=duration_s,
            dist_name=dist_name,
            level='top',
        )

    def __call__(self, dist_name, repo, pkg_name, filepath='package.xml'):
        assert repo.version
        start_s = time.perf_counter()
        self.last_fetch_info = None

        # Backwards compatibility for legacy ReleaseCache without release_resources
        if not hasattr(self._distribution_cache, 'release_resources'):
            if filepath != 'package.xml':
                self.last_fetch_info = {'fetch_type': 'unsupported', 'status': 'not_found', 'provider': None}
                return None
            package_xml = self._distribution_cache.release_package_xmls.get(pkg_name, None)
            if package_xml:
                with trace_operation('sanitize:xml', target=filepath):
                    package_xml = sanitize_xml(package_xml)
                self._distribution_cache.release_package_xmls[pkg_name] = package_xml
                self.last_fetch_info = {'fetch_type': 'cache', 'status': 'ok', 'provider': None}
                self._emit_standalone_report(
                    dist_name, pkg_name, filepath, 'cache', 'ok', time.perf_counter() - start_s
                )
                return package_xml
            if not self._manifest_providers:
                self.last_fetch_info = {'fetch_type': 'cache_miss', 'status': 'not_found', 'provider': None}
                return None
            tag_key = self._get_release_failure_key(repo, pkg_name)
            req_key = (tag_key[0], tag_key[1], filepath)
            if tag_key in self._failed_tags or req_key in self._failed_requests:
                self.last_fetch_info = {'fetch_type': 'cached_failure', 'status': 'skipped', 'provider': None}
                self._emit_standalone_report(
                    dist_name, pkg_name, filepath, 'cached_failure', 'skipped', time.perf_counter() - start_s
                )
                return None
            errors = []
            permanent_failure = False
            fetched_provider = None
            for mp in self._manifest_providers or []:
                provider_name = get_provider_name(mp)
                mp_name = getattr(mp, '__name__', str(mp))
                with trace_span(
                    'provider:%s' % provider_name,
                    kind='provider',
                    fetch_type=provider_name,
                    target=pkg_name,
                    dist_name=dist_name,
                ) as pspan:
                    try:
                        raw_xml = mp(dist_name, repo, pkg_name)
                    except PermanentManifestError as e:
                        pspan.status = 'failed'
                        logger.debug(
                            'Permanent failure in "%s()": %s; skipping fallback providers' % (mp_name, e)
                        )
                        errors.append((mp_name, e, False))
                        permanent_failure = True
                        break
                    except SkipManifestProvider as e:
                        pspan.status = 'skip'
                        logger.debug('Skipped "%s()": %s' % (mp_name, e))
                        errors.append((mp_name, e, True))
                        continue
                    except Exception as e:
                        pspan.status = 'failed'
                        logger.debug('Skipped "%s()": %s' % (mp_name, e))
                        errors.append((mp_name, e, False))
                        continue
                with trace_operation('sanitize:xml', target=filepath):
                    package_xml = sanitize_xml(raw_xml)
                fetched_provider = provider_name
                break
            if package_xml is None:
                if self._manifest_providers:
                    self._failed_requests.add(req_key)
                    if permanent_failure or filepath == 'package.xml':
                        self._failed_tags.add(tag_key)
                    relevant = [(name, err) for name, err, skipped in errors if not skipped]
                    if not relevant:
                        relevant = [(name, err) for name, err, _ in errors]
                    details = '\n'.join('  - %s(): %s' % (name, err) for name, err in relevant)
                    logger.error(
                        'Failed to fetch package.xml for package "%s" from "%s":\n%s' %
                        (pkg_name, repo.url, details))
                self.last_fetch_info = {'fetch_type': 'failed', 'status': 'failed', 'provider': None}
                self._emit_standalone_report(
                    dist_name, pkg_name, filepath, 'failed', 'failed', time.perf_counter() - start_s
                )
                return None
            self._distribution_cache.release_package_xmls[pkg_name] = package_xml
            self.last_fetch_info = {
                'fetch_type': fetched_provider,
                'status': 'ok',
                'provider': fetched_provider,
            }
            self._emit_standalone_report(
                dist_name, pkg_name, filepath, fetched_provider, 'ok', time.perf_counter() - start_s
            )
            return package_xml

        pkg_resources = self._distribution_cache.release_resources.get(pkg_name, {})
        if filepath in pkg_resources:
            manifest_content = self._sanitize_resource(filepath, pkg_resources[filepath])
            if manifest_content is not None:
                self._distribution_cache.release_resources[pkg_name][filepath] = manifest_content
            status = 'ok' if manifest_content is not None else 'not_found'
            self.last_fetch_info = {'fetch_type': 'cache', 'status': status, 'provider': None}
            self._emit_standalone_report(
                dist_name, pkg_name, filepath, 'cache', status, time.perf_counter() - start_s
            )
            return manifest_content

        if not self._manifest_providers:
            self.last_fetch_info = {'fetch_type': 'cache_miss', 'status': 'not_found', 'provider': None}
            return None

        tag_key = self._get_release_failure_key(repo, pkg_name)
        req_key = (tag_key[0], tag_key[1], filepath)
        if tag_key in self._failed_tags or req_key in self._failed_requests:
            self.last_fetch_info = {'fetch_type': 'cached_failure', 'status': 'skipped', 'provider': None}
            self._emit_standalone_report(
                dist_name, pkg_name, filepath, 'cached_failure', 'skipped', time.perf_counter() - start_s
            )
            return None

        # Use manifest providers to lazy load
        fetched = False
        fetched_provider = None
        manifest_content = None
        errors = []
        permanent_failure = False
        for mp in self._manifest_providers:
            provider_name = get_provider_name(mp)
            mp_name = getattr(mp, '__name__', str(mp))
            with trace_span(
                'provider:%s' % provider_name,
                kind='provider',
                fetch_type=provider_name,
                target=pkg_name,
                dist_name=dist_name,
            ) as pspan:
                try:
                    raw_content = mp(dist_name, repo, pkg_name, filepath)
                    if raw_content is None:
                        pspan.status = 'not_found'
                except PermanentManifestError as e:
                    pspan.status = 'failed'
                    logger.debug(
                        'Permanent failure in "%s()": %s; skipping fallback providers' % (mp_name, e)
                    )
                    errors.append((mp_name, e, False))
                    permanent_failure = True
                    break
                except SkipManifestProvider as e:
                    pspan.status = 'skip'
                    logger.debug('Skipped "%s()": %s' % (mp_name, e))
                    errors.append((mp_name, e, True))
                    continue
                except Exception as e:
                    # pass and try next manifest provider
                    pspan.status = 'failed'
                    logger.debug('Skipped "%s()": %s' % (mp_name, e))
                    errors.append((mp_name, e, False))
                    continue
            try:
                manifest_content = self._sanitize_resource(filepath, raw_content)
            except Exception as e:
                logger.debug('Skipped "%s()": %s' % (mp_name, e))
                errors.append((mp_name, e, False))
                continue
            fetched = True
            fetched_provider = provider_name
            break

        if not fetched or (filepath == 'package.xml' and manifest_content is None):
            self._failed_requests.add(req_key)
            if permanent_failure or filepath == 'package.xml':
                self._failed_tags.add(tag_key)
            relevant = [(name, err) for name, err, skipped in errors if not skipped]
            if not relevant:
                relevant = [(name, err) for name, err, _ in errors]
            if relevant:
                details = '\n'.join('  - %s(): %s' % (name, err) for name, err in relevant)
                logger.error(
                    'Failed to fetch %s for package "%s" from "%s":\n%s' %
                    (filepath, pkg_name, repo.url, details))
            else:
                logger.error(
                    'Failed to fetch %s for package "%s" from "%s"' %
                    (filepath, pkg_name, repo.url))
            self.last_fetch_info = {'fetch_type': 'failed', 'status': 'failed', 'provider': None}
            self._emit_standalone_report(
                dist_name, pkg_name, filepath, 'failed', 'failed', time.perf_counter() - start_s
            )
            return None

        # Populate the cache (including None for optional resources confirmed missing)
        if pkg_name not in self._distribution_cache.release_resources:
            self._distribution_cache.release_resources[pkg_name] = {}
        self._distribution_cache.release_resources[pkg_name][filepath] = manifest_content
        status = 'ok' if manifest_content is not None else 'not_found'
        self.last_fetch_info = {
            'fetch_type': fetched_provider,
            'status': status,
            'provider': fetched_provider,
        }
        self._emit_standalone_report(
            dist_name, pkg_name, filepath, fetched_provider, status, time.perf_counter() - start_s
        )
        return manifest_content


class CachedSourceManifestProvider(object):

    def __init__(self, distribution_cache, source_manifest_providers=None):
        self._distribution_cache = distribution_cache
        self._source_manifest_providers = source_manifest_providers
        self._failed_source_keys = set()
        self.last_fetch_info = None

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

    def _emit_standalone_report(self, repo_name, fetch_type, status, duration_s, detail=None):
        reporter = get_active_reporter()
        if reporter is not None and reporter.has_enclosing_fetch():
            return
        report_fetch(
            'source:repo',
            repo_name,
            fetch_type,
            status=status,
            duration_s=duration_s,
            detail=detail,
            level='top',
        )

    def __call__(self, repo):
        assert repo.url
        start_s = time.perf_counter()
        self.last_fetch_info = None
        fetched_provider = 'cache'

        repo_cache = self._distribution_cache.source_repo_resources.get(repo.name, None)
        if repo_cache is None:
            if not self._source_manifest_providers:
                self.last_fetch_info = {'fetch_type': 'cache_miss', 'status': 'not_found', 'provider': None}
                return None
            source_key = (repo.url, repo.version)
            is_cached_failure = (
                source_key in self._failed_source_keys or (
                    hasattr(self._distribution_cache, 'is_source_repo_failed') and
                    self._distribution_cache.is_source_repo_failed(repo.name, repo.url, repo.version)
                )
            )
            if is_cached_failure:
                self.last_fetch_info = {'fetch_type': 'cached_failure', 'status': 'skipped', 'provider': None}
                self._emit_standalone_report(
                    repo.name, 'cached_failure', 'skipped', time.perf_counter() - start_s
                )
                return None

            filepaths, metadata = self._get_source_query_params()
            # Use manifest providers to lazy load
            errors = []
            permanent_failure = False
            fetched_provider = None
            for mp in self._source_manifest_providers or []:
                provider_name = get_provider_name(mp)
                mp_name = getattr(mp, '__name__', str(mp))
                with trace_span(
                    'provider:%s' % provider_name,
                    kind='provider',
                    fetch_type=provider_name,
                    target=repo.name,
                ) as pspan:
                    try:
                        try:
                            repo_cache = mp(repo, filepaths=filepaths, metadata=metadata)
                        except TypeError:
                            try:
                                repo_cache = mp(repo, filepaths=filepaths)
                            except TypeError:
                                repo_cache = mp(repo)
                    except PermanentManifestError as e:
                        pspan.status = 'failed'
                        logger.debug(
                            'Permanent failure in "%s()": %s; skipping fallback providers' % (mp_name, e)
                        )
                        errors.append((mp_name, e, False))
                        permanent_failure = True
                        break
                    except SkipManifestProvider as e:
                        pspan.status = 'skip'
                        logger.debug('Skipped "%s()": %s' % (mp_name, e))
                        errors.append((mp_name, e, True))
                        continue
                    except Exception as e:
                        # pass and try next manifest provider
                        pspan.status = 'failed'
                        logger.debug('Skipped "%s()": %s' % (mp_name, e))
                        errors.append((mp_name, e, False))
                        continue

                    if repo_cache is None:
                        pspan.status = 'not_found'

                if repo_cache is not None:
                    fetched_provider = provider_name
                    if hasattr(repo_cache, 'last_update_time') and repo_cache.last_update_time() is None:
                        repo_cache.set_last_update_time(datetime.datetime.now())
                    self._distribution_cache.source_repo_resources[repo.name] = repo_cache
                    if hasattr(self._distribution_cache, 'clear_source_repo_failure'):
                        self._distribution_cache.clear_source_repo_failure(
                            repo.name, repo.url, repo.version
                        )
                    break
            if repo_cache is None and self._source_manifest_providers:
                relevant = [(name, err) for name, err, skipped in errors if not skipped]
                if not relevant:
                    relevant = [(name, err) for name, err, _ in errors]
                if relevant:
                    details = '\n'.join('  - %s(): %s' % (name, err) for name, err in relevant)
                    logger.error(
                        'Failed to fetch source package.xml files for repo "%s" from "%s":\n%s' %
                        (repo.name, repo.url, details))
                else:
                    logger.error(
                        'Failed to fetch source package.xml files for repo "%s" from "%s"' %
                        (repo.name, repo.url))
                self._failed_source_keys.add(source_key)
                if hasattr(self._distribution_cache, 'record_source_repo_failure'):
                    summary = (
                        '; '.join('%s(): %s' % (name, err) for name, err in relevant)
                        if relevant else None
                    )
                    self._distribution_cache.record_source_repo_failure(
                        repo.name,
                        repo.url,
                        repo.version,
                        error=summary,
                        permanent=permanent_failure,
                    )
                self.last_fetch_info = {'fetch_type': 'failed', 'status': 'failed', 'provider': None}
                self._emit_standalone_report(
                    repo.name, 'failed', 'failed', time.perf_counter() - start_s
                )
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
            self.last_fetch_info = {
                'fetch_type': fetched_provider,
                'status': 'ok',
                'provider': fetched_provider if fetched_provider != 'cache' else None,
            }
            self._emit_standalone_report(
                repo.name,
                fetched_provider,
                'ok',
                time.perf_counter() - start_s,
                detail='packages=%d' % len(repo_cache),
            )
        return repo_cache
