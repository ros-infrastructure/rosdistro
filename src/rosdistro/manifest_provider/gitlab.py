# Software License Agreement (BSD License)
#
# Copyright (c) 2021, Open Source Robotics Foundation, Inc.
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

import json
import os
import re
import socket
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlencode

from catkin_pkg.package import parse_package_string

from rosdistro import logger
from rosdistro.manifest_provider import (
    PermanentManifestError,
    SkipManifestProvider,
    get_http_header,
    get_http_timeout,
    is_unauthenticated_http_url,
    read_http_error_body,
)
from rosdistro.manifest_provider.cache import (
    DEFAULT_SOURCE_FILEPATHS,
    PACKAGE_ASSET_LIST_RESOURCES,
    ROOT_DOC_CANDIDATES,
    partition_repo_tree,
    sanitize_and_truncate_docs,
)
from rosdistro.source_repository_cache import SourceRepositoryCache

GITLAB_PRIVATE_TOKEN = os.getenv('GITLAB_PRIVATE_TOKEN', None)
ROSDISTRO_GITLAB_SERVER = os.getenv('ROSDISTRO_GITLAB_SERVER', None)
DEFAULT_GITLAB_PAGINATION_TIMEOUT = 120.0

GITLAB_REPO_METADATA_FIELDS = {
    'stars': 'star_count',
    'description': 'description',
    'tags': 'tag_list',
    'last_commit_time': 'last_activity_at',
}


def _get_pagination_timeout():
    raw = os.getenv('ROSDISTRO_GITLAB_PAGINATION_TIMEOUT')
    if raw is None or raw == '':
        return DEFAULT_GITLAB_PAGINATION_TIMEOUT
    timeout = float(raw)
    return timeout if timeout > 0 else None


def _gitlab_urlopen(url):
    req = Request(url)
    if GITLAB_PRIVATE_TOKEN:
        req.add_header('Private-Token', GITLAB_PRIVATE_TOKEN)
    logger.debug('Performing GitLab API query "%s"' % (url,))
    return urlopen(req, timeout=get_http_timeout())


def _gitlab_api_query(server, path, resource, attrs):
    url = 'https://%s/api/v4/projects/%s/%s' % (server, urlquote(path, safe=''), resource)
    if attrs:
        url += '?' + urlencode(attrs)
    return _gitlab_urlopen(url)


def _gitlab_paged_api_query(server, path, resource, attrs):
    _attrs = {
        'per_page': 50,
        **attrs,
        'pagination': 'keyset',
        'page': '1',
    }

    url = 'https://%s/api/v4/projects/%s/%s' % (server, urlquote(path, safe=''), resource)
    if _attrs:
        url += '?' + urlencode(_attrs)

    pagination_timeout = _get_pagination_timeout()
    deadline = (time.monotonic() + pagination_timeout) if pagination_timeout is not None else None
    seen_urls = set()

    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise RuntimeError(
                'GitLab paginated API query "%s" timed out after %g seconds' %
                (url, pagination_timeout)
            )
        if url in seen_urls:
            logger.debug('Detected circular GitLab pagination link "%s", stopping.' % (url,))
            break
        seen_urls.add(url)
        with _gitlab_urlopen(url) as res:
            for result in json.loads(res.read().decode('utf-8')):
                yield result

            # Get the URL to the next page
            links = res.getheader('Link')
            if not links:
                break
            match = re.match(r'.*<([^>]*)>; rel="next"', links)
            if not match:
                break
            url = match.group(1)


def _is_definitive_gitlab_failure(err, repo_url):
    """Return True when a GitLab HTTPError means git clone will also fail."""
    if not is_unauthenticated_http_url(repo_url):
        return False
    code = getattr(err, 'code', None)
    if code in (404, 410, 451):
        return True
    if code == 403:
        headers = getattr(err, 'headers', None)
        if get_http_header(headers, 'Retry-After') is not None:
            return False
        remaining = get_http_header(headers, 'RateLimit-Remaining')
        if remaining == '0':
            return False
        body = read_http_error_body(err)
        lower_body = body.lower()
        if 'rate limit' in lower_body or 'too many' in lower_body or 'abuse' in lower_body:
            return False
        if remaining is not None or body:
            return True
    return False


