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

from rosdistro.common import rmtree
from rosdistro.manifest_provider.cache import (
    DEFAULT_SOURCE_FILEPATHS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    list_local_repo_files,
    partition_repo_tree,
    sanitize_and_truncate_docs,
)
from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro.vcs import Git, ref_is_hash
from rosdistro import logger


def git_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    try:
        release_tag = repo.get_release_tag(pkg_name)
        with _temp_git_clone(repo.url, release_tag) as git_repo_path:
            filename = os.path.join(git_repo_path, filepath)
            if not os.path.exists(filename):
                if filepath != 'package.xml':
                    return None
                raise RuntimeError('Could not find %s in repository "%s"' % (filepath, repo.url))
            with open(filename, 'r') as f:
                return f.read()
    except Exception as e:
        raise RuntimeError('Unable to fetch %s: %s' % (filepath, e))


def git_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    xmlpath = 'package.xml'
    try:
        with _temp_git_clone(repo.url, repo.version) as git_repo_path:
            logger.debug(f'Cloning repository {repo.url} to get source info')
            git_client = Git(git_repo_path)
            # Include the git hash in our cache dictionary.
            git_hash = git_client.command('rev-parse', 'HEAD')['output']
            cache = SourceRepositoryCache.from_ref(git_hash)

            package_xml_paths, package_assets, root_files = partition_repo_tree(
                list_local_repo_files(git_repo_path)
            )

            if metadata is None or 'last_commit_time' in metadata:
                commit_time_res = git_client.command('log', '-1', '--format=%cI', 'HEAD')
                if commit_time_res.get('returncode', 0) == 0 and commit_time_res.get('output'):
                    cache.set_metadata('last_commit_time', commit_time_res['output'].strip())

            for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
                if metadata is None or meta_key in metadata:
                    for candidate in candidates:
                        if candidate not in root_files:
                            continue
                        candidate_path = os.path.join(git_repo_path, candidate)
                        if os.path.isfile(candidate_path):
                            with open(candidate_path, 'r') as f:
                                cache.set_metadata(meta_key, sanitize_and_truncate_docs(f.read()))
                            break

            # Find filepath files inside the repo.
            for package_path in package_xml_paths:
                with open(os.path.join(git_repo_path, package_path, xmlpath), 'r') as f:
                    package_xml = f.read()
                try:
                    name = parse_package_string(package_xml).name
                except InvalidPackage:
                    raise RuntimeError('Unable to parse %s file found in %s' % (xmlpath, repo.url))
                cache.add(name, package_path, package_xml, xmlpath)
                for filepath in filepaths:
                    if filepath == xmlpath:
                        continue
                    if filepath in PACKAGE_ASSET_LIST_RESOURCES:
                        cache.add(
                            name,
                            package_path,
                            package_assets.get(package_path, {}).get(filepath, []),
                            filepath,
                        )
                        continue
                    repo_filename = os.path.join(git_repo_path, package_path, filepath)
                    if not os.path.exists(repo_filename):
                        logger.debug(f'- git load of {filepath} from {repo.url} at {repo.version} skipped because it did not exist.')
                        continue
                    with open(repo_filename, 'r') as f:
                        logger.debug('- git load %s from %s' % (filepath, repo_filename))
                        contents = f.read()
                        cache.add(name, package_path, contents, filepath)

    except Exception as e:
        raise RuntimeError('Unable to fetch source files: %s' % (e))

    return cache


@contextmanager
def _temp_git_clone(url, ref):
    base = tempfile.mkdtemp('rosdistro')
    git = Git(cwd=base)
    try:
        if git.version_gte('1.8.0') and not ref_is_hash(ref):
            # Directly clone the required ref with least amount of additional history.
            # Available since git 1.8.0, but only works for tags and branches, not hashes:
            # https://git.kernel.org/cgit/git/git.git/tree/Documentation/git-clone.txt?h=v1.8.0#n158
            result = git.command('clone', url, '.', '--depth', '1', '--branch', ref)
            if result['returncode'] != 0:
                raise RuntimeError('Could not clone repository "%s" at reference "%s"' % (url, ref))
        else:
            # Old git doesn't support cloning a tag/branch directly, so full clone and checkout.
            result = git.command('clone', url, '.')
            if result['returncode'] != 0:
                raise RuntimeError('Could not clone repository "%s"' % url)

            result = git.command('checkout', ref)
            if result['returncode'] != 0:
                raise RuntimeError('Could not checkout ref "%s" of repository "%s"' % (ref, url))

        yield base
    finally:
        rmtree(base)
