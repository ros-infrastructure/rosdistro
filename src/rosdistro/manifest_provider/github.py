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
    _run_parallel,
    open_url_with_backoff,
    parse_retry_after,
)
from rosdistro.repository_specification import RepositorySpecification
from rosdistro.vcs import ref_is_hash

GITHUB_USER = os.getenv('GITHUB_USER', None)
GITHUB_PASSWORD = os.getenv('GITHUB_PASSWORD', None)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', None)

ROSDISTRO_RETRY_MAX = int(os.getenv('ROSDISTRO_RETRY_MAX', '30'))

GITHUB_GRAPHQL_URL = 'https://api.github.com/graphql'
DEFAULT_GITHUB_GRAPHQL_BATCH_SIZE = 50
DEFAULT_GITHUB_GRAPHQL_MIN_REMAINING = 50
_GRAPHQL_TARGET_SELECTION = (
    '{ target { ... on Commit { oid } ... on Tag { target { ... on Commit { oid } } } } }'
)

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


def _get_github_token():
    env_token = os.getenv('GITHUB_TOKEN')
    if env_token is not None:
        return env_token or None
    return GITHUB_TOKEN or None


def _has_control_chars(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _parse_github_owner_repo(url):
    if not url or not isinstance(url, str):
        return None
    match = RepositorySpecification.VCS_REGEX.match(url.strip())
    if not match:
        return None
    server, path = match.groups()
    if server.rsplit('@', 1)[-1].lower() != 'github.com':
        return None
    parts = path.strip('/').split('/')
    if len(parts) != 2:
        return None
    owner, name = parts
    if not owner or not name or _has_control_chars(owner) or _has_control_chars(name):
        return None
    return owner, name


def _extract_graphql_commit_oid(ref_obj):
    if not isinstance(ref_obj, dict):
        return None
    target = ref_obj.get('target')
    if not isinstance(target, dict):
        return None
    oid = target.get('oid')
    if not oid:
        inner = target.get('target')
        if isinstance(inner, dict):
            oid = inner.get('oid')
    if isinstance(oid, str) and ref_is_hash(oid):
        return oid
    return None


def _build_graphql_refs_query(batch):
    lines = ['query {']
    for repo_alias, owner, name, version_entries in batch:
        lines.append('  %s: repository(owner: %s, name: %s) {' % (
            repo_alias, json.dumps(owner), json.dumps(name),
        ))
        for idx, ver_str, _ in version_entries:
            if ver_str.startswith('refs/'):
                lines.append('    q%d: ref(qualifiedName: %s) %s' % (
                    idx, json.dumps(ver_str), _GRAPHQL_TARGET_SELECTION,
                ))
            else:
                lines.append('    h%d: ref(qualifiedName: %s) %s' % (
                    idx, json.dumps('refs/heads/' + ver_str), _GRAPHQL_TARGET_SELECTION,
                ))
                lines.append('    t%d: ref(qualifiedName: %s) %s' % (
                    idx, json.dumps('refs/tags/' + ver_str), _GRAPHQL_TARGET_SELECTION,
                ))
        lines.append('  }')
    lines.append('  rateLimit { remaining resetAt cost }')
    lines.append('}')
    return '\n'.join(lines)


def _resolve_graphql_batch_size(batch_size):
    if batch_size is not None:
        return max(1, int(batch_size))
    raw = os.getenv('ROSDISTRO_GITHUB_GRAPHQL_BATCH_SIZE')
    if raw is not None and raw.strip() != '':
        try:
            return max(1, int(raw))
        except ValueError:
            logger.debug('Invalid ROSDISTRO_GITHUB_GRAPHQL_BATCH_SIZE=%r; using default %d.' % (
                raw, DEFAULT_GITHUB_GRAPHQL_BATCH_SIZE,
            ))
    return DEFAULT_GITHUB_GRAPHQL_BATCH_SIZE


def resolve_github_remote_refs(url_version_pairs, batch_size=None, max_workers=1):
    """Batch-resolve ``github.com`` ``(url, version)`` pairs to peeled commit SHAs via GraphQL.

    Returns a ``dict[(url, version), commit_sha]`` containing only cleanly resolved refs;
    ambiguous, missing, errored, or unauthenticated entries are omitted so callers fall back
    to ``git ls-remote`` without recording false negative-cache entries.
    """
    if os.getenv('ROSDISTRO_GITHUB_GRAPHQL', '').strip().lower() in ('0', 'false', 'no', 'off'):
        return {}
    token = _get_github_token()
    if not token:
        return {}

    grouped = {}
    for pair in url_version_pairs:
        if not pair or len(pair) != 2:
            continue
        url, version = pair
        if version is None:
            continue
        ver_str = str(version)
        if not ver_str or ref_is_hash(ver_str) or _has_control_chars(ver_str):
            continue
        parsed = _parse_github_owner_repo(url)
        if parsed is None:
            continue
        owner, name = parsed
        repo_key = (owner.lower(), name.lower())
        entry = grouped.get(repo_key)
        if entry is None:
            entry = (owner, name, {})
            grouped[repo_key] = entry
        pairs_for_ver = entry[2].setdefault(ver_str, [])
        if pair not in pairs_for_ver:
            pairs_for_ver.append(pair)

    if not grouped:
        return {}

    effective_batch_size = _resolve_graphql_batch_size(batch_size)
    repo_items = list(grouped.values())
    batches = []
    for start in range(0, len(repo_items), effective_batch_size):
        chunk = repo_items[start:start + effective_batch_size]
        batch = []
        for r_idx, (owner, name, versions_map) in enumerate(chunk):
            version_entries = [
                (v_idx, ver_str, orig_pairs)
                for v_idx, (ver_str, orig_pairs) in enumerate(versions_map.items())
            ]
            batch.append(('r%d' % r_idx, owner, name, version_entries))
        batches.append(batch)

    stop_graphql = threading.Event()
    total_batches = len(batches)
    indexed_batches = list(enumerate(batches))

    def _run_batch(item):
        batch_idx, batch = item
        if stop_graphql.is_set():
            return {}
        query = _build_graphql_refs_query(batch)
        req = Request(
            GITHUB_GRAPHQL_URL,
            data=json.dumps({'query': query}).encode('utf-8'),
            method='POST',
        )
        req.add_header('Authorization', 'Bearer %s' % token)
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        batch_detail = 'batch=%d/%d, repos=%d' % (batch_idx + 1, total_batches, len(batch))

        try:
            resp, slot_cm = open_url_with_backoff(
                req,
                urlopen_fn=urlopen,
                fetch_type='github_graphql',
                timeout=get_http_timeout(),
                retry_max=ROSDISTRO_RETRY_MAX,
                is_retryable_fn=_is_github_retryable,
                sleep_fn=time.sleep,
                warn_fn=_warn_rate_limit_once,
                rate_limit_hint='Set GITHUB_TOKEN to increase the rate limit.',
                provider_label='GitHub GraphQL API',
                protocol='http_graphql',
                operation='http:post',
                detail=batch_detail,
            )
            try:
                with resp:
                    payload = json.loads(resp.read().decode('utf-8'))
            finally:
                if slot_cm is not None:
                    slot_cm.__exit__(None, None, None)
        except HostRateLimitError as e:
            stop_graphql.set()
            logger.debug('GitHub GraphQL batch stopped due to rate limit: %s' % e)
            return {}
        except HTTPError as e:
            if e.code in (401, 403, 429):
                stop_graphql.set()
            logger.debug('GitHub GraphQL batch failed with HTTP %s: %s' % (e.code, e))
            return {}
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError, TypeError) as e:
            logger.debug('GitHub GraphQL batch failed: %s' % e)
            return {}

        if not isinstance(payload, dict):
            return {}

        errored_repos = set()
        errored_fields = set()
        raw_errors = payload.get('errors')
        if isinstance(raw_errors, list):
            for err in raw_errors:
                if not isinstance(err, dict):
                    continue
                if str(err.get('type') or '').upper() == 'RATE_LIMITED':
                    stop_graphql.set()
                err_path = err.get('path')
                if isinstance(err_path, list) and err_path:
                    if len(err_path) == 1:
                        errored_repos.add(err_path[0])
                    else:
                        errored_fields.add((err_path[0], err_path[1]))

        data = payload.get('data')
        if not isinstance(data, dict):
            return {}

        rate_limit = data.get('rateLimit')
        if isinstance(rate_limit, dict):
            remaining = rate_limit.get('remaining')
            if (
                isinstance(remaining, (int, float))
                and not isinstance(remaining, bool)
                and remaining < DEFAULT_GITHUB_GRAPHQL_MIN_REMAINING
            ):
                stop_graphql.set()
                logger.debug(
                    'GitHub GraphQL rate limit low (remaining=%s); stopping remaining batches.'
                    % remaining
                )

        batch_resolved = {}
        for repo_alias, _owner, _name, version_entries in batch:
            if repo_alias in errored_repos:
                continue
            repo_data = data.get(repo_alias)
            if not isinstance(repo_data, dict):
                continue
            for idx, ver_str, orig_pairs in version_entries:
                if ver_str.startswith('refs/'):
                    q_field = 'q%d' % idx
                    if (repo_alias, q_field) in errored_fields:
                        continue
                    oid = _extract_graphql_commit_oid(repo_data.get(q_field))
                else:
                    h_field = 'h%d' % idx
                    t_field = 't%d' % idx
                    if (repo_alias, h_field) in errored_fields or (repo_alias, t_field) in errored_fields:
                        continue
                    head_oid = _extract_graphql_commit_oid(repo_data.get(h_field))
                    tag_oid = _extract_graphql_commit_oid(repo_data.get(t_field))
                    if head_oid and tag_oid and head_oid != tag_oid:
                        continue
                    oid = tag_oid or head_oid
                if oid:
                    for orig_pair in orig_pairs:
                        batch_resolved[orig_pair] = oid
        return batch_resolved

    effective_workers = min(max(1, int(max_workers or 1)), 4)
    resolved = {}
    for batch_result in _run_parallel(indexed_batches, _run_batch, effective_workers, 'rosdistro-gql'):
        if batch_result:
            resolved.update(batch_result)
    return resolved
