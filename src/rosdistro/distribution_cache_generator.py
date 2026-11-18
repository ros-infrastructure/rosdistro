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

from contextlib import contextmanager
import copy
import datetime
import gzip
import os
import re
import time

from catkin_pkg.package import InvalidPackage, parse_package_string

import yaml

from . import _get_dist_file_data, get_cached_distribution, get_distribution_cache, get_index, logger
from .cache_profiler import (
    CacheProfiler,
    get_active_profiler,
    trace_operation,
    trace_span,
    use_profiler,
)
from .distribution_cache import CacheConfig, DistributionCache, NO_MAX_LINES_RESOURCES, resolve_git_remote_ref
from .fetch_reporter import format_phase_summary, report_fetch
from .manifest_provider.cache import PACKAGE_ASSET_LIST_RESOURCES, sanitize_and_truncate_docs
from .source_repository_cache import SourceRepositoryCache
from .vcs import ref_is_hash


def _find_local_cache_file(dist_name, cache_dir=None):
    for filename in (
        '%s-extended-cache.yaml' % dist_name,
        '%s-extended-cache.yaml.gz' % dist_name,
        '%s-cache.yaml' % dist_name,
        '%s-cache.yaml.gz' % dist_name,
    ):
        candidate = filename if cache_dir in (None, '', '.') else os.path.join(cache_dir, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def _load_local_cache_file(dist_name, path=None):
    candidate = path if path is not None else _find_local_cache_file(dist_name)
    if candidate is None:
        return None, None
    if candidate.endswith('.gz'):
        with trace_operation('gzip:read', target=candidate):
            with gzip.open(candidate, 'rb') as f:
                yaml_str = f.read()
    else:
        with open(candidate, 'r') as f:
            yaml_str = f.read()
    with trace_operation('yaml:load', target=candidate):
        data = yaml.safe_load(yaml_str)
    return DistributionCache(dist_name, data), candidate


def _effective_source_repo(dist_file, repo_name, fallback_src_to_doc):
    repo_entry = dist_file.repositories.get(repo_name)
    if repo_entry is None:
        return None
    if repo_entry.source_repository is not None or not fallback_src_to_doc:
        return repo_entry.source_repository
    return repo_entry.doc_repository


def _is_release_resource_compatible(source_config, target_config, pkg_name, repo, filepath, value):
    if not source_config.is_release_resource_enabled(filepath):
        return False
    if filepath == 'package.xml':
        if not isinstance(value, str) or not value:
            return False
        try:
            parsed = parse_package_string(value)
        except InvalidPackage:
            return False
        if parsed.name != pkg_name:
            return False
        if not re.match(r'^%s(-[\dA-z~\+\.]+)?$' % re.escape(parsed.version), repo.version):
            return False
        return True
    if value is None or filepath in NO_MAX_LINES_RESOURCES:
        return True
    if not isinstance(value, str):
        return False
    source_max = source_config.get_release_resource_max_lines(filepath)
    target_max = target_config.get_release_resource_max_lines(filepath)
    return source_max == target_max or source_max is None


def _copy_release_resource_value(source_config, target_config, filepath, value):
    copied = copy.deepcopy(value)
    if isinstance(copied, str) and filepath not in NO_MAX_LINES_RESOURCES:
        source_max = source_config.get_release_resource_max_lines(filepath)
        target_max = target_config.get_release_resource_max_lines(filepath)
        if source_max is None and target_max is not None:
            copied = sanitize_and_truncate_docs(copied, max_length=target_max)
    return copied


def _is_source_config_compatible(source_config, target_config):
    for res in target_config.enabled_source_resources():
        if not source_config.is_source_resource_enabled(res):
            return False
        if res not in NO_MAX_LINES_RESOURCES:
            source_max = source_config.get_source_resource_max_lines(res)
            target_max = target_config.get_source_resource_max_lines(res)
            if source_max != target_max and source_max is not None:
                return False
    for meta in target_config.enabled_source_metadata():
        if not source_config.is_source_metadata_enabled(meta):
            return False
        source_max = source_config.get_source_metadata_max_lines(meta)
        target_max = target_config.get_source_metadata_max_lines(meta)
        if source_max != target_max:
            return False
    return True


def _is_source_entry_complete(repo_cache, target_config):
    enabled_resources = target_config.enabled_source_resources()
    required_assets = [r for r in enabled_resources if r in PACKAGE_ASSET_LIST_RESOURCES]
    require_pkg_xml = target_config.is_source_resource_enabled('package.xml')
    for pkg_name in repo_cache:
        pkg_data = repo_cache[pkg_name]
        if not isinstance(pkg_data, dict):
            return False
        if require_pkg_xml and not (isinstance(pkg_data.get('package.xml'), str) and pkg_data['package.xml']):
            return False
        for asset_key in required_assets:
            if asset_key not in pkg_data or not isinstance(pkg_data[asset_key], list):
                return False
    return True


def _project_source_repo_cache(source_cache, source_config, target_config, verified_now=False):
    enabled_resources = set(target_config.enabled_source_resources())
    enabled_metadata = set(target_config.enabled_source_metadata())

    new_data = {'_ref': source_cache.ref()}
    if verified_now:
        new_data['_last_update_time'] = datetime.datetime.now().isoformat()
    else:
        last_update = source_cache.last_update_time()
        if last_update is not None:
            new_data['_last_update_time'] = last_update.isoformat()

    for key, val in source_cache._data.items():
        if key.startswith('_'):
            if key in ('_ref', '_last_update_time'):
                continue
            meta_name = key[1:]
            if meta_name not in enabled_metadata:
                continue
            new_data[key] = copy.deepcopy(val)
        elif isinstance(val, dict):
            pkg_entry = {'package_path': val.get('package_path', '')}
            for res_key, res_val in val.items():
                if res_key == 'package_path' or res_key not in enabled_resources:
                    continue
                copied_res = copy.deepcopy(res_val)
                if isinstance(copied_res, str) and res_key not in NO_MAX_LINES_RESOURCES:
                    source_max = source_config.get_source_resource_max_lines(res_key)
                    target_max = target_config.get_source_resource_max_lines(res_key)
                    if source_max is None and target_max is not None:
                        copied_res = sanitize_and_truncate_docs(copied_res, max_length=target_max)
                pkg_entry[res_key] = copied_res
            new_data[key] = pkg_entry

    return SourceRepositoryCache(new_data)


class _NeighborCachePool(object):
    """Manages neighboring DistributionCache instances (already-processed distros and lazily
    loaded local cache files) and tracks authoritative remote git refs observed during the run.

    Local cache files loaded on demand for future distros are handed over via
    ``take_local_cache()`` when those distros are subsequently updated, so no distro cache file
    is loaded from disk more than once per run.
    """

    def __init__(self, index=None, ignore_local=False, initial_caches=None, cache_dir=None):
        self._ignore_local = ignore_local
        self._processed_order = []
        self._processed_caches = {}
        self._pending_local = []
        self._local_caches = {}
        self._local_order = []
        self._attempted_local = set()
        self._source_url_indexes = {}
        self.known_remote_refs = {}

        if initial_caches:
            if isinstance(initial_caches, dict):
                for d_name, d_cache in initial_caches.items():
                    if isinstance(d_cache, DistributionCache):
                        self.add_cache(d_name, d_cache)
            else:
                for d_cache in initial_caches:
                    if isinstance(d_cache, DistributionCache):
                        self.add_cache(d_cache.distribution_file.name, d_cache)

        if not ignore_local and index is not None and isinstance(getattr(index, 'distributions', None), dict):
            discovered = []
            for other_dist in sorted(index.distributions.keys()):
                candidate = _find_local_cache_file(other_dist, cache_dir=cache_dir)
                if candidate is not None:
                    try:
                        mtime = os.path.getmtime(candidate)
                    except OSError:
                        mtime = 0.0
                    discovered.append((mtime, other_dist, candidate))
            discovered.sort(key=lambda item: (-item[0], item[1]))
            self._pending_local = [(d_name, path) for _, d_name, path in discovered]

    def add_cache(self, dist_name, cache):
        if not isinstance(cache, DistributionCache):
            return
        self._processed_caches[dist_name] = cache
        self._local_caches.pop(dist_name, None)
        self._source_url_indexes.pop(dist_name, None)
        if dist_name in self._processed_order:
            self._processed_order.remove(dist_name)
        self._processed_order.append(dist_name)

    def take_local_cache(self, dist_name):
        """Transfer ownership of a local DistributionCache already loaded on demand for a future
        distro, avoiding re-reading its cache file from disk when it is updated.
        """
        self._source_url_indexes.pop(dist_name, None)
        return self._local_caches.pop(dist_name, None)

    def record_remote_ref(self, url, version, ref_hash):
        if url and version and ref_hash:
            self.known_remote_refs[(url, version)] = ref_hash

    def _get_source_url_index(self, dist_name, cache):
        url_index = self._source_url_indexes.get(dist_name)
        if url_index is None:
            url_index = {}
            dist_file = cache.distribution_file
            fallback_src_to_doc = cache.config.fallback_src_to_doc
            for repo_name, repo_cache in (cache.source_repo_resources or {}).items():
                if not isinstance(repo_cache, SourceRepositoryCache) or len(repo_cache) == 0:
                    continue
                src_repo = _effective_source_repo(dist_file, repo_name, fallback_src_to_doc)
                if src_repo is None or not src_repo.url:
                    continue
                url_index.setdefault(src_repo.url, []).append((repo_name, src_repo, repo_cache))
            self._source_url_indexes[dist_name] = url_index
        return url_index

    def _iter_neighbors(self, exclude_dist_name):
        # 1. Already-processed in-memory DistributionCache instances (most recent first)
        for d_name in reversed(self._processed_order):
            if d_name == exclude_dist_name:
                continue
            yield d_name, self._processed_caches[d_name]

        # 2. Already-loaded local neighbor DistributionCache instances (newest file mtime first)
        for d_name in self._local_order:
            if d_name == exclude_dist_name or d_name in self._processed_caches:
                continue
            loaded = self._local_caches.get(d_name)
            if loaded is not None:
                yield d_name, loaded[0]

        # 3. Lazily load any remaining local neighbor cache files on disk
        for d_name, path in self._pending_local:
            if d_name == exclude_dist_name or d_name in self._processed_caches or d_name in self._attempted_local:
                continue
            self._attempted_local.add(d_name)
            loaded_cache = None
            with trace_span(
                'cache:neighbor_load',
                kind='fetch',
                resource_type='cache:neighbor_load',
                fetch_type='local_file',
                target=d_name,
                detail='path=%s' % path,
            ) as lspan:
                try:
                    loaded_cache, _ = _load_local_cache_file(d_name, path=path)
                    if loaded_cache is None:
                        lspan.status = 'not_found'
                except Exception as e:
                    lspan.status = 'failed'
                    logger.debug('Failed to load neighboring local cache "%s" from "%s": %s' % (d_name, path, e))
            if loaded_cache is not None:
                self._local_caches[d_name] = (loaded_cache, path)
                self._local_order.append(d_name)
                report_fetch(
                    'cache:neighbor_load',
                    d_name,
                    'local_file',
                    status='ok',
                    duration_s=lspan.duration_s,
                    detail='path=%s' % path,
                    level='top',
                )
                yield d_name, loaded_cache

    def find_release_resource(self, exclude_dist_name, pkg_name, repo, filepath, target_config):
        if repo is None or repo.version is None:
            return False, None, None
        try:
            release_tag = repo.get_release_tag(pkg_name)
        except KeyError:
            return False, None, None
        with trace_operation('neighbor:lookup', target=pkg_name):
            for neighbor_dist, neighbor_cache in self._iter_neighbors(exclude_dist_name):
                pkg_data = (neighbor_cache.release_resources or {}).get(pkg_name)
                if not isinstance(pkg_data, dict) or filepath not in pkg_data:
                    continue
                dist_file = neighbor_cache.distribution_file
                n_pkg = dist_file.release_packages.get(pkg_name)
                if n_pkg is None:
                    continue
                n_repo_entry = dist_file.repositories.get(n_pkg.repository_name)
                if n_repo_entry is None or n_repo_entry.release_repository is None:
                    continue
                n_repo = n_repo_entry.release_repository
                if (
                    n_repo.version is None
                    or pkg_data.get('version') != n_repo.version
                    or n_repo.type != repo.type
                    or n_repo.url != repo.url
                    or n_repo.version != repo.version
                ):
                    continue
                try:
                    if n_repo.get_release_tag(pkg_name) != release_tag:
                        continue
                except KeyError:
                    continue
                val = pkg_data[filepath]
                if not _is_release_resource_compatible(
                    neighbor_cache.config, target_config, pkg_name, repo, filepath, val
                ):
                    continue
                copied = _copy_release_resource_value(neighbor_cache.config, target_config, filepath, val)
                return True, copied, neighbor_dist
        return False, None, None

    def find_source_repo_cache(self, exclude_dist_name, target_repo, target_config):
        if target_repo is None or not target_repo.url:
            return None, None
        if target_repo.type == 'git' and not target_repo.version:
            return None, None

        with trace_operation('neighbor:lookup', target=target_repo.name):
            now = datetime.datetime.now()
            ref_key = (target_repo.url, target_repo.version)

            expected_sha = None
            ls_remote_failed = False
            if target_repo.type == 'git':
                if ref_is_hash(target_repo.version):
                    expected_sha = target_repo.version
                elif ref_key in self.known_remote_refs:
                    expected_sha = self.known_remote_refs[ref_key]
                    if expected_sha is None:
                        ls_remote_failed = True

            min_update_delta = target_config.min_update_delta

            expired_git_candidates = []
            compatible_neighbors = []

            for neighbor_dist, neighbor_cache in self._iter_neighbors(exclude_dist_name):
                if not _is_source_config_compatible(neighbor_cache.config, target_config):
                    continue
                url_entries = self._get_source_url_index(neighbor_dist, neighbor_cache).get(target_repo.url, ())
                if not url_entries:
                    continue
                compatible_neighbors.append((neighbor_dist, neighbor_cache, url_entries))

                if expected_sha is not None:
                    for _, n_repo, repo_cache in url_entries:
                        if n_repo.type != 'git' or repo_cache.ref() != expected_sha:
                            continue
                        if not _is_source_entry_complete(repo_cache, target_config):
                            continue
                        verified_now = not ref_is_hash(target_repo.version)
                        copied = _project_source_repo_cache(
                            repo_cache, neighbor_cache.config, target_config, verified_now=verified_now
                        )
                        return copied, neighbor_dist
                    continue

                if ls_remote_failed:
                    continue

                for _, n_repo, repo_cache in url_entries:
                    if n_repo.type != target_repo.type or n_repo.version != target_repo.version:
                        continue
                    if not _is_source_entry_complete(repo_cache, target_config):
                        continue
                    last_update_time = repo_cache.last_update_time()
                    if min_update_delta > 0 and last_update_time is not None:
                        if last_update_time.tzinfo is not None:
                            entry_age = (
                                datetime.datetime.now(last_update_time.tzinfo) - last_update_time
                            ).total_seconds()
                        else:
                            entry_age = (now - last_update_time).total_seconds()
                        if entry_age < min_update_delta:
                            copied = _project_source_repo_cache(
                                repo_cache, neighbor_cache.config, target_config, verified_now=False
                            )
                            return copied, neighbor_dist
                    if target_repo.type == 'git' and isinstance(repo_cache.ref(), str) and repo_cache.ref():
                        expired_git_candidates.append((neighbor_dist, neighbor_cache, repo_cache))

            if expected_sha is None and not ls_remote_failed and expired_git_candidates:
                remote_hash, _ = resolve_git_remote_ref(target_repo.url, target_repo.version)
                self.known_remote_refs[ref_key] = remote_hash
                if remote_hash is not None:
                    for neighbor_dist, neighbor_cache, url_entries in compatible_neighbors:
                        for _, n_repo, repo_cache in url_entries:
                            if n_repo.type != 'git' or repo_cache.ref() != remote_hash:
                                continue
                            if not _is_source_entry_complete(repo_cache, target_config):
                                continue
                            copied = _project_source_repo_cache(
                                repo_cache, neighbor_cache.config, target_config, verified_now=True
                            )
                            return copied, neighbor_dist

        return None, None


def _get_last_provider_fetch_info(dist, attr_name):
    providers = getattr(dist, attr_name, None)
    if not isinstance(providers, (list, tuple)):
        return None
    for mp in providers:
        info = getattr(mp, 'last_fetch_info', None)
        if isinstance(info, dict):
            return info
    return None


def _fetch_release_resource(
    dist, dist_name, cache, pkg_name, repo, resource_name,
    cache_config, neighbor_pool, has_release_cache_dict, active_profiler,
):
    """Fetch a single release resource via cache, local neighbor, or manifest providers,
    recording timing and emitting standardized framework output."""
    if resource_name == 'package.xml':
        was_cached = bool(
            has_release_cache_dict and cache.release_resources[pkg_name].get('package.xml')
        )
    else:
        was_cached = bool(
            has_release_cache_dict and resource_name in cache.release_resources[pkg_name]
        )

    copied_from_neighbor = False
    neighbor_dist = None
    resource_type = 'release:%s' % resource_name

    with active_profiler.span(
        resource_type,
        kind='fetch',
        resource_type=resource_type,
        target=pkg_name,
        dist_name=dist_name,
        deferred_label=True,
    ) as fetch_span:
        if not was_cached and neighbor_pool is not None and has_release_cache_dict:
            found, copied_val, neighbor_dist = neighbor_pool.find_release_resource(
                dist_name, pkg_name, repo, resource_name, cache_config
            )
            if found:
                cache.release_resources[pkg_name][resource_name] = copied_val
                copied_from_neighbor = True

        if resource_name == 'package.xml':
            content = dist.get_release_package_xml(pkg_name)
        else:
            content = dist.get_release_resource(pkg_name, resource_name)

        detail_parts = []
        if copied_from_neighbor:
            fetch_type = 'local_neighbor'
            status = 'ok' if content is not None else 'not_found'
            detail_parts.append('neighbor=%s' % neighbor_dist)
        elif was_cached:
            fetch_type = 'cache'
            status = 'ok' if content is not None else 'not_found'
        else:
            info = _get_last_provider_fetch_info(dist, '_manifest_providers')
            if info is not None:
                fetch_type = info.get('fetch_type') or 'unknown'
                status = info.get('status') or ('ok' if content is not None else 'not_found')
            elif content is not None:
                fetch_type = 'provider'
                status = 'ok'
            elif resource_name == 'package.xml':
                fetch_type = 'failed'
                status = 'failed'
            else:
                fetch_type = 'provider'
                status = 'not_found'

        if resource_name == 'package.xml' and getattr(repo, 'version', None):
            detail_parts.append('version=%s' % repo.version)

        detail = ', '.join(detail_parts) if detail_parts else None
        fetch_span.fetch_type = fetch_type
        fetch_span.status = status
        fetch_span.detail = detail

    active_profiler.report_fetch(
        resource_type,
        pkg_name,
        fetch_type,
        status=status,
        duration_s=fetch_span.duration_s,
        dist_name=dist_name,
        detail=detail,
        level='top',
    )
    return content, fetch_type, status, fetch_span.duration_s


@contextmanager
def _noop_context():
    yield


def generate_distribution_caches(
    index, dist_names=None, preclean=False,
    ignore_local=False, include_source=False, debug=False, limit=None, config=None,
    output_dir=None, profiler=None,
):
    active_profiler = profiler
    if active_profiler is None:
        active_profiler = get_active_profiler()
    if active_profiler is None:
        active_profiler = CacheProfiler(enabled=True, record_spans=False, debug=debug)
    elif debug:
        active_profiler.debug = True

    with use_profiler(active_profiler):
        needs_root = not active_profiler.has_active_span()
        root_ctx = (
            active_profiler.span('build_cache', kind='root')
            if needs_root else _noop_context()
        )
        with root_ctx:
            if os.path.isfile(index):
                index = 'file://' + os.path.abspath(index)
            index = get_index(index)

            if not dist_names:
                dist_names = sorted(index.distributions.keys())

            neighbor_pool = (
                None if preclean
                else _NeighborCachePool(index=index, ignore_local=ignore_local, cache_dir=output_dir)
            )

            errors = []
            caches = {}
            for dist_name in dist_names:
                with active_profiler.span('distro:%s' % dist_name, kind='distro', dist_name=dist_name):
                    try:
                        cache = generate_distribution_cache(
                            index, dist_name, preclean=preclean,
                            ignore_local=ignore_local, include_source=include_source,
                            debug=debug, limit=limit, config=config,
                            neighbor_caches=neighbor_pool, cache_dir=output_dir,
                            profiler=active_profiler)
                    except RuntimeError as e:
                        errors.append(str(e))
                        continue
                    caches[dist_name] = cache
                    if neighbor_pool is not None and cache is not None:
                        neighbor_pool.add_cache(dist_name, cache)
                    if output_dir is not None and cache is not None:
                        write_distribution_cache(
                            dist_name,
                            cache,
                            include_source=include_source,
                            write_extended=(config is not None),
                            output_dir=output_dir,
                        )
            if errors:
                raise RuntimeError('\n'.join(errors))
            return caches


def generate_distribution_cache(
    index, dist_name, preclean=False, ignore_local=False,
    include_source=False, debug=False, limit=None, config=None,
    neighbor_caches=None, cache_dir=None, profiler=None,
):
    active_profiler = profiler
    if active_profiler is None:
        active_profiler = get_active_profiler()
    if active_profiler is None:
        active_profiler = CacheProfiler(enabled=True, record_spans=False, debug=debug)
    elif debug:
        active_profiler.debug = True

    with use_profiler(active_profiler):
        needs_distro = active_profiler.current_dist_name() != dist_name
        distro_ctx = (
            active_profiler.span('distro:%s' % dist_name, kind='distro', dist_name=dist_name)
            if needs_distro else _noop_context()
        )
        with distro_ctx:
            return _generate_distribution_cache_impl(
                index, dist_name, preclean=preclean, ignore_local=ignore_local,
                include_source=include_source, debug=debug, limit=limit, config=config,
                neighbor_caches=neighbor_caches, cache_dir=cache_dir, active_profiler=active_profiler,
            )


def _generate_distribution_cache_impl(
    index, dist_name, preclean=False, ignore_local=False,
    include_source=False, debug=False, limit=None, config=None,
    neighbor_caches=None, cache_dir=None, active_profiler=None,
):
    cache_config = CacheConfig.from_input(config, include_source=include_source, limit=limit)
    if preclean:
        neighbor_pool = None
    elif isinstance(neighbor_caches, _NeighborCachePool):
        neighbor_pool = neighbor_caches
    else:
        neighbor_pool = _NeighborCachePool(
            index=index, ignore_local=ignore_local, initial_caches=neighbor_caches,
            cache_dir=cache_dir,
        )

    get_cached_kwargs = {}
    if cache_dir not in (None, '', '.'):
        get_cached_kwargs['cache_dir'] = cache_dir
    if neighbor_pool is not None:
        get_cached_kwargs['known_remote_refs'] = neighbor_pool.known_remote_refs
        preloaded = neighbor_pool.take_local_cache(dist_name)
        if preloaded is not None:
            get_cached_kwargs['preloaded_cache'] = preloaded
    dist, cache = _get_cached_distribution(
        index, dist_name, preclean=preclean, ignore_local=ignore_local,
        include_source=include_source, config=cache_config, **get_cached_kwargs)
    if cache is not None:
        cache.config = cache_config

    has_release_cache_dict = isinstance(getattr(cache, 'release_resources', None), dict)
    has_source_cache_dict = isinstance(getattr(cache, 'source_repo_resources', None), dict)

    effective_limit = cache_config.limit
    all_release_pkgs = sorted(dist.release_packages.keys())
    if effective_limit is not None:
        release_pkgs = all_release_pkgs[:effective_limit]
    else:
        release_pkgs = all_release_pkgs

    all_release_resources = cache_config.enabled_release_resources()
    limit_note = (
        ' (limited to %d of %d)' % (len(release_pkgs), len(all_release_pkgs))
        if effective_limit is not None and len(all_release_pkgs) > effective_limit else ''
    )
    print(
        '- fetch release resources (%d packages%s: %s)'
        % (len(release_pkgs), limit_note, ', '.join(all_release_resources)),
        flush=True,
    )

    errors = []
    rel_counts = {}
    rel_durations = {}
    rel_phase_start = time.perf_counter()
    with active_profiler.span('phase:release_fetch', kind='phase', dist_name=dist_name):
        for pkg_name in release_pkgs:
            repo = dist.repositories[dist.release_packages[pkg_name].repository_name].release_repository
            if repo.version is None:
                active_profiler.record_instant_fetch(
                    'release:package.xml',
                    pkg_name,
                    'skipped',
                    status='skipped',
                    dist_name=dist_name,
                    detail='no version',
                    kind='fetch',
                    emit_console=True,
                )
                rel_counts['skipped'] = rel_counts.get('skipped', 0) + 1
                continue
            if cache:
                if pkg_name in cache.release_resources:
                    cached_ver = cache.release_resources[pkg_name].get('version')
                    if cached_ver is not None and cached_ver != repo.version:
                        logger.debug("Invalidating package '%s' due to version mismatch" % pkg_name)
                        cache.release_resources[pkg_name] = {}
                else:
                    cache.release_resources[pkg_name] = {}
                cache.release_resources[pkg_name]['version'] = repo.version

            for resource_name in all_release_resources:
                content, fetch_type, _, dur_s = _fetch_release_resource(
                    dist, dist_name, cache, pkg_name, repo, resource_name,
                    cache_config, neighbor_pool, has_release_cache_dict, active_profiler,
                )
                rel_counts[fetch_type] = rel_counts.get(fetch_type, 0) + 1
                rel_durations[fetch_type] = rel_durations.get(fetch_type, 0.0) + dur_s

                if resource_name == 'package.xml':
                    if not content:
                        errors.append('%s: missing package.xml file for package "%s"' % (dist_name, pkg_name))
                        break
                    try:
                        pkg = parse_package_string(content)
                    except InvalidPackage as e:
                        errors.append('%s: invalid package.xml file for package "%s": %s' % (dist_name, pkg_name, e))
                        break
                    if not re.match(r'^%s(-[\dA-z~\+\.]+)?$' % re.escape(pkg.version), repo.version):
                        errors.append(
                            '%s: different version in package.xml (%s) for package "%s" than for the repository (%s) '
                            '(after removing the debian increment)'
                            % (dist_name, pkg.version, pkg_name, repo.version)
                        )

    rel_phase_dur = time.perf_counter() - rel_phase_start
    print(
        format_phase_summary(
            'release fetch', rel_counts, rel_durations, rel_phase_dur, dist_name=dist_name
        ),
        flush=True,
    )

    if include_source or cache_config.has_any_source_enabled():
        all_repos = sorted(dist.repositories.keys())
        if effective_limit is not None:
            repos_to_scan = all_repos[:effective_limit]
        else:
            repos_to_scan = all_repos
        src_limit_note = (
            ' (limited to %d of %d)' % (len(repos_to_scan), len(all_repos))
            if effective_limit is not None and len(all_repos) > effective_limit else ''
        )
        print(
            '- fetch source repositories (%d repositories%s)' % (len(repos_to_scan), src_limit_note),
            flush=True,
        )

        src_counts = {}
        src_durations = {}
        src_phase_start = time.perf_counter()
        with active_profiler.span('phase:source_fetch', kind='phase', dist_name=dist_name):
            for repo_name in repos_to_scan:
                repo_entry = dist.repositories[repo_name]
                source_repo = (
                    repo_entry.source_repository
                    if repo_entry.source_repository is not None or not cache_config.fallback_src_to_doc
                    else repo_entry.doc_repository
                )
                if source_repo is None:
                    active_profiler.record_instant_fetch(
                        'source:repo',
                        repo_name,
                        'skipped',
                        status='skipped',
                        dist_name=dist_name,
                        detail='no source or doc entry',
                        kind='fetch',
                        emit_console=True,
                    )
                    src_counts['skipped'] = src_counts.get('skipped', 0) + 1
                    continue

                was_cached = bool(
                    has_source_cache_dict and cache.source_repo_resources.get(repo_name) is not None
                )
                is_known_failed = bool(
                    not was_cached
                    and hasattr(cache, 'is_source_repo_failed')
                    and cache.is_source_repo_failed(repo_name, source_repo.url, source_repo.version)
                )
                copied_from_neighbor = False
                neighbor_dist = None

                with active_profiler.span(
                    'source:repo',
                    kind='fetch',
                    resource_type='source:repo',
                    target=repo_name,
                    dist_name=dist_name,
                    deferred_label=True,
                ) as fetch_span:
                    if not was_cached and not is_known_failed and neighbor_pool is not None and has_source_cache_dict:
                        copied_repo_cache, neighbor_dist = neighbor_pool.find_source_repo_cache(
                            dist_name, source_repo, cache_config
                        )
                        if copied_repo_cache is not None:
                            cache.source_repo_resources[repo_name] = copied_repo_cache
                            if hasattr(cache, 'clear_source_repo_failure'):
                                cache.clear_source_repo_failure(
                                    repo_name, source_repo.url, source_repo.version
                                )
                            copied_from_neighbor = True

                    repo_cache = dist.get_source_repo_resources(repo_name)
                    if (
                        not was_cached
                        and not copied_from_neighbor
                        and neighbor_pool is not None
                        and isinstance(repo_cache, SourceRepositoryCache)
                        and getattr(source_repo, 'type', 'git') == 'git'
                        and getattr(source_repo, 'version', None)
                        and repo_cache.ref()
                    ):
                        neighbor_pool.record_remote_ref(
                            source_repo.url, source_repo.version, repo_cache.ref()
                        )

                    detail_parts = []
                    if copied_from_neighbor:
                        fetch_type = 'local_neighbor'
                        status = 'ok'
                        detail_parts.append('neighbor=%s' % neighbor_dist)
                    elif was_cached:
                        fetch_type = 'cache'
                        status = 'ok'
                    elif is_known_failed:
                        fetch_type = 'cached_failure'
                        status = 'skipped'
                    else:
                        info = _get_last_provider_fetch_info(dist, '_source_manifest_providers')
                        if info is not None:
                            fetch_type = info.get('fetch_type') or 'unknown'
                            status = info.get('status') or ('ok' if repo_cache is not None else 'failed')
                        elif repo_cache is not None:
                            fetch_type = 'provider'
                            status = 'ok'
                        else:
                            fetch_type = 'failed'
                            status = 'failed'

                    if isinstance(repo_cache, SourceRepositoryCache):
                        detail_parts.append('packages=%d' % len(repo_cache))
                    detail = ', '.join(detail_parts) if detail_parts else None
                    fetch_span.fetch_type = fetch_type
                    fetch_span.status = status
                    fetch_span.detail = detail

                src_counts[fetch_type] = src_counts.get(fetch_type, 0) + 1
                src_durations[fetch_type] = src_durations.get(fetch_type, 0.0) + fetch_span.duration_s
                active_profiler.report_fetch(
                    'source:repo',
                    repo_name,
                    fetch_type,
                    status=status,
                    duration_s=fetch_span.duration_s,
                    dist_name=dist_name,
                    detail=detail,
                    level='top',
                )

        src_phase_dur = time.perf_counter() - src_phase_start
        print(
            format_phase_summary(
                'source fetch', src_counts, src_durations, src_phase_dur, dist_name=dist_name
            ),
            flush=True,
        )

    if errors:
        raise RuntimeError('\n'.join(errors))

    return cache


class CacheYamlDumper(yaml.SafeDumper):
    """ A yaml dumper specific to dumping the serialized rosdistro cache file.

    Allows long lines and direct unicode representation. This avoids writing escape
    sequences, line continuations, and other noise into the cache file. Also permits
    long strings to alias each other (by default only objects do).
    """

    yaml_alias_min_length = CacheConfig.DEFAULT_YAML_ALIAS_MIN_LENGTH

    def __init__(self, *args, **kwargs):
        kwargs['width'] = 10000
        kwargs['allow_unicode'] = True
        super(CacheYamlDumper, self).__init__(*args, **kwargs)

    def ignore_aliases(self, content):
        """ Allow long strings that look like package XML to alias to each other in the YAML output. """
        return not (isinstance(content, str) and len(content) > self.yaml_alias_min_length)

    def represent_mapping(self, tag, mapping, flow_style=False):
        """ Gives compact representation for the distribution_file section, while allowing the package
            XML cache sections room to breathe."""
        if any([x in mapping for x in ('source', 'release', 'doc')]):
            flow_style = True
        return yaml.SafeDumper.represent_mapping(self, tag, mapping, flow_style)

    @classmethod
    def with_min_length(cls, min_length):
        if min_length is None or min_length == cls.yaml_alias_min_length:
            return cls
        return type('ConfiguredCacheYamlDumper', (cls,), {'yaml_alias_min_length': int(min_length)})


def _write_cache_file(dist_name, filename, target_path, fetch_type, data_bytes, compressed=False):
    with trace_span(
        'cache:write',
        kind='fetch',
        resource_type='cache:write',
        fetch_type=fetch_type,
        target=filename,
        dist_name=dist_name,
    ) as wspan:
        if compressed:
            with trace_operation('gzip:write', target=filename):
                with gzip.open(target_path, 'wb') as f:
                    f.write(data_bytes)
        else:
            with open(target_path, 'wb') as f:
                f.write(data_bytes)
    report_fetch(
        'cache:write',
        filename,
        fetch_type,
        status='ok',
        duration_s=wspan.duration_s,
        dist_name=dist_name,
        detail=('path=%s' % target_path) if target_path != filename else None,
        level='top',
    )


def write_distribution_cache(
    dist_name, cache, include_source=False, write_extended=False, output_dir='.'
):
    """Serialize and write cache files for a single distribution."""
    dumper = CacheYamlDumper.with_min_length(cache.config.yaml_alias_min_length)
    if output_dir not in (None, '', '.'):
        os.makedirs(output_dir, exist_ok=True)

    def _target(filename):
        return filename if output_dir in (None, '', '.') else os.path.join(output_dir, filename)

    with trace_span('phase:write_cache', kind='phase', dist_name=dist_name):
        # Write the legacy Format 2 minimal cache
        with trace_operation('yaml:dump', fetch_type='legacy_yaml', target=dist_name):
            legacy_data = yaml.dump(
                cache.get_legacy_data(include_source=include_source), Dumper=dumper
            ).encode()
        legacy_yaml_name = '%s-cache.yaml' % dist_name
        _write_cache_file(
            dist_name, legacy_yaml_name, _target(legacy_yaml_name),
            'legacy_yaml', legacy_data, compressed=False,
        )
        legacy_gz_name = '%s-cache.yaml.gz' % dist_name
        _write_cache_file(
            dist_name, legacy_gz_name, _target(legacy_gz_name),
            'legacy_gzip', legacy_data, compressed=True,
        )

        # Write the extended Format 3 cache when requested (e.g. --config or --extended)
        if write_extended:
            with trace_operation('yaml:dump', fetch_type='extended_yaml', target=dist_name):
                extended_data = yaml.dump(cache.get_data(version=3), Dumper=dumper).encode()
            extended_yaml_name = '%s-extended-cache.yaml' % dist_name
            _write_cache_file(
                dist_name, extended_yaml_name, _target(extended_yaml_name),
                'extended_yaml', extended_data, compressed=False,
            )
            extended_gz_name = '%s-extended-cache.yaml.gz' % dist_name
            _write_cache_file(
                dist_name, extended_gz_name, _target(extended_gz_name),
                'extended_gzip', extended_data, compressed=True,
            )


def _get_cached_distribution(
    index, dist_name, preclean=False, ignore_local=False,
    include_source=False, config=None, known_remote_refs=None, preloaded_cache=None,
    cache_dir=None
):
    print('Build cache for "%s"' % dist_name, flush=True)
    cache_config = CacheConfig.from_input(config, include_source=include_source)
    cache = None
    load_fetch_type = 'scratch'
    load_detail = None
    with trace_span('phase:load_cache', kind='phase', dist_name=dist_name):
        with trace_span(
            'cache:load',
            kind='fetch',
            resource_type='cache:load',
            target=dist_name,
            dist_name=dist_name,
            deferred_label=True,
        ) as load_span:
            try:
                if not preclean:
                    if not ignore_local:
                        if preloaded_cache is not None:
                            cache, candidate = preloaded_cache
                            load_fetch_type = 'local_preloaded'
                            load_detail = 'path=%s' % candidate
                        else:
                            candidate = _find_local_cache_file(dist_name, cache_dir=cache_dir)
                            if candidate is not None:
                                cache, _ = _load_local_cache_file(dist_name, path=candidate)
                                load_fetch_type = 'local_file'
                                load_detail = 'path=%s' % candidate
                    if not cache:
                        try:
                            cache = get_distribution_cache(index, dist_name, extended=True)
                            load_fetch_type = 'remote_cache'
                            load_detail = 'extended'
                        except Exception:
                            cache = get_distribution_cache(index, dist_name, extended=False)
                            load_fetch_type = 'remote_cache'
                            load_detail = 'legacy'
            except Exception as e:
                load_fetch_type = 'scratch'
                load_detail = 'fallback (%s)' % e
                logger.debug('Failed to fetch old cache for "%s": %s' % (dist_name, e))

            load_span.fetch_type = load_fetch_type
            load_span.status = 'ok'
            load_span.detail = load_detail

        report_fetch(
            'cache:load',
            dist_name,
            load_fetch_type,
            status='ok',
            duration_s=load_span.duration_s,
            dist_name=dist_name,
            detail=load_detail,
            level='top',
        )

    with trace_span('phase:update_cache', kind='phase', dist_name=dist_name):
        if cache:
            with trace_operation('cache:apply_config', target=dist_name):
                cache.apply_config(cache_config)
            # get current distribution file
            with trace_operation('cache:fetch_dist_file', target=dist_name):
                rel_file_data = _get_dist_file_data(index, dist_name, 'distribution')
            # since format 2 of the index file might contain a single value rather than a list
            if not isinstance(rel_file_data, list):
                rel_file_data = [rel_file_data]
            # if we're not including the source portion of the cache, strip it out of the existing cache
            # in order to skip the potentially lengthy cache invalidation process.
            if not (include_source or cache_config.has_any_source_enabled()):
                cache.source_repo_resources = {}
                cache.failed_source_repos = {}
            # update cache with current distribution file, which filters existing cache by validity.
            cache.update_distribution(rel_file_data, known_remote_refs=known_remote_refs)
        else:
            # get empty cache with distribution file
            with trace_operation('cache:fetch_dist_file', target=dist_name):
                distribution_file_data = _get_dist_file_data(index, dist_name, 'distribution')
            cache = DistributionCache(dist_name, distribution_file_data=distribution_file_data, config=cache_config)

    # get distribution
    return get_cached_distribution(index, dist_name, cache=cache, allow_lazy_load=True), cache