def gitlab_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('gitlab.com') and server != ROSDISTRO_GITLAB_SERVER:
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise SkipManifestProvider('can not handle non gitlab urls')

    resource = 'repository/files/%s/raw' % urlquote(filepath, safe='')
    attrs = {
        'ref': repo.get_release_tag(pkg_name),
    }
    try:
        with _gitlab_api_query(server, path, resource, attrs) as res:
            return res.read().decode('utf-8')
    except HTTPError as e:
        if e.code == 404 and filepath != 'package.xml':
            logger.debug('- File not found (%s), trying "%s"' % (e, e.filename))
            return None
        logger.debug('- failed (%s), trying "%s"' % (e, e.filename))
        if _is_definitive_gitlab_failure(e, repo.url):
            raise PermanentManifestError(
                'Failed to fetch "%s" from "%s": %s' % (filepath, repo.url, e)
            ) from e
        raise
    except (URLError, TimeoutError, socket.timeout) as e:
        logger.debug('- failed (%s), trying "%s"' % (e, getattr(e, 'filename', None) or repo.url))
        raise


def gitlab_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    assert repo.version
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('gitlab.com') and server != ROSDISTRO_GITLAB_SERVER:
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise SkipManifestProvider('can not handle non gitlab urls')

    # Resolve the version ref to a sha since we need to make multiple queries
    attrs = {
        'per_page': 1,
        'ref_name': repo.version,
    }
    try:
        commit_obj = next(_gitlab_paged_api_query(server, path, 'repository/commits', attrs))
    except StopIteration:
        msg = 'No commits found for ref "%s" in repository "%s"' % (repo.version, repo.url)
        if is_unauthenticated_http_url(repo.url):
            raise PermanentManifestError(msg)
        raise RuntimeError(msg)
    except HTTPError as e:
        if _is_definitive_gitlab_failure(e, repo.url):
            raise PermanentManifestError(
                'Unable to fetch commits for ref "%s" from "%s": %s' % (repo.version, repo.url, e)
            ) from e
        raise
    sha = commit_obj['id']
    commit_date = commit_obj.get('committed_date') or commit_obj.get('created_at')

    # Look for package.xml files and assets in the tree
    attrs = {
        'recursive': 'true',
        'ref': sha,
    }
    blob_paths = [
        obj['path']
        for obj in _gitlab_paged_api_query(server, path, 'repository/tree', attrs)
        if obj.get('type', 'blob') not in ('tree', 'commit')
    ]
    package_xml_paths, package_assets, root_files = partition_repo_tree(blob_paths)

    cache = SourceRepositoryCache.from_ref(sha)

    if (metadata is None or 'last_commit_time' in metadata) and commit_date is not None:
        cache.set_metadata('last_commit_time', commit_date)

    # Fetch project info for configured metadata (stars, description, tags, etc.)
    if metadata is None or len(metadata) > 0:
        if metadata is None or any(k in metadata for k in GITLAB_REPO_METADATA_FIELDS):
            try:
                with _gitlab_api_query(server, path, '', {}) as res:
                    project_json = json.loads(res.read().decode('utf-8'))
                    for meta_key, api_field in GITLAB_REPO_METADATA_FIELDS.items():
                        if meta_key == 'last_commit_time' and cache.get_metadata('last_commit_time') is not None:
                            continue
                        if (metadata is None or meta_key in metadata) and api_field in project_json:
                            cache.set_metadata(meta_key, project_json[api_field])
            except (URLError, TimeoutError, socket.timeout) as e:
                logger.debug('- failed to load project info from %s: %s' % (path, e))

        for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
            if metadata is None or meta_key in metadata:
                for candidate in candidates:
                    if candidate not in root_files:
                        continue
                    resource = 'repository/files/' + urlquote(candidate, safe='') + '/raw'
                    try:
                        with _gitlab_api_query(server, path, resource, {'ref': sha}) as res:
                            doc_contents = res.read().decode('utf-8')
                    except (URLError, TimeoutError, socket.timeout) as e:
                        logger.debug('- failed to load %s from %s: %s' % (candidate, path, e))
                        continue
                    cache.set_metadata(meta_key, sanitize_and_truncate_docs(doc_contents))
                    break

    for package_xml_path in package_xml_paths:
        package_xml_filename = 'package.xml'
        resource_path = urlquote(
            package_xml_path + '/' + package_xml_filename if package_xml_path else package_xml_filename, safe='')
        resource = 'repository/files/' + resource_path + '/raw'
        with _gitlab_api_query(server, path, resource, {'ref': sha}) as res:
            package_xml = res.read().decode('utf-8')
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
            resource_path = urlquote(
                package_xml_path + '/' + filepath if package_xml_path else filepath, safe='')
            resource = 'repository/files/' + resource_path + '/raw'
            try:
                with _gitlab_api_query(server, path, resource, {'ref': sha}) as res:
                    contents = res.read().decode('utf-8')
            except (URLError, TimeoutError, socket.timeout) as e:
                logger.debug('- failed to load %s from %s: %s' % (filepath, path, e))
                continue
            cache.add(name, package_xml_path, contents, filepath)

    return cache
