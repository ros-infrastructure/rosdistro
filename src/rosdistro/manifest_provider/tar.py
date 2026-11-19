# Software License Agreement (BSD License)
#
# Copyright (c) 2020, Canonical Ltd.
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
#  * Neither the name of Canonical Ltd. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
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

import base64
import io
import os
import posixpath
import tarfile
import tempfile
import urllib
from urllib.request import urlopen, Request

from catkin_pkg.package import InvalidPackage, parse_package_string

from rosdistro.cache_profiler import sanitize_url_for_trace, trace_operation
from rosdistro.common import rmtree
from rosdistro.fetch_reporter import report_instant_sub_fetch, report_sub_fetch
from rosdistro.manifest_provider import SkipManifestProvider, get_http_timeout
from rosdistro.manifest_provider.sanitize import sanitize_and_truncate_docs
from rosdistro.manifest_provider.tree import (
    DEFAULT_SOURCE_FILEPATHS,
    IGNORE_MARKERS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    partition_repo_tree,
)
from rosdistro.source_repository_cache import SourceRepositoryCache

_TAR_USER = os.getenv('TAR_USER', None)
_TAR_PASSWORD = os.getenv('TAR_PASSWORD', None)

def tar_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    if repo.type != 'tar':
        raise SkipManifestProvider('Cannot handle non-tar repository.')

    subdir = repo.get_release_tag(pkg_name)

    request = Request(repo.url)
    if _TAR_USER and _TAR_PASSWORD:
        credential_pair = '%s:%s' % (_TAR_USER, _TAR_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        request.add_header('Authorization', authheader)
    elif _TAR_PASSWORD:
        request.add_header('Private-Token', _TAR_PASSWORD)

    with trace_operation('http:get', fetch_type='tar', target=sanitize_url_for_trace(repo.url)):
        with urlopen(request, timeout=get_http_timeout()) as response:
            tar_bytes = response.read()
    with trace_operation('tar:extract', fetch_type='tar', target=pkg_name):
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            try:
                extracted = tar.extractfile(subdir + '/' + filepath)
            except KeyError:
                if filepath != 'package.xml':
                    return None
                raise RuntimeError('Could not find %s in tarball "%s"' % (subdir + '/' + filepath, repo.url))
            if extracted is None:
                if filepath != 'package.xml':
                    return None
                raise RuntimeError('Could not extract %s from tarball "%s"' % (subdir + '/' + filepath, repo.url))
            package_xml = extracted.read()
            return package_xml.decode('utf-8')


def tar_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    if repo.type != 'tar':
        raise SkipManifestProvider('Cannot handle non-tar repository.')
    xmlpath = 'package.xml'

    try:
        request = Request(repo.url)
        if _TAR_USER and _TAR_PASSWORD:
            credential_pair = '%s:%s' % (_TAR_USER, _TAR_PASSWORD)
            authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
            request.add_header('Authorization', authheader)
        elif _TAR_PASSWORD:
            request.add_header('Private-Token', _TAR_PASSWORD)

        with report_sub_fetch('source:repo_tree', repo.name, 'tar'):
            with trace_operation('http:get', fetch_type='tar', target=sanitize_url_for_trace(repo.url)):
                with urlopen(request, timeout=get_http_timeout()) as response:
                    tar_bytes = response.read()
            tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))

        with tar:
            tmpdir = tempfile.mkdtemp()
            try:
                blob_paths = [
                    m.name.lstrip('./')
                    for m in tar.getmembers()
                    if m.isfile()
                ]
                with report_sub_fetch('source:partition_tree', repo.name, 'tar'):
                    package_xml_paths, package_assets, root_files = partition_repo_tree(blob_paths)

                # Extract the package.xmls and requested filepaths
                with trace_operation('tar:extract', fetch_type='tar', target=repo.name):
                    tar.extractall(path=tmpdir, members=_package_xml_members(tar, filepaths))
                cache = SourceRepositoryCache.from_ref(None)

                for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
                    if metadata is None or meta_key in metadata:
                        matching_candidates = [c for c in candidates if c in root_files]
                        if not matching_candidates:
                            report_instant_sub_fetch(
                                'source:metadata:%s' % meta_key, repo.name, 'tar',
                                status='not_found', detail='not_in_tree',
                            )
                            continue
                        with report_sub_fetch('source:metadata:%s' % meta_key, repo.name, 'tar') as ctx:
                            found_doc = False
                            for candidate in matching_candidates:
                                candidate_path = os.path.join(tmpdir, candidate)
                                if os.path.isfile(candidate_path):
                                    with open(candidate_path, 'r') as f:
                                        cache.set_metadata(meta_key, sanitize_and_truncate_docs(f.read()))
                                    found_doc = True
                                    break
                            if not found_doc:
                                ctx.status = 'not_found'

                for package_path in package_xml_paths:
                    local_pkg_dir = os.path.join(tmpdir, *package_path.split('/')) if package_path else tmpdir
                    pkg_target = '%s/%s' % (repo.name, package_path) if package_path else repo.name
                    with report_sub_fetch('source:package.xml', pkg_target, 'tar') as ctx:
                        with open(os.path.join(local_pkg_dir, xmlpath), 'r') as f:
                            package_xml = f.read()
                        try:
                            name = parse_package_string(package_xml).name
                        except InvalidPackage:
                            ctx.status = 'failed'
                            raise RuntimeError('Unable to parse package.xml file found in %s' % repo.url)
                        ctx.target = '%s/%s' % (repo.name, name)
                    cache.add(name, package_path, package_xml, xmlpath)
                    pkg_label = '%s/%s' % (repo.name, name)
                    for filepath in filepaths:
                        if filepath == xmlpath:
                            continue
                        if filepath in PACKAGE_ASSET_LIST_RESOURCES:
                            assets = package_assets.get(package_path, {}).get(filepath, [])
                            cache.add(name, package_path, assets, filepath)
                            report_instant_sub_fetch(
                                'source:%s' % filepath, pkg_label, 'tar',
                                status='ok', detail='count=%d' % len(assets),
                            )
                            continue
                        repo_filename = os.path.join(local_pkg_dir, *filepath.split('/'))
                        if not os.path.isfile(repo_filename):
                            report_instant_sub_fetch(
                                'source:%s' % filepath, pkg_label, 'tar',
                                status='not_found', detail='not_in_tree',
                            )
                            continue
                        with report_sub_fetch('source:%s' % filepath, pkg_label, 'tar'):
                            with open(repo_filename, 'r') as f:
                                contents = f.read()
                        cache.add(name, package_path, contents, filepath)

                return cache
            finally:
                rmtree(tmpdir)
    except Exception as e:
        raise RuntimeError('Unable to fetch source package.xml files: %s' % e)


def _package_xml_members(tar, filepaths=None):
    target_filenames = {'package.xml'} | set(IGNORE_MARKERS)
    for candidates in ROOT_DOC_CANDIDATES.values():
        target_filenames.update(candidates)
    if filepaths:
        target_filenames.update(
            fp for fp in filepaths if fp not in PACKAGE_ASSET_LIST_RESOURCES
        )
    for tarinfo in tar:
        if posixpath.basename(tarinfo.name) in target_filenames or any(
            tarinfo.name == fp or tarinfo.name.endswith('/' + fp) for fp in target_filenames
        ):
            yield tarinfo
