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
import datetime
import email.utils
import json
import os
import socket
import time

from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote as urlquote

from catkin_pkg.package import parse_package_string

from rosdistro import logger
from rosdistro.cache_profiler import sanitize_url_for_trace, trace_operation
from rosdistro.fetch_reporter import report_instant_sub_fetch, report_sub_fetch
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

GITHUB_USER = os.getenv('GITHUB_USER', None)
GITHUB_PASSWORD = os.getenv('GITHUB_PASSWORD', None)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', None)

ROSDISTRO_RETRY_MAX = int(os.getenv('ROSDISTRO_RETRY_MAX', '30'))

_rate_limit_warned = False

GITHUB_REPO_METADATA_FIELDS = {
    'stars': 'stargazers_count',
    'description': 'description',
    'tags': 'topics',
    'last_commit_time': 'pushed_at',
}


def _get_header(headers, name):
    return get_http_header(headers, name)


def _is_permanent_403(err):
    """Return True if *err* is an authoritative non-rate-limit GitHub 403."""
    if getattr(err, 'code', None) != 403:
        return False
    headers = getattr(err, 'headers', None)
    if _parse_retry_after(_get_header(headers, 'Retry-After')) is not None:
        return False
    remaining = _get_header(headers, 'X-RateLimit-Remaining')
    if remaining is None or remaining == '0':
        return False
    body = read_http_error_body(err)
    lower_body = body.lower()
    if not body or 'rate limit' in lower_body or 'abuse' in lower_body:
        return False
    return True


def _is_definitive_github_failure(err, repo_url):
    """Return True when a GitHub HTTPError means git clone will also fail."""
    if not is_unauthenticated_http_url(repo_url):
        return False
    code = getattr(err, 'code', None)
    if code in (404, 410, 451):
        return True
    return _is_permanent_403(err)


def _parse_retry_after(value):
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _compute_retry_delay(err, remaining_budget, backoff, url_str):
    global _rate_limit_warned
    headers = getattr(err, 'headers', None)

    retry_after = _parse_retry_after(_get_header(headers, 'Retry-After'))
    if retry_after is not None:
        if retry_after > remaining_budget:
            logger.debug(
                'Fetch of %s failed with %d (Retry-After=%.0fs exceeds remaining retry budget %.0fs); failing fast.'
                % (url_str, err.code, retry_after, remaining_budget)
            )
            return None
        return retry_after if retry_after > 0 else min(backoff, remaining_budget)

    remaining = _get_header(headers, 'X-RateLimit-Remaining')
    reset = _get_header(headers, 'X-RateLimit-Reset')
    if remaining == '0':
        if reset is not None:
            try:
                wait_until_reset = float(reset) - time.time()
            except (TypeError, ValueError):
                wait_until_reset = None
            if wait_until_reset is not None:
                if wait_until_reset > remaining_budget:
                    msg = (
                        'GitHub API rate limit exceeded for %s (resets in %.0f seconds, '
                        'exceeds retry budget %.0f seconds); failing fast so fallback providers can run. '
                        'Set GITHUB_TOKEN to increase the rate limit.'
                        % (url_str, wait_until_reset, remaining_budget)
                    )
                    if not _rate_limit_warned:
                        _rate_limit_warned = True
                        logger.warning(msg)
                    else:
                        logger.debug(msg)
                    return None
                if wait_until_reset > 0:
                    return min(wait_until_reset + 0.5, remaining_budget)
        return min(backoff, remaining_budget)

    if _is_permanent_403(err):
        logger.debug(
            'Fetch of %s failed with non-rate-limit 403 (X-RateLimit-Remaining=%s); failing fast.'
            % (url_str, remaining)
        )
        return None

    return min(backoff, remaining_budget)


def _get_url_contents(url):
    timeout = get_http_timeout()
    remaining_budget = max(0.0, float(ROSDISTRO_RETRY_MAX))
    backoff = 1.0
    url_str = sanitize_url_for_trace(getattr(url, 'full_url', url))
    while True:
        try:
            with trace_operation('http:get', fetch_type='github', target=url_str):
                with urlopen(url, timeout=timeout) as resp:
                    return resp.read().decode('utf-8')
        except HTTPError as e:
            if e.code not in (403, 429):
                raise
            if remaining_budget <= 0:
                raise
            delay = _compute_retry_delay(e, remaining_budget, backoff, url_str)
            if delay is None or delay <= 0:
                raise
            logger.debug(
                'Fetch of %s failed with %d, assuming rate limit, retrying after period %.1f seconds.'
                % (url_str, e.code, delay)
            )
            with trace_operation('http:rate_limit_sleep', fetch_type='github', target=url_str):
                time.sleep(delay)
            remaining_budget -= delay
            backoff *= 1.5


def github_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        raise SkipManifestProvider('can not handle non github urls')

    release_tag = repo.get_release_tag(pkg_name)

    if not repo.has_remote_tag(release_tag):
        raise PermanentManifestError('specified tag "%s" is not a git tag' % release_tag)

    url = 'https://raw.githubusercontent.com/%s/%s/%s' % (path, urlquote(release_tag, safe='/'), filepath)
    try:
        return _get_url_contents(url)
    except HTTPError as e:
        if e.code == 404 and filepath != 'package.xml':
            return None
        if _is_definitive_github_failure(e, repo.url):
            raise PermanentManifestError('Failed to fetch "%s": %s' % (url, e)) from e
        raise e
    except (URLError, TimeoutError, socket.timeout) as e:
        raise RuntimeError('Failed to fetch "%s": %s' % (url, e))


