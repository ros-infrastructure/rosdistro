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
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen

from rosdistro import logger
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
from rosdistro.rate_limiter import (
    HostRateLimitError,
    open_url_with_backoff,
    parse_retry_after,
)
from rosdistro.vcs import ref_is_hash

GITHUB_USER = os.getenv('GITHUB_USER', None)
GITHUB_PASSWORD = os.getenv('GITHUB_PASSWORD', None)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', None)

ROSDISTRO_RETRY_MAX = int(os.getenv('ROSDISTRO_RETRY_MAX', '30'))

_rate_limit_warn_lock = threading.Lock()
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
    if code == 422:
        body = read_http_error_body(err).lower()
        if 'no commit found' in body:
            return True
    return _is_permanent_403(err)


def _parse_retry_after(value):
    return parse_retry_after(value)


def _warn_rate_limit_once(msg):
    global _rate_limit_warned
    with _rate_limit_warn_lock:
        if not _rate_limit_warned:
            _rate_limit_warned = True
            logger.warning(msg)
            return
    logger.debug(msg)


def _is_github_retryable(err, retry_after):
    if _is_permanent_403(err):
        return False
    return err.code in (403, 429) or (err.code == 503 and retry_after is not None)


def _get_url_contents(url):
    resp, slot_cm = open_url_with_backoff(
        url,
        urlopen_fn=urlopen,
        fetch_type='github',
        timeout=get_http_timeout(),
        retry_max=ROSDISTRO_RETRY_MAX,
        is_retryable_fn=_is_github_retryable,
        sleep_fn=time.sleep,
        warn_fn=_warn_rate_limit_once,
        rate_limit_hint='Set GITHUB_TOKEN to increase the rate limit.',
        provider_label='GitHub API',
    )
    try:
        with resp:
            return resp.read().decode('utf-8')
    finally:
        if slot_cm is not None:
            slot_cm.__exit__(None, None, None)


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

    def _resolve_commit_sha(self):
        ref_str = str(self._query.ref)
        if ref_is_hash(ref_str):
            return ref_str
        quoted_version = urlquote(ref_str, safe='/')
        commit_url = 'https://api.github.com/repos/%s/commits/%s' % (self._path, quoted_version)
        req = _build_github_api_request(commit_url)
        req.add_header('Accept', 'application/vnd.github.sha')
        try:
            body = _get_url_contents(req).strip()
        except HTTPError as e:
            if _is_definitive_github_failure(e, self._query.repo.url):
                raise PermanentManifestError(
                    'Unable to resolve commit SHA from %s: %s' % (commit_url, e)
                ) from e
            raise RuntimeError('Unable to resolve commit SHA from %s: %s' % (commit_url, e)) from e
        except (URLError, TimeoutError, socket.timeout) as e:
            raise RuntimeError('Unable to resolve commit SHA from %s: %s' % (commit_url, e)) from e

        if body.startswith('{'):
            commit_json = json.loads(body)
            body = str(commit_json['sha']).strip()
        if not body:
            raise RuntimeError('Empty commit SHA returned from %s' % commit_url)
        return body

    def list_files(self):
        commit_sha = self._resolve_commit_sha()
        quoted_sha = urlquote(commit_sha, safe='/')
        tree_url = 'https://api.github.com/repos/%s/git/trees/%s?recursive=1' % (
            self._path, quoted_sha
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

        self.ref = commit_sha
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
                except (URLError, TimeoutError, socket.timeout, HostRateLimitError):
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
