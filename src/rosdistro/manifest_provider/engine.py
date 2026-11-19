# Software License Agreement (BSD License)
#
# Copyright (c) 2026, Open Source Robotics Foundation, Inc.
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

import posixpath
import socket
from typing import Any, Dict, FrozenSet, NamedTuple, Optional, Tuple
from urllib.error import URLError

from catkin_pkg.package import InvalidPackage, parse_package_string

from rosdistro.fetch_reporter import report_instant_sub_fetch, report_sub_fetch
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.sanitize import sanitize_and_truncate_docs
from rosdistro.manifest_provider.tree import (
    DEFAULT_SOURCE_FILEPATHS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    partition_repo_tree,
)
from rosdistro.source_repository_cache import SourceRepositoryCache


class ManifestQuery(NamedTuple):
    """Immutable query profile describing what a manifest provider should fetch."""

    repo: Any
    ref: Optional[str]
    ref_is_tag: bool
    package_name: Optional[str]
    subpath: str
    filepaths: Tuple[str, ...]
    metadata: Optional[Tuple[str, ...]]
    label: str

    @classmethod
    def for_release(cls, repo, pkg_name, filepaths=('package.xml',)):
        """Build a direct-strategy query profile for a single released package."""
        release_tag = repo.get_release_tag(pkg_name)
        if getattr(repo, 'type', 'git') == 'tar':
            ref = None
            ref_is_tag = False
            subpath = release_tag
        else:
            ref = release_tag
            ref_is_tag = True
            subpath = ''
        return cls(
            repo=repo,
            ref=ref,
            ref_is_tag=ref_is_tag,
            package_name=pkg_name,
            subpath=subpath,
            filepaths=tuple(filepaths),
            metadata=(),
            label='release',
        )

    @classmethod
    def for_source(cls, repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
        """Build a tree-strategy query profile for a source or doc repository."""
        resolved_filepaths = (
            tuple(DEFAULT_SOURCE_FILEPATHS) if filepaths is None else tuple(filepaths)
        )
        resolved_metadata = None if metadata is None else tuple(metadata)
        return cls(
            repo=repo,
            ref=getattr(repo, 'version', None),
            ref_is_tag=False,
            package_name=None,
            subpath='',
            filepaths=resolved_filepaths,
            metadata=resolved_metadata,
            label='source',
        )

    @property
    def discover(self):
        return self.package_name is None

    @property
    def needs_tree(self):
        if self.discover:
            return True
        if any(fp in PACKAGE_ASSET_LIST_RESOURCES for fp in self.filepaths):
            return True
        if self.metadata is None or any(m in ROOT_DOC_CANDIDATES for m in self.metadata):
            return True
        return False

    @property
    def required(self) -> FrozenSet[str]:
        if self.discover:
            return frozenset(('package.xml',))
        return frozenset(fp for fp in self.filepaths if fp == 'package.xml')

    @property
    def has_metadata_request(self):
        return self.metadata is None or len(self.metadata) > 0

    def wants_metadata(self, key):
        return self.metadata is None or key in self.metadata


class PackageManifest(NamedTuple):
    name: str
    package_path: str
    files: Dict[str, Any]


class ManifestResult(object):
    """Structured result returned by ``execute_query()``."""

    def __init__(self, packages, ref=None, metadata=None, errors=None):
        self.packages = packages
        self.ref = ref
        self.metadata = dict(metadata) if metadata else {}
        self.errors = dict(errors) if errors else {}

    def file(self, filepath, pkg_name=None):
        """Return fetched contents for *filepath*, re-raising any recorded error for it."""
        if filepath in self.errors:
            raise self.errors[filepath]
        if pkg_name is None:
            if len(self.packages) != 1:
                raise KeyError('Package name required when result contains %d packages' % len(self.packages))
            pkg_manifest = next(iter(self.packages.values()))
        else:
            pkg_manifest = self.packages[pkg_name]
        return pkg_manifest.files.get(filepath)

    def to_source_repository_cache(self):
        cache = SourceRepositoryCache.from_ref(self.ref)
        for meta_key, meta_val in self.metadata.items():
            cache.set_metadata(meta_key, meta_val)
        for pkg_name, pkg_manifest in self.packages.items():
            pkg_path = pkg_manifest.package_path
            if 'package.xml' in pkg_manifest.files:
                cache.add(pkg_name, pkg_path, pkg_manifest.files['package.xml'], 'package.xml')
            for filepath, content in pkg_manifest.files.items():
                if filepath == 'package.xml':
                    continue
                cache.add(pkg_name, pkg_path, content, filepath)
        return cache


class ManifestSession(object):
    """Per-query transport session opened by a ``ManifestProvider``."""

    ref = None

    def list_files(self):
        raise SkipManifestProvider('tree listing not supported')

    def read_file(self, path):
        raise NotImplementedError()

    def fetch_metadata(self, keys):
        return {}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class ManifestProvider(object):
    """Stateless transport provider capable of executing ``ManifestQuery`` profiles."""

    name = None

    def accepts(self, repo):
        raise NotImplementedError()

    def open(self, query):
        raise NotImplementedError()

    def is_permanent_error(self, err, repo):
        return False

    def missing_is_permanent(self, repo):
        return False


def provider_for(obj):
    """Return the underlying ``ManifestProvider`` for *obj*, or ``None`` for legacy callables."""
    if isinstance(obj, ManifestProvider):
        return obj
    # Inspect __dict__ directly so unittest.mock.MagicMock never fools detection.
    obj_dict = getattr(obj, '__dict__', None)
    if isinstance(obj_dict, dict):
        candidate = obj_dict.get('_unified_provider')
        if isinstance(candidate, ManifestProvider):
            return candidate
    return None


def _execute_tree_query(provider, session, query):
    repo = query.repo
    label = query.label

    with report_sub_fetch('%s:repo_tree' % label, repo.name, provider.name) as ctx:
        try:
            blob_paths = session.list_files()
        except SkipManifestProvider:
            ctx.status = 'skip'
            raise
        except PermanentManifestError:
            ctx.status = 'failed'
            raise
        except Exception as e:
            ctx.status = 'failed'
            if provider.is_permanent_error(e, repo):
                raise PermanentManifestError(str(e)) from e
            raise

    blob_path_set = {p.replace('\\', '/').strip('/') for p in blob_paths if p and p.strip('/')}
    with report_sub_fetch('%s:partition_tree' % label, repo.name, provider.name):
        package_xml_paths, package_assets, root_files = partition_repo_tree(blob_paths)

    metadata = {}
    if query.has_metadata_request:
        non_doc_keys = (
            None
            if query.metadata is None
            else tuple(k for k in query.metadata if k not in ROOT_DOC_CANDIDATES)
        )
        if non_doc_keys is None or len(non_doc_keys) > 0:
            fetched_meta = session.fetch_metadata(non_doc_keys) or {}
            for meta_key, meta_val in fetched_meta.items():
                if query.wants_metadata(meta_key):
                    metadata[meta_key] = meta_val

        for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
            if not query.wants_metadata(meta_key):
                continue
            matching_candidates = [c for c in candidates if c in root_files]
            if not matching_candidates:
                report_instant_sub_fetch(
                    '%s:metadata:%s' % (label, meta_key),
                    repo.name,
                    provider.name,
                    status='not_found',
                    detail='not_in_tree',
                )
                continue
            with report_sub_fetch('%s:metadata:%s' % (label, meta_key), repo.name, provider.name) as ctx:
                found_doc = False
                had_error = False
                for candidate in matching_candidates:
                    try:
                        doc_contents = session.read_file(candidate)
                    except (URLError, TimeoutError, socket.timeout, OSError, RuntimeError):
                        had_error = True
                        continue
                    if doc_contents is None:
                        continue
                    # Root-doc truncation is preserved here until commit 12 moves it solely
                    # into CachedSourceManifestProvider.
                    metadata[meta_key] = sanitize_and_truncate_docs(doc_contents)
                    found_doc = True
                    break
                if not found_doc:
                    ctx.status = 'failed' if had_error else 'not_found'

    packages = {}
    for package_path in package_xml_paths:
        xml_rel = posixpath.join(package_path, 'package.xml') if package_path else 'package.xml'
        pkg_target = '%s/%s' % (repo.name, package_path) if package_path else repo.name
        with report_sub_fetch('%s:package.xml' % label, pkg_target, provider.name) as ctx:
            try:
                package_xml = session.read_file(xml_rel)
            except Exception as e:
                ctx.status = 'failed'
                raise RuntimeError('Unable to fetch package.xml from %s: %s' % (repo.url, e)) from e
            if package_xml is None:
                ctx.status = 'failed'
                raise RuntimeError('Unable to find package.xml at "%s" in %s' % (xml_rel, repo.url))
            try:
                name = parse_package_string(package_xml).name
            except InvalidPackage as e:
                ctx.status = 'failed'
                raise RuntimeError('Unable to parse package.xml file found in %s' % repo.url) from e
            ctx.target = '%s/%s' % (repo.name, name)

        pkg_files = {'package.xml': package_xml}
        pkg_label = '%s/%s' % (repo.name, name)
        for filepath in query.filepaths:
            if filepath == 'package.xml':
                continue
            if filepath in PACKAGE_ASSET_LIST_RESOURCES:
                assets = package_assets.get(package_path, {}).get(filepath, [])
                pkg_files[filepath] = assets
                report_instant_sub_fetch(
                    '%s:%s' % (label, filepath),
                    pkg_label,
                    provider.name,
                    status='ok',
                    detail='count=%d' % len(assets),
                )
                continue
            rel_target = (
                posixpath.join(package_path, filepath) if package_path else filepath
            ).replace('\\', '/').strip('/')
            if rel_target not in blob_path_set:
                report_instant_sub_fetch(
                    '%s:%s' % (label, filepath),
                    pkg_label,
                    provider.name,
                    status='not_found',
                    detail='not_in_tree',
                )
                continue
            with report_sub_fetch('%s:%s' % (label, filepath), pkg_label, provider.name) as ctx:
                try:
                    contents = session.read_file(rel_target)
                except Exception as e:
                    ctx.status = 'failed'
                    raise RuntimeError(
                        'Unable to fetch %s from %s: %s' % (filepath, repo.url, e)
                    ) from e
                if contents is None:
                    ctx.status = 'not_found'
                    continue
            pkg_files[filepath] = contents

        packages[name] = PackageManifest(name=name, package_path=package_path, files=pkg_files)

    return ManifestResult(packages=packages, ref=session.ref, metadata=metadata)


def _execute_direct_query(provider, session, query):
    repo = query.repo
    pkg_name = query.package_name
    required = query.required

    # Fetch required files first so missing/broken package.xml aborts before optional files.
    ordered_filepaths = [fp for fp in query.filepaths if fp in required] + [
        fp for fp in query.filepaths if fp not in required
    ]

    pkg_files = {}
    errors = {}
    for filepath in ordered_filepaths:
        rel_path = posixpath.join(query.subpath, filepath) if query.subpath else filepath
        is_required = filepath in required
        try:
            contents = session.read_file(rel_path)
        except PermanentManifestError as e:
            if is_required:
                raise
            errors[filepath] = e
            continue
        except Exception as e:
            classified = (
                PermanentManifestError('Failed to fetch "%s" from "%s": %s' % (rel_path, repo.url, e))
                if provider.is_permanent_error(e, repo)
                else e
            )
            if is_required:
                if classified is not e:
                    raise classified from e
                raise
            errors[filepath] = classified
            continue

        if contents is None:
            if is_required:
                if provider.missing_is_permanent(repo):
                    raise PermanentManifestError(
                        'Failed to fetch "%s" from "%s"' % (rel_path, repo.url)
                    )
                raise RuntimeError('Could not find %s in "%s"' % (rel_path, repo.url))
            pkg_files[filepath] = None
        else:
            pkg_files[filepath] = contents

    packages = {pkg_name: PackageManifest(name=pkg_name, package_path=query.subpath, files=pkg_files)}
    return ManifestResult(packages=packages, ref=session.ref, metadata={}, errors=errors)


def execute_query(provider, query):
    """Execute *query* against *provider* and return a ``ManifestResult``."""
    provider.accepts(query.repo)
    with provider.open(query) as session:
        if query.discover:
            return _execute_tree_query(provider, session, query)
        return _execute_direct_query(provider, session, query)


def fetch_release_file(provider, repo, pkg_name, filepath='package.xml'):
    """Legacy-compatible helper fetching one release file through *provider*."""
    provider.accepts(repo)
    query = ManifestQuery.for_release(repo, pkg_name, filepaths=(filepath,))
    result = execute_query(provider, query)
    return result.file(filepath, pkg_name=pkg_name)


def fetch_source_cache(provider, repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    """Legacy-compatible helper fetching a ``SourceRepositoryCache`` through *provider*."""
    provider.accepts(repo)
    query = ManifestQuery.for_source(repo, filepaths=filepaths, metadata=metadata)
    result = execute_query(provider, query)
    return result.to_source_repository_cache()
