# Software License Agreement (BSD License)
#
# Copyright (c) 2020, Canonical Ltd.
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
#  * Neither the name of Canonical Ltd. nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
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
import io
import os
import tarfile
from urllib.request import Request, urlopen

from rosdistro.cache_profiler import sanitize_url_for_trace, trace_operation
from rosdistro.manifest_provider import (
    PermanentManifestError,
    SkipManifestProvider,
    get_http_timeout,
)
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    ManifestSession,
    fetch_release_file,
    fetch_source_cache,
)
from rosdistro.manifest_provider.tree import DEFAULT_SOURCE_FILEPATHS

_TAR_USER = os.getenv('TAR_USER', None)
_TAR_PASSWORD = os.getenv('TAR_PASSWORD', None)


def _normalize_tar_path(name):
    path = name.replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    return path.lstrip('/')


def _build_tar_request(url):
    request = Request(url)
    if _TAR_USER and _TAR_PASSWORD:
        credential_pair = '%s:%s' % (_TAR_USER, _TAR_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        request.add_header('Authorization', authheader)
    elif _TAR_PASSWORD:
        request.add_header('Private-Token', _TAR_PASSWORD)
    return request


class _TarSession(ManifestSession):

    def __init__(self, query):
        self._query = query
        self._tar = None
        self._members = None
        self.ref = None

    def _ensure_open(self):
        if self._tar is not None:
            return
        repo = self._query.repo
        request = _build_tar_request(repo.url)
        with trace_operation('http:get', fetch_type='tar', target=sanitize_url_for_trace(repo.url)):
            with urlopen(request, timeout=get_http_timeout()) as response:
                tar_bytes = response.read()
        self._tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        members = {}
        for member in self._tar.getmembers():
            if not member.isfile():
                continue
            norm = _normalize_tar_path(member.name)
            if norm:
                members[norm] = member
        self._members = members

    def list_files(self):
        self._ensure_open()
        return list(self._members.keys())

    def read_file(self, path):
        self._ensure_open()
        norm = _normalize_tar_path(path)
        member = self._members.get(norm)
        if member is None:
            return None
        with trace_operation('tar:extract', fetch_type='tar', target=norm):
            extracted = self._tar.extractfile(member)
            if extracted is None:
                return None
            return extracted.read().decode('utf-8', errors='replace')

    def close(self):
        if self._tar is not None:
            self._tar.close()
            self._tar = None
            self._members = None


class TarProvider(ManifestProvider):

    name = 'tar'

    def accepts(self, repo):
        if getattr(repo, 'type', 'git') != 'tar':
            raise SkipManifestProvider('Cannot handle non-tar repository.')

    def open(self, query):
        return _TarSession(query)


TAR = TarProvider()


def tar_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    return fetch_release_file(TAR, repo, pkg_name, filepath)


tar_manifest_provider._unified_provider = TAR


def tar_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    TAR.accepts(repo)
    try:
        return fetch_source_cache(TAR, repo, filepaths=filepaths, metadata=metadata)
    except (SkipManifestProvider, PermanentManifestError):
        raise
    except Exception as e:
        raise RuntimeError('Unable to fetch source package.xml files: %s' % e) from e


tar_source_manifest_provider._unified_provider = TAR
