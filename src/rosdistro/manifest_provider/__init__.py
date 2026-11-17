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
from urllib.parse import urlparse


DEFAULT_HTTP_TIMEOUT = 30.0


def get_http_timeout():
    """Return the configured HTTP socket timeout in seconds, or None if disabled."""
    raw = os.getenv('ROSDISTRO_HTTP_TIMEOUT')
    if raw is None or raw == '':
        return DEFAULT_HTTP_TIMEOUT
    timeout = float(raw)
    return timeout if timeout > 0 else None


def is_unauthenticated_http_url(url):
    """Return True if *url* is an http(s) URL without embedded credentials."""
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password


def get_http_header(headers, name):
    """Case-insensitive lookup of *name* in an HTTP headers mapping."""
    if not headers:
        return None
    val = headers.get(name)
    if val is not None:
        return val
    lower_name = name.lower()
    for k, v in headers.items():
        if k.lower() == lower_name:
            return v
    return None


def read_http_error_body(err):
    """Read and cache the decoded body of an ``HTTPError`` without losing it on re-reads."""
    cached = getattr(err, '_rosdistro_body', None)
    if cached is not None:
        return cached
    body = ''
    try:
        if getattr(err, 'fp', None) is not None:
            raw_body = err.read()
            if isinstance(raw_body, bytes):
                body = raw_body.decode('utf-8', errors='replace')
            elif isinstance(raw_body, str):
                body = raw_body
    except Exception:
        body = ''
    try:
        err._rosdistro_body = body
    except Exception:
        pass
    return body


class SkipManifestProvider(RuntimeError):
    """Raised when a manifest provider cannot handle a given repository."""


class PermanentManifestError(RuntimeError):
    """Raised when an authoritative provider confirms the target repo/ref is
    permanently unavailable (e.g. HTTP 404 or non-rate-limit 403), so falling
    back to slower providers such as ``git clone`` should be skipped."""


# This function remains here as a compatibility shim because there is code
# in older rosinstall_generator versions which uses it in this location.
def get_release_tag(repo, pkg_name):
    return repo.get_release_tag(pkg_name)
