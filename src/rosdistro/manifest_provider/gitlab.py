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

from contextlib import contextmanager
import json
import os
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlencode
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
    get_retry_max,
    open_url_with_backoff,
)

GITLAB_PRIVATE_TOKEN = os.getenv('GITLAB_PRIVATE_TOKEN', None)
ROSDISTRO_GITLAB_SERVER = os.getenv('ROSDISTRO_GITLAB_SERVER', None)
ROSDISTRO_RETRY_MAX = None
DEFAULT_GITLAB_PAGINATION_TIMEOUT = 120.0

_rate_limit_warn_lock = threading.Lock()
_rate_limit_warned = False

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


def _warn_rate_limit_once(msg):
    global _rate_limit_warned
    with _rate_limit_warn_lock:
        if not _rate_limit_warned:
            _rate_limit_warned = True
            logger.warning(msg)
            return
    logger.debug(msg)


def _is_gitlab_retryable(err, retry_after):
    code = getattr(err, 'code', None)
    if code == 429:
        return True
    if code == 503 and retry_after is not None:
        return True
    if code == 403:
        if retry_after is not None:
            return True
        if get_http_header(getattr(err, 'headers', None), 'RateLimit-Remaining') == '0':
            return True
        lower_body = read_http_error_body(err).lower()
        return 'rate limit' in lower_body or 'too many' in lower_body or 'abuse' in lower_body
    return False


@contextmanager
def _gitlab_urlopen(url, deadline=None):
    req = Request(url)
    if GITLAB_PRIVATE_TOKEN:
        req.add_header('Private-Token', GITLAB_PRIVATE_TOKEN)
    logger.debug('Performing GitLab API query "%s"' % (url,))
    resp, slot_cm = open_url_with_backoff(
        req,
        urlopen_fn=urlopen,
        fetch_type='gitlab',
        timeout=get_http_timeout(),
        retry_max=get_retry_max(ROSDISTRO_RETRY_MAX),
        is_retryable_fn=_is_gitlab_retryable,
        sleep_fn=time.sleep,
        remaining_header='RateLimit-Remaining',
        reset_header='RateLimit-Reset',
        warn_fn=_warn_rate_limit_once,
        rate_limit_hint='Set GITLAB_PRIVATE_TOKEN to increase the rate limit.',
        provider_label='GitLab API',
        deadline=deadline,
    )
    try:
        with resp as res:
            yield res
    finally:
        if slot_cm is not None:
            slot_cm.__exit__(None, None, None)


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
        with _gitlab_urlopen(url, deadline=deadline) as res:
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


class _GitLabSession(ManifestSession):

    def __init__(self, query):
        self._query = query
        self._server, self._path = query.repo.get_url_parts()
        self.ref = None
        self._commit_date = None

    def list_files(self):
        repo = self._query.repo
        assert repo.version
        attrs = {
            'per_page': 1,
            'ref_name': self._query.ref,
        }
        try:
            commit_obj = next(_gitlab_paged_api_query(self._server, self._path, 'repository/commits', attrs))
        except StopIteration:
            msg = 'No commits found for ref "%s" in repository "%s"' % (self._query.ref, repo.url)
            if is_unauthenticated_http_url(repo.url):
                raise PermanentManifestError(msg)
            raise RuntimeError(msg)
        except HTTPError as e:
            if _is_definitive_gitlab_failure(e, repo.url):
                raise PermanentManifestError(
                    'Unable to fetch commits for ref "%s" from "%s": %s' % (self._query.ref, repo.url, e)
                ) from e
            raise

        self.ref = commit_obj['id']
        self._commit_date = commit_obj.get('committed_date') or commit_obj.get('created_at')

        tree_attrs = {
            'recursive': 'true',
            'ref': self.ref,
        }
        return [
            obj['path']
            for obj in _gitlab_paged_api_query(self._server, self._path, 'repository/tree', tree_attrs)
            if obj.get('type', 'blob') not in ('tree', 'commit')
        ]

    def read_file(self, path):
        ref = self.ref if self.ref is not None else self._query.ref
        resource = 'repository/files/%s/raw' % urlquote(path, safe='')
        try:
            with _gitlab_api_query(self._server, self._path, resource, {'ref': ref}) as res:
                return res.read().decode('utf-8')
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def fetch_metadata(self, keys):
        result = {}
        if (keys is None or 'last_commit_time' in keys) and self._commit_date is not None:
            result['last_commit_time'] = self._commit_date

        need_project_info = (
            keys is None
            or any(
                k in GITLAB_REPO_METADATA_FIELDS and k not in result
                for k in keys
            )
        )
        if need_project_info:
            with report_sub_fetch(
                '%s:metadata:repo_info' % self._query.label,
                self._query.repo.name,
                'gitlab',
            ) as ctx:
                try:
                    with _gitlab_api_query(self._server, self._path, '', {}) as res:
                        project_json = json.loads(res.read().decode('utf-8'))
                        for meta_key, api_field in GITLAB_REPO_METADATA_FIELDS.items():
                            if meta_key in result:
                                continue
                            if (keys is None or meta_key in keys) and api_field in project_json:
                                result[meta_key] = project_json[api_field]
                except (URLError, TimeoutError, socket.timeout, HostRateLimitError):
                    ctx.status = 'failed'
        return result


class GitLabProvider(ManifestProvider):

    name = 'gitlab'

    def accepts(self, repo):
        if getattr(repo, 'type', 'git') != 'git':
            raise SkipManifestProvider('Cannot handle non-git repository.')
        server, _ = repo.get_url_parts()
        if not server.endswith('gitlab.com') and server != ROSDISTRO_GITLAB_SERVER:
            raise SkipManifestProvider('can not handle non gitlab urls')

    def open(self, query):
        return _GitLabSession(query)

    def is_permanent_error(self, err, repo):
        return _is_definitive_gitlab_failure(err, repo.url)

    def missing_is_permanent(self, repo):
        return is_unauthenticated_http_url(repo.url)


GITLAB = GitLabProvider()


def gitlab_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    return fetch_release_file(GITLAB, repo, pkg_name, filepath)


gitlab_manifest_provider._unified_provider = GITLAB


def gitlab_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    return fetch_source_cache(GITLAB, repo, filepaths=filepaths, metadata=metadata)


gitlab_source_manifest_provider._unified_provider = GITLAB
