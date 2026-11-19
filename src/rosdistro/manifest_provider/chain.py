# Software License Agreement (BSD License)
#
# Copyright (c) 2026, Open Source Robotics Foundation, Inc.
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

import inspect
from typing import Any, NamedTuple, Optional, Tuple

from rosdistro import logger
from rosdistro.cache_profiler import trace_span
from rosdistro.fetch_reporter import get_provider_name
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    fetch_release_file,
    fetch_source_cache,
)

REJECT = object()


class ProviderAttempt(NamedTuple):
    mp_name: str
    error: Exception
    skipped: bool


class ChainOutcome(NamedTuple):
    succeeded: bool
    result: Any
    provider_name: Optional[str]
    attempts: Tuple[ProviderAttempt, ...]
    permanent: bool


def select_relevant_attempts(attempts):
    """Return non-skipped ``(mp_name, error)`` pairs, or all attempts if every one was skipped."""
    relevant = [(a.mp_name, a.error) for a in attempts if not a.skipped]
    if not relevant:
        relevant = [(a.mp_name, a.error) for a in attempts]
    return relevant


def format_attempts(attempts, multiline=True):
    """Format relevant provider failures for error logs or negative-cache summaries."""
    relevant = select_relevant_attempts(attempts)
    if not relevant:
        return ''
    if multiline:
        return '\n'.join('  - %s(): %s' % (name, err) for name, err in relevant)
    return '; '.join('%s(): %s' % (name, err) for name, err in relevant)


def _can_bind(sig, *args, **kwargs):
    try:
        sig.bind(*args, **kwargs)
        return True
    except TypeError:
        return False


def call_release_provider(mp, dist_name, repo, pkg_name, filepath='package.xml'):
    """Invoke a release manifest provider callable or ``ManifestProvider`` instance."""
    if isinstance(mp, ManifestProvider):
        return fetch_release_file(mp, repo, pkg_name, filepath)
    try:
        sig = inspect.signature(mp)
    except (TypeError, ValueError):
        return mp(dist_name, repo, pkg_name, filepath)
    if _can_bind(sig, dist_name, repo, pkg_name, filepath):
        return mp(dist_name, repo, pkg_name, filepath)
    if filepath == 'package.xml':
        return mp(dist_name, repo, pkg_name)
    return None


def call_source_provider(mp, repo, filepaths=None, metadata=None):
    """Invoke a source manifest provider callable or ``ManifestProvider`` instance."""
    if isinstance(mp, ManifestProvider):
        return fetch_source_cache(mp, repo, filepaths=filepaths, metadata=metadata)
    try:
        sig = inspect.signature(mp)
    except (TypeError, ValueError):
        return mp(repo, filepaths=filepaths, metadata=metadata)
    if _can_bind(sig, repo, filepaths=filepaths, metadata=metadata):
        return mp(repo, filepaths=filepaths, metadata=metadata)
    if _can_bind(sig, repo, filepaths=filepaths):
        return mp(repo, filepaths=filepaths)
    return mp(repo)


def provider_display_name(mp):
    """Return the provider name used in error logs for *mp*."""
    if isinstance(mp, ManifestProvider) and isinstance(mp.name, str) and mp.name:
        return mp.name
    return getattr(mp, '__name__', str(mp))


def run_provider_chain(providers, invoke, *, target, dist_name=None, accept=None):
    """Run *invoke(mp)* across *providers* with tracing, skip/permanent handling, and fallback.

    When *accept* is provided, it is called with the raw provider return value outside
    the ``provider:*`` span. Returning ``REJECT`` continues to the next provider without
    recording an error; raising an ``Exception`` records a non-skipped failure and
    continues to the next provider.
    """
    attempts = []
    permanent = False

    for mp in providers or []:
        provider_name = get_provider_name(mp)
        mp_name = provider_display_name(mp)
        with trace_span(
            'provider:%s' % provider_name,
            kind='provider',
            fetch_type=provider_name,
            target=target,
            dist_name=dist_name,
        ) as pspan:
            try:
                raw = invoke(mp)
            except PermanentManifestError as e:
                pspan.status = 'failed'
                logger.debug(
                    'Permanent failure in "%s()": %s; skipping fallback providers' % (mp_name, e)
                )
                attempts.append(ProviderAttempt(mp_name, e, False))
                permanent = True
                break
            except SkipManifestProvider as e:
                pspan.status = 'skip'
                logger.debug('Skipped "%s()": %s' % (mp_name, e))
                attempts.append(ProviderAttempt(mp_name, e, True))
                continue
            except Exception as e:
                pspan.status = 'failed'
                logger.debug('Skipped "%s()": %s' % (mp_name, e))
                attempts.append(ProviderAttempt(mp_name, e, False))
                continue

            if raw is None:
                pspan.status = 'not_found'

        if accept is not None:
            try:
                value = accept(raw)
            except Exception as e:
                logger.debug('Skipped "%s()": %s' % (mp_name, e))
                attempts.append(ProviderAttempt(mp_name, e, False))
                continue
            if value is REJECT:
                continue
        else:
            value = raw

        return ChainOutcome(True, value, provider_name, tuple(attempts), False)

    return ChainOutcome(False, None, None, tuple(attempts), permanent)
