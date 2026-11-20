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

from contextlib import contextmanager
import os
import tempfile
import time

from rosdistro import logger
from rosdistro.cache_profiler import trace_operation
from rosdistro.common import rmtree
from rosdistro.fetch_reporter import report_sub_fetch
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    ManifestSession,
    fetch_release_file,
    fetch_source_cache,
)
from rosdistro.manifest_provider.tree import (
    DEFAULT_SOURCE_FILEPATHS,
    list_local_repo_files,
)
from rosdistro.rate_limiter import HostRateLimitError, extract_host, run_git_with_backoff
from rosdistro.vcs import Git, normalize_git_url, ref_is_hash


class _GitSession(ManifestSession):

    def __init__(self, query):
        self._query = query
        self._clone_ctx = None
        self._entered = False
        self._repo_path = None
        self._git_client = None
        self.ref = None

    def _ensure_cloned(self, resolve_head=False):
        if not self._entered:
            self._clone_ctx = _temp_git_clone(self._query.repo.url, self._query.ref)
            self._repo_path = self._clone_ctx.__enter__()
            self._entered = True
        if resolve_head and self._git_client is None:
            self._git_client = Git(self._repo_path)
            self.ref = self._git_client.command('rev-parse', 'HEAD')['output']

    def list_files(self):
        self._ensure_cloned(resolve_head=True)
        with trace_operation('fs:list_repo_files', fetch_type='git', target=self._query.repo.name):
            return list_local_repo_files(self._repo_path)

    def read_file(self, path):
        self._ensure_cloned(resolve_head=False)
        parts = [p for p in path.replace('\\', '/').split('/') if p]
        filename = os.path.join(self._repo_path, *parts)
        if not os.path.isfile(filename):
            return None
        with open(filename, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()

    def fetch_metadata(self, keys):
        if keys is None or 'last_commit_time' in keys:
            self._ensure_cloned(resolve_head=True)
            with report_sub_fetch(
                '%s:metadata:last_commit_time' % self._query.label,
                self._query.repo.name,
                'git',
            ) as ctx:
                commit_time_res = self._git_client.command('log', '-1', '--format=%cI', 'HEAD')
                if commit_time_res.get('returncode', 0) == 0 and commit_time_res.get('output'):
                    return {'last_commit_time': commit_time_res['output'].strip()}
                ctx.status = 'not_found'
        return {}

    def close(self):
        if self._entered and self._clone_ctx is not None:
            try:
                self._clone_ctx.__exit__(None, None, None)
            finally:
                self._entered = False
                self._clone_ctx = None
                self._repo_path = None
                self._git_client = None


class GitProvider(ManifestProvider):

    name = 'git'

    def accepts(self, repo):
        if getattr(repo, 'type', 'git') != 'git':
            raise SkipManifestProvider('Cannot handle non-git repository.')

    def open(self, query):
        return _GitSession(query)


GIT = GitProvider()


def git_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    GIT.accepts(repo)
    try:
        return fetch_release_file(GIT, repo, pkg_name, filepath)
    except (SkipManifestProvider, PermanentManifestError, HostRateLimitError):
        raise
    except Exception as e:
        raise RuntimeError('Unable to fetch %s: %s' % (filepath, e)) from e


git_manifest_provider._unified_provider = GIT


def git_source_manifest_provider(repo, filepaths=DEFAULT_SOURCE_FILEPATHS, metadata=None):
    GIT.accepts(repo)
    try:
        return fetch_source_cache(GIT, repo, filepaths=filepaths, metadata=metadata)
    except (SkipManifestProvider, PermanentManifestError, HostRateLimitError):
        raise
    except Exception as e:
        raise RuntimeError('Unable to fetch source files: %s' % e) from e


git_source_manifest_provider._unified_provider = GIT


@contextmanager
def _temp_git_clone(url, ref):
    base = tempfile.mkdtemp('rosdistro')
    git = Git(cwd=base)
    clone_url = normalize_git_url(url)

    def _reset_dir():
        nonlocal base, git
        if base and os.path.exists(base):
            rmtree(base)
        base = None
        base = tempfile.mkdtemp('rosdistro')
        git = Git(cwd=base)

    try:
        cloned = False
        if git.version_gte('1.8.0') and not ref_is_hash(ref):
            # Directly clone the required ref with least amount of additional history.
            # Available since git 1.8.0, but only works for tags and branches, not hashes:
            # https://git.kernel.org/cgit/git/git.git/tree/Documentation/git-clone.txt?h=v1.8.0#n158
            clone_args = ('clone', clone_url, '.', '--depth', '1')
            if ref:
                clone_args += ('--branch', ref)
            result = run_git_with_backoff(
                lambda: git.command(*clone_args), url, sleep_fn=time.sleep, on_retry=_reset_dir,
            )
            cloned = result['returncode'] == 0
            if not cloned:
                if result.get('rate_limited'):
                    raise HostRateLimitError(
                        extract_host(url) or url,
                        0.0,
                        reason='Could not clone repository "%s": %s' % (url, result.get('error') or 'rate limited'),
                    )
                logger.debug(
                    'Shallow clone of "%s" at reference "%s" failed, '
                    'falling back to full clone.' % (url, ref))
                _reset_dir()

        if not cloned:
            # Old git doesn't support cloning a tag/branch directly, or shallow clone failed,
            # so full clone and checkout.
            result = run_git_with_backoff(
                lambda: git.command('clone', clone_url, '.'), url, sleep_fn=time.sleep, on_retry=_reset_dir,
            )
            if result['returncode'] != 0:
                error_msg = 'Could not clone repository "%s"' % url
                if result.get('error'):
                    error_msg += ': %s' % result['error']
                if result.get('rate_limited'):
                    raise HostRateLimitError(extract_host(url) or url, 0.0, reason=error_msg)
                raise RuntimeError(error_msg)

            if ref:
                result = git.command('checkout', ref)
                if result['returncode'] != 0:
                    error_msg = 'Could not checkout ref "%s" of repository "%s"' % (ref, url)
                    if result.get('error'):
                        error_msg += ': %s' % result['error']
                    raise RuntimeError(error_msg)

        yield base
    finally:
        if base and os.path.exists(base):
            rmtree(base)
