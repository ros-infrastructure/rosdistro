# Software License Agreement (BSD License)
#
# Copyright (c) 2016, Clearpath Robotics
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
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen

from rosdistro.cache_profiler import sanitize_url_for_trace, trace_operation
from rosdistro.manifest_provider import (
    PermanentManifestError,
    SkipManifestProvider,
    get_http_timeout,
    is_unauthenticated_http_url,
)
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    ManifestSession,
    fetch_release_file,
)

# This Bitbucket provider can optionally send basic auth to fetch from
# private repositories if you supply the environment variables
# BITBUCKET_USER and BITBUCKET_PASSWORD. This can be the credentials
# for an actual Bitbucket user, or it can be a team name with API key.
#
# Generate an API key for your team here:
# https://bitbucket.org/account/user/<team-name>/api-key/
BITBUCKET_USER = os.getenv('BITBUCKET_USER', None)
BITBUCKET_PASSWORD = os.getenv('BITBUCKET_PASSWORD', None)


def _auth_request(url):
    req = Request(url)
    if BITBUCKET_USER and BITBUCKET_PASSWORD:
        credential_pair = '%s:%s' % (BITBUCKET_USER, BITBUCKET_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        req.add_header('Authorization', authheader)
    return req


class _BitbucketSession(ManifestSession):

    def __init__(self, query):
        self._query = query
        self._server, self._path = query.repo.get_url_parts()
        if query.ref_is_tag and not query.repo.has_remote_tag(query.ref):
            raise PermanentManifestError('specified tag "%s" is not a git tag' % query.ref)

    def read_file(self, path):
        quoted_ref = urlquote(str(self._query.ref), safe='/')
        url = 'https://bitbucket.org/%s/raw/%s/%s' % (self._path, quoted_ref, path)
        try:
            req = _auth_request(url)
            with trace_operation('http:get', fetch_type='bitbucket', target=sanitize_url_for_trace(url)):
                with urlopen(req, timeout=get_http_timeout()) as resp:
                    return resp.read().decode('utf-8')
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (410, 451):
                raise
            raise RuntimeError('Failed to fetch "%s": %s' % (url, e)) from e
        except (URLError, TimeoutError, socket.timeout) as e:
            raise RuntimeError('Failed to fetch "%s": %s' % (url, e)) from e


class BitbucketProvider(ManifestProvider):

    name = 'bitbucket'

    def accepts(self, repo):
        if getattr(repo, 'type', 'git') != 'git':
            raise SkipManifestProvider('Cannot handle non-git repository.')
        server, _ = repo.get_url_parts()
        if not server.endswith('bitbucket.org'):
            raise SkipManifestProvider('Cannot handle non bitbucket url.')

    def open(self, query):
        return _BitbucketSession(query)

    def is_permanent_error(self, err, repo):
        return getattr(err, 'code', None) in (404, 410, 451) and is_unauthenticated_http_url(repo.url)

    def missing_is_permanent(self, repo):
        return is_unauthenticated_http_url(repo.url)


BITBUCKET = BitbucketProvider()


def bitbucket_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    return fetch_release_file(BITBUCKET, repo, pkg_name, filepath)


bitbucket_manifest_provider._unified_provider = BITBUCKET
