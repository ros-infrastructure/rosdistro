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

import base64
import json
import os
import time

from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.error import URLError

from catkin_pkg.package import parse_package_string

from rosdistro import logger
from rosdistro.manifest_provider import SkipManifestProvider
from rosdistro.manifest_provider.cache import (
    DEFAULT_SOURCE_FILEPATHS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    partition_repo_tree,
    sanitize_and_truncate_docs,
)
from rosdistro.source_repository_cache import SourceRepositoryCache

GITHUB_USER = os.getenv('GITHUB_USER', None)
GITHUB_PASSWORD = os.getenv('GITHUB_PASSWORD', None)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', None)

ROSDISTRO_RETRY_MAX = int(os.getenv('ROSDISTRO_RETRY_MAX', '120'))

GITHUB_REPO_METADATA_FIELDS = {
    'stars': 'stargazers_count',
    'description': 'description',
    'tags': 'topics',
    'last_commit_time': 'pushed_at',
}


def _get_url_contents(url):
    backoff = 1
    while backoff < ROSDISTRO_RETRY_MAX:
        try:
            return urlopen(url).read().decode('utf-8')
        except HTTPError as e:
            if e.code != 403:
                raise e
            url_str = getattr(url, 'full_url', url)
            logger.debug(f'Fetch of {url_str} failed with 403, assuming rate limit, retrying after period {backoff} seconds.')
        time.sleep(backoff)
        backoff *= 1.5
    # Last return after timeout to collect the error object again.
    return urlopen(url).read().decode('utf-8')


def github_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        logger.debug('Skip non-github url "%s"' % repo.url)
        raise SkipManifestProvider('can not handle non github urls')

    release_tag = repo.get_release_tag(pkg_name)

    if not repo.has_remote_tag(release_tag):
        raise RuntimeError('specified tag "%s" is not a git tag' % release_tag)

    url = 'https://raw.githubusercontent.com/%s/%s/%s' % (path, release_tag, filepath)
    try:
        logger.debug('Load %s file from url "%s"' % (filepath, url))
        return _get_url_contents(url)
    except HTTPError as e:
        if e.code == 404 and filepath != 'package.xml':
            logger.debug('- File not found (%s), trying "%s"' % (e, url))
            return None
        logger.debug('- HTTP ERROR (%s), trying "%s"' % (e, url))
        raise e
    except URLError as e:
        logger.debug('- failed (%s), trying "%s"' % (e, url))
        raise RuntimeError()


def github_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        logger.debug('Skip non-github url "%s"' % repo.url)
        raise SkipManifestProvider('can not handle non github urls')

    tree_url = 'https://api.github.com/repos/%s/git/trees/%s?recursive=1' % (path, repo.version)
    req = Request(tree_url)
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    if GITHUB_USER and GITHUB_PASSWORD:
        logger.debug('- using http basic auth from supplied environment variables.')
        credential_pair = '%s:%s' % (GITHUB_USER, GITHUB_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        req.add_header('Authorization', authheader)
    try:
        tree_json = json.loads(_get_url_contents(req))
        logger.debug('- load repo tree from %s' % tree_url)
    except URLError as e:
        raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e))

    if tree_json['truncated']:
        raise RuntimeError('JSON tree is truncated, must perform full clone.')

    blob_paths = [
        obj['path']
        for obj in tree_json['tree']
        if obj.get('type', 'blob') not in ('tree', 'commit')
    ]
    package_xml_paths, package_assets, root_files = partition_repo_tree(blob_paths)

    cache = SourceRepositoryCache.from_ref(tree_json['sha'])

    # Fetch repository info for configured metadata (stars, description, tags, last_commit_time, etc.)
    if metadata is None or len(metadata) > 0:
        if metadata is None or any(k in metadata for k in GITHUB_REPO_METADATA_FIELDS):
            repo_url = 'https://api.github.com/repos/%s' % path
            repo_req = Request(repo_url)
            if GITHUB_TOKEN:
                repo_req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
            if GITHUB_USER and GITHUB_PASSWORD:
                credential_pair = '%s:%s' % (GITHUB_USER, GITHUB_PASSWORD)
                authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
                repo_req.add_header('Authorization', authheader)
            try:
                repo_json = json.loads(_get_url_contents(repo_req))
                for meta_key, api_field in GITHUB_REPO_METADATA_FIELDS.items():
                    if (metadata is None or meta_key in metadata) and api_field in repo_json:
                        cache.set_metadata(meta_key, repo_json[api_field])
            except URLError as e:
                logger.debug('- failed to load repository info from %s: %s' % (repo_url, e))

        for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
            if metadata is None or meta_key in metadata:
                for candidate in candidates:
                    if candidate not in root_files:
                        continue
                    doc_url = 'https://raw.githubusercontent.com/%s/%s/%s' % (path, cache.ref(), candidate)
                    logger.debug('- load repo %s from %s' % (candidate, doc_url))
                    try:
                        doc_contents = _get_url_contents(doc_url)
                    except URLError as e:
                        logger.debug('- failed to load %s from %s: %s' % (candidate, doc_url, e))
                        continue
                    cache.set_metadata(meta_key, sanitize_and_truncate_docs(doc_contents))
                    break

    for package_xml_path in package_xml_paths:
        package_xml_filename = 'package.xml'
        url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
            (path, cache.ref(), package_xml_path + '/' + package_xml_filename if package_xml_path else package_xml_filename)
        logger.debug('- load %s from %s' % (package_xml_filename, url))
        package_xml = _get_url_contents(url)
        name = parse_package_string(package_xml).name
        cache.add(name, package_xml_path, package_xml, package_xml_filename)
        for filepath in filepaths:
            if filepath == package_xml_filename:
                continue
            if filepath in PACKAGE_ASSET_LIST_RESOURCES:
                cache.add(
                    name,
                    package_xml_path,
                    package_assets.get(package_xml_path, {}).get(filepath, []),
                    filepath,
                )
                continue
            url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
                (path, cache.ref(), package_xml_path + '/' + filepath if package_xml_path else filepath)
            logger.debug('- load %s from %s' % (filepath, url))
            try:
                contents = _get_url_contents(url)
            except HTTPError as e:
                if e.code == 404:
                    logger.debug('- File not found (%s), skipping "%s"' % (e, url))
                    continue
                logger.debug('- HTTP ERROR (%s), trying "%s"' % (e, url))
                raise e
            cache.add(name, package_xml_path, contents, filepath)

    return cache
