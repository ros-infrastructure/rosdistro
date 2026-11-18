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

import os
import tempfile

from contextlib import contextmanager

from catkin_pkg.package import InvalidPackage, parse_package_string

from rosdistro.cache_profiler import trace_operation
from rosdistro.common import rmtree
from rosdistro.fetch_reporter import report_instant_sub_fetch, report_sub_fetch
from rosdistro.manifest_provider import SkipManifestProvider
from rosdistro.manifest_provider.cache import (
    DEFAULT_SOURCE_FILEPATHS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    list_local_repo_files,
    partition_repo_tree,
    sanitize_and_truncate_docs,
)
from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro.vcs import Git, normalize_git_url, ref_is_hash
from rosdistro import logger


def git_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    try:
        release_tag = repo.get_release_tag(pkg_name)
        with _temp_git_clone(repo.url, release_tag) as git_repo_path:
            filename = os.path.join(git_repo_path, *filepath.split('/'))
            if not os.path.exists(filename):
                if filepath != 'package.xml':
                    return None
                raise RuntimeError('Could not find %s in repository "%s"' % (filepath, repo.url))
            with open(filename, 'r') as f:
                return f.read()
    except Exception as e:
        raise RuntimeError('Unable to fetch %s: %s' % (filepath, e))


def git_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    xmlpath = 'package.xml'
    try:
        clone_ctx = _temp_git_clone(repo.url, repo.version)
        entered = False
        try:
            with report_sub_fetch('source:repo_tree', repo.name, 'git') as ctx:
                try:
                    git_repo_path = clone_ctx.__enter__()
                    entered = True
                    git_client = Git(git_repo_path)
                    # Include the git hash in our cache dictionary.
                    git_hash = git_client.command('rev-parse', 'HEAD')['output']
                    cache = SourceRepositoryCache.from_ref(git_hash)
                    with trace_operation('fs:list_repo_files', fetch_type='git', target=repo.name):
                        local_files = list_local_repo_files(git_repo_path)
                except BaseException:
                    ctx.status = 'failed'
                    raise

            with report_sub_fetch('source:partition_tree', repo.name, 'git'):
                package_xml_paths, package_assets, root_files = partition_repo_tree(local_files)

            if metadata is None or 'last_commit_time' in metadata:
                with report_sub_fetch('source:metadata:last_commit_time', repo.name, 'git') as ctx:
                    commit_time_res = git_client.command('log', '-1', '--format=%cI', 'HEAD')
                    if commit_time_res.get('returncode', 0) == 0 and commit_time_res.get('output'):
                        cache.set_metadata('last_commit_time', commit_time_res['output'].strip())
                    else:
                        ctx.status = 'not_found'

            for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
                if metadata is None or meta_key in metadata:
                    matching_candidates = [c for c in candidates if c in root_files]
                    if not matching_candidates:
                        report_instant_sub_fetch(
                            'source:metadata:%s' % meta_key, repo.name, 'git',
                            status='not_found', detail='not_in_tree',
                        )
                        continue
                    with report_sub_fetch('source:metadata:%s' % meta_key, repo.name, 'git') as ctx:
                        found_doc = False
                        for candidate in matching_candidates:
                            candidate_path = os.path.join(git_repo_path, candidate)
                            if os.path.isfile(candidate_path):
                                with open(candidate_path, 'r') as f:
                                    cache.set_metadata(meta_key, sanitize_and_truncate_docs(f.read()))
                                found_doc = True
                                break
                        if not found_doc:
                            ctx.status = 'not_found'

            # Find filepath files inside the repo.
            for package_path in package_xml_paths:
                local_pkg_dir = os.path.join(git_repo_path, *package_path.split('/')) if package_path else git_repo_path
                pkg_target = '%s/%s' % (repo.name, package_path) if package_path else repo.name
                with report_sub_fetch('source:package.xml', pkg_target, 'git') as ctx:
                    with open(os.path.join(local_pkg_dir, xmlpath), 'r') as f:
                        package_xml = f.read()
                    try:
                        name = parse_package_string(package_xml).name
                    except InvalidPackage:
                        ctx.status = 'failed'
                        raise RuntimeError('Unable to parse %s file found in %s' % (xmlpath, repo.url))
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
                            'source:%s' % filepath, pkg_label, 'git',
                            status='ok', detail='count=%d' % len(assets),
                        )
                        continue
                    repo_filename = os.path.join(local_pkg_dir, *filepath.split('/'))
                    if not os.path.exists(repo_filename):
                        report_instant_sub_fetch(
                            'source:%s' % filepath, pkg_label, 'git',
                            status='not_found', detail='not_in_tree',
                        )
                        continue
                    with report_sub_fetch('source:%s' % filepath, pkg_label, 'git'):
                        with open(repo_filename, 'r') as f:
                            contents = f.read()
                    cache.add(name, package_path, contents, filepath)
        finally:
            if entered:
                clone_ctx.__exit__(None, None, None)

    except Exception as e:
        raise RuntimeError('Unable to fetch source files: %s' % (e))

    return cache


@contextmanager
def _temp_git_clone(url, ref):
    base = tempfile.mkdtemp('rosdistro')
    git = Git(cwd=base)
    clone_url = normalize_git_url(url)
    try:
        cloned = False
        if git.version_gte('1.8.0') and not ref_is_hash(ref):
            # Directly clone the required ref with least amount of additional history.
            # Available since git 1.8.0, but only works for tags and branches, not hashes:
            # https://git.kernel.org/cgit/git/git.git/tree/Documentation/git-clone.txt?h=v1.8.0#n158
            clone_args = ('clone', clone_url, '.', '--depth', '1')
            if ref:
                clone_args += ('--branch', ref)
            result = git.command(*clone_args)
            cloned = result['returncode'] == 0
            if not cloned:
                logger.debug(
                    'Shallow clone of "%s" at reference "%s" failed, '
                    'falling back to full clone.' % (url, ref))
                rmtree(base)
                base = None
                base = tempfile.mkdtemp('rosdistro')
                git = Git(cwd=base)

        if not cloned:
            # Old git doesn't support cloning a tag/branch directly, or shallow clone failed,
            # so full clone and checkout.
            result = git.command('clone', clone_url, '.')
            if result['returncode'] != 0:
                error_msg = 'Could not clone repository "%s"' % url
                if result.get('error'):
                    error_msg += ': %s' % result['error']
                raise RuntimeError(error_msg)

            if ref:
                result = git.command('checkout', ref)
                if result['returncode'] != 0:
                    error_msg = 'Could not checkout ref "%s" of repository "%s"' % (ref, url)
                    if result.get('error'):
                        error_msg += ': %s' % result['error']
                    raise RuntimeError(error_msg)

        yield base
    finally:
        if base and os.path.exists(base):
            rmtree(base)
