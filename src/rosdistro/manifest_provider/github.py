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
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen

from rosdistro import logger
from rosdistro.cache_profiler import sanitize_url_for_trace, trace_operation
from rosdistro.fetch_reporter import report_sub_fetch
from rosdistro.manifest_provider import (
    PermanentManifestError,
    SkipManifestProvider,
    get_http_header,
    get_http_timeout,
    is_unauthenticated_http_url,
    read_http_error_body,
)
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    ManifestSession,
    fetch_release_file,
    fetch_source_cache,
)
from rosdistro.manifest_provider.tree import DEFAULT_SOURCE_FILEPATHS

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


def _build_github_api_request(url):
    req = Request(url)
    if GITHUB_TOKEN:
        req.add_header('Authorization', f'Bearer {GITHUB_TOKEN}')
    if GITHUB_USER and GITHUB_PASSWORD:
        credential_pair = '%s:%s' % (GITHUB_USER, GITHUB_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        req.add_header('Authorization', authheader)
    return req


class _GitHubSession(ManifestSession):

    def __init__(self, query):
        self._query = query
        self._server, self._path = query.repo.get_url_parts()
        self.ref = None
        if query.ref_is_tag and not query.repo.has_remote_tag(query.ref):
            raise PermanentManifestError('specified tag "%s" is not a git tag' % query.ref)

    def list_files(self):
        quoted_version = urlquote(str(self._query.ref), safe='/')
        tree_url = 'https://api.github.com/repos/%s/git/trees/%s?recursive=1' % (
            self._path, quoted_version
        )
        req = _build_github_api_request(tree_url)
        try:
            tree_json = json.loads(_get_url_contents(req))
        except HTTPError as e:
            if _is_definitive_github_failure(e, self._query.repo.url):
                raise PermanentManifestError(
                    'Unable to fetch JSON tree from %s: %s' % (tree_url, e)
                ) from e
            raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e)) from e
        except (URLError, TimeoutError, socket.timeout) as e:
            raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e)) from e

        if tree_json['truncated']:
            raise RuntimeError('JSON tree is truncated, must perform full clone.')

        self.ref = tree_json['sha']
        return [
            obj['path']
            for obj in tree_json['tree']
            if obj.get('type', 'blob') not in ('tree', 'commit')
        ]

    def read_file(self, path):
        ref_segment = (
            self.ref if self.ref is not None else urlquote(str(self._query.ref), safe='/')
        )
        url = 'https://raw.githubusercontent.com/%s/%s/%s' % (self._path, ref_segment, path)
        try:
            return _get_url_contents(url)
        except HTTPError as e:
            if e.code == 404:
                return None
            raise
        except (URLError, TimeoutError, socket.timeout) as e:
            raise RuntimeError('Failed to fetch "%s": %s' % (url, e)) from e

    def fetch_metadata(self, keys):
        result = {}
        if keys is None or any(k in GITHUB_REPO_METADATA_FIELDS for k in keys):
            repo_url = 'https://api.github.com/repos/%s' % self._path
            repo_req = _build_github_api_request(repo_url)
            with report_sub_fetch(
                '%s:metadata:repo_info' % self._query.label,
                self._query.repo.name,
                'github',
            ) as ctx:
                try:
                    repo_json = json.loads(_get_url_contents(repo_req))
                    for meta_key, api_field in GITHUB_REPO_METADATA_FIELDS.items():
                        if (keys is None or meta_key in keys) and api_field in repo_json:
                            result[meta_key] = repo_json[api_field]
                except (URLError, TimeoutError, socket.timeout):
                    ctx.status = 'failed'
        return result


class GitHubProvider(ManifestProvider):

    name = 'github'

    def accepts(self, repo):
        if getattr(repo, 'type', 'git') != 'git':
            raise SkipManifestProvider('Cannot handle non-git repository.')
        server, _ = repo.get_url_parts()
        if not server.endswith('github.com'):
            raise SkipManifestProvider('can not handle non github urls')

    def open(self, query):
        return _GitHubSession(query)

    def is_permanent_error(self, err, repo):
        return _is_definitive_github_failure(err, repo.url)

    def missing_is_permanent(self, repo):
        return is_unauthenticated_http_url(repo.url)


GITHUB = GitHubProvider()


def github_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    return fetch_release_file(GITHUB, repo, pkg_name, filepath)


github_manifest_provider._unified_provider = GITHUB


def github_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    return fetch_source_cache(GITHUB, repo, filepaths=filepaths, metadata=metadata)


github_source_manifest_provider._unified_provider = GITHUB