def github_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    if getattr(repo, 'type', 'git') != 'git':
        raise SkipManifestProvider('Cannot handle non-git repository.')
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        raise SkipManifestProvider('can not handle non github urls')

    quoted_version = urlquote(str(repo.version), safe='/')
    tree_url = 'https://api.github.com/repos/%s/git/trees/%s?recursive=1' % (path, quoted_version)
    req = Request(tree_url)
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    if GITHUB_USER and GITHUB_PASSWORD:
        credential_pair = '%s:%s' % (GITHUB_USER, GITHUB_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        req.add_header('Authorization', authheader)
    with report_sub_fetch('source:repo_tree', repo.name, 'github') as ctx:
        try:
            tree_json = json.loads(_get_url_contents(req))
        except HTTPError as e:
            ctx.status = 'failed'
            if _is_definitive_github_failure(e, repo.url):
                raise PermanentManifestError(
                    'Unable to fetch JSON tree from %s: %s' % (tree_url, e)
                ) from e
            raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e))
        except (URLError, TimeoutError, socket.timeout) as e:
            ctx.status = 'failed'
            raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e))

        if tree_json['truncated']:
            ctx.status = 'failed'
            raise RuntimeError('JSON tree is truncated, must perform full clone.')

    blob_paths = [
        obj['path']
        for obj in tree_json['tree']
        if obj.get('type', 'blob') not in ('tree', 'commit')
    ]
    blob_path_set = {p.replace('\\', '/').strip('/') for p in blob_paths if p and p.strip('/')}
    with report_sub_fetch('source:partition_tree', repo.name, 'github'):
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
            with report_sub_fetch('source:metadata:repo_info', repo.name, 'github') as ctx:
                try:
                    repo_json = json.loads(_get_url_contents(repo_req))
                    for meta_key, api_field in GITHUB_REPO_METADATA_FIELDS.items():
                        if (metadata is None or meta_key in metadata) and api_field in repo_json:
                            cache.set_metadata(meta_key, repo_json[api_field])
                except (URLError, TimeoutError, socket.timeout):
                    ctx.status = 'failed'

        for meta_key, candidates in ROOT_DOC_CANDIDATES.items():
            if metadata is None or meta_key in metadata:
                matching_candidates = [c for c in candidates if c in root_files]
                if not matching_candidates:
                    report_instant_sub_fetch(
                        'source:metadata:%s' % meta_key, repo.name, 'github',
                        status='not_found', detail='not_in_tree',
                    )
                    continue
                with report_sub_fetch('source:metadata:%s' % meta_key, repo.name, 'github') as ctx:
                    found_doc = False
                    for candidate in matching_candidates:
                        doc_url = 'https://raw.githubusercontent.com/%s/%s/%s' % (path, cache.ref(), candidate)
                        try:
                            doc_contents = _get_url_contents(doc_url)
                        except (URLError, TimeoutError, socket.timeout):
                            continue
                        cache.set_metadata(meta_key, sanitize_and_truncate_docs(doc_contents))
                        found_doc = True
                        break
                    if not found_doc:
                        ctx.status = 'failed'

    for package_xml_path in package_xml_paths:
        package_xml_filename = 'package.xml'
        url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
            (path, cache.ref(), package_xml_path + '/' + package_xml_filename if package_xml_path else package_xml_filename)
        pkg_target = '%s/%s' % (repo.name, package_xml_path) if package_xml_path else repo.name
        with report_sub_fetch('source:package.xml', pkg_target, 'github') as ctx:
            try:
                package_xml = _get_url_contents(url)
            except (URLError, TimeoutError, socket.timeout) as e:
                ctx.status = 'failed'
                raise RuntimeError('Unable to fetch %s from %s: %s' % (package_xml_filename, url, e))
            name = parse_package_string(package_xml).name
            ctx.target = '%s/%s' % (repo.name, name)
        cache.add(name, package_xml_path, package_xml, package_xml_filename)
        pkg_label = '%s/%s' % (repo.name, name)
        for filepath in filepaths:
            if filepath == package_xml_filename:
                continue
            if filepath in PACKAGE_ASSET_LIST_RESOURCES:
                assets = package_assets.get(package_xml_path, {}).get(filepath, [])
                cache.add(name, package_xml_path, assets, filepath)
                report_instant_sub_fetch(
                    'source:%s' % filepath, pkg_label, 'github',
                    status='ok', detail='count=%d' % len(assets),
                )
                continue
            rel_target = (package_xml_path + '/' + filepath if package_xml_path else filepath).replace('\\', '/').strip('/')
            if rel_target not in blob_path_set:
                report_instant_sub_fetch(
                    'source:%s' % filepath, pkg_label, 'github',
                    status='not_found', detail='not_in_tree',
                )
                continue
            url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
                (path, cache.ref(), package_xml_path + '/' + filepath if package_xml_path else filepath)
            with report_sub_fetch('source:%s' % filepath, pkg_label, 'github') as ctx:
                try:
                    contents = _get_url_contents(url)
                except HTTPError as e:
                    if e.code == 404:
                        ctx.status = 'not_found'
                        continue
                    ctx.status = 'failed'
                    raise e
                except (URLError, TimeoutError, socket.timeout) as e:
                    ctx.status = 'failed'
                    raise RuntimeError('Unable to fetch %s from %s: %s' % (filepath, url, e))
            cache.add(name, package_xml_path, contents, filepath)

    return cache
