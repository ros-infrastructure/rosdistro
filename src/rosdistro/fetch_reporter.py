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

"""Standardized, homogeneous console reporting for resource fetches."""

from contextlib import contextmanager
import sys
import threading
import time

from . import logger


_active_reporter_local = threading.local()


def get_provider_name(provider):
    """Return a canonical short fetch-type name for a manifest provider callable."""
    if provider is None:
        return 'unknown'
    if isinstance(provider, str):
        return provider
    name = getattr(provider, '__name__', None)
    if not name:
        name = type(provider).__name__
    for suffix in ('_source_manifest_provider', '_manifest_provider'):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name


def format_duration(seconds):
    """Format a duration in seconds into a concise human-readable string."""
    if seconds is None:
        return '0.000s'
    if seconds < 60.0:
        return '%.3fs' % seconds
    if seconds < 3600.0:
        mins, rem = divmod(seconds, 60.0)
        return '%dm %05.2fs' % (int(mins), rem)
    hours, rem = divmod(seconds, 3600.0)
    mins, secs = divmod(rem, 60.0)
    return '%dh %02dm %04.1fs' % (int(hours), int(mins), secs)


def format_fetch_event(
    resource_type,
    target,
    fetch_type,
    status='ok',
    duration_s=None,
    dist_name=None,
    detail=None,
):
    """Format a standardized console/log line for any fetched resource."""
    parts = []
    if status:
        parts.append(str(status))
    if duration_s is not None:
        parts.append('%.3fs' % duration_s)
    if detail:
        parts.append(str(detail))
    meta = ' (%s)' % ', '.join(parts) if parts else ''
    prefix = '[%s] ' % dist_name if dist_name else ''
    return '  %s[%s] %s via=%s%s' % (prefix, resource_type, target, fetch_type, meta)


def format_phase_summary(phase_label, counts_by_fetch, durations_by_fetch, total_s, dist_name=None):
    """Format a concise per-phase breakdown line by fetch type."""
    total_items = sum(counts_by_fetch.values())
    parts = []
    for ft, cnt in sorted(
        counts_by_fetch.items(),
        key=lambda kv: (-durations_by_fetch.get(kv[0], 0.0), -kv[1], kv[0]),
    ):
        ft_dur = durations_by_fetch.get(ft, 0.0)
        parts.append('%s=%d [%.2fs]' % (ft, cnt, ft_dur))
    breakdown = ', '.join(parts) if parts else 'none'
    prefix = ' [%s]' % dist_name if dist_name else ''
    return '- %s summary%s: %d item(s) in %s (%s)' % (
        phase_label, prefix, total_items, format_duration(total_s), breakdown,
    )


class _FetchContext(object):
    __slots__ = ('target', 'status', 'detail')

    def __init__(self, target, status='ok', detail=None):
        self.target = target
        self.status = status
        self.detail = detail


class FetchReporter(object):
    """Standardized console/debug reporter active during cache operations."""

    def __init__(self, debug=False, console_stream=None):
        self.debug = bool(debug)
        self._console_stream = console_stream
        self._local = threading.local()

    def _dist_stack(self):
        stack = getattr(self._local, 'dist_stack', None)
        if stack is None:
            stack = []
            self._local.dist_stack = stack
        return stack

    def _fetch_depth(self):
        return getattr(self._local, 'fetch_depth', 0)

    def current_dist_name(self):
        stack = self._dist_stack()
        return stack[-1] if stack else None

    def has_enclosing_fetch(self):
        return self._fetch_depth() > 0

    @contextmanager
    def distro_scope(self, dist_name):
        stack = self._dist_stack()
        stack.append(dist_name)
        try:
            yield
        finally:
            stack.pop()

    @contextmanager
    def fetch_scope(self):
        self._local.fetch_depth = self._fetch_depth() + 1
        try:
            yield
        finally:
            self._local.fetch_depth = max(0, self._fetch_depth() - 1)

    def _write_console(self, line):
        stream = self._console_stream if self._console_stream is not None else sys.stdout
        stream.write(line + '\n')
        stream.flush()

    def should_print_fetch(self, resource_type, fetch_type, status='ok', level='top'):
        """Decide whether a standardized fetch event should print directly to console."""
        if level == 'sub':
            return self.debug
        if self.debug:
            return True
        if resource_type in ('cache:load', 'cache:neighbor_load', 'cache:write'):
            return True
        if resource_type == 'cache:validate_source_ref':
            return fetch_type == 'git_ls_remote' or status in ('dropped', 'failed')
        return fetch_type not in ('cache', 'skipped')

    def report_fetch(
        self,
        resource_type,
        target,
        fetch_type,
        status='ok',
        duration_s=None,
        dist_name=None,
        detail=None,
        level='top',
    ):
        """Emit a standardized fetch report line via the framework."""
        effective_dist = dist_name or self.current_dist_name()
        msg = format_fetch_event(
            resource_type=resource_type,
            target=target,
            fetch_type=fetch_type,
            status=status,
            duration_s=duration_s,
            dist_name=effective_dist,
            detail=detail,
        )
        if self.should_print_fetch(resource_type, fetch_type, status=status, level=level):
            self._write_console(msg)
        else:
            logger.debug(msg.strip())

    @contextmanager
    def sub_fetch(self, resource_type, target, fetch_type, detail=None):
        """Time a sub-resource fetch inside a provider and emit standardized debug output."""
        ctx = _FetchContext(target=target, status='ok', detail=detail)
        start = time.perf_counter()
        try:
            yield ctx
        except BaseException:
            if ctx.status == 'ok':
                ctx.status = 'failed'
            raise
        finally:
            dur = time.perf_counter() - start
            self.report_fetch(
                resource_type=resource_type,
                target=ctx.target,
                fetch_type=fetch_type,
                status=ctx.status,
                duration_s=dur,
                detail=ctx.detail,
                level='sub',
            )

    def instant_sub_fetch(self, resource_type, target, fetch_type, status='ok', detail=None):
        """Emit a standardized debug report for an instantaneous sub-resource fetch."""
        self.report_fetch(
            resource_type=resource_type,
            target=target,
            fetch_type=fetch_type,
            status=status,
            duration_s=0.0,
            detail=detail,
            level='sub',
        )


def get_active_reporter():
    """Return the active FetchReporter for the current thread, or None."""
    return getattr(_active_reporter_local, 'reporter', None)


@contextmanager
def use_reporter(reporter):
    """Install *reporter* as the active FetchReporter for the current thread."""
    prev = getattr(_active_reporter_local, 'reporter', None)
    _active_reporter_local.reporter = reporter
    try:
        yield reporter
    finally:
        _active_reporter_local.reporter = prev


def report_fetch(
    resource_type,
    target,
    fetch_type,
    status='ok',
    duration_s=None,
    dist_name=None,
    detail=None,
    level='top',
):
    """Report a fetch event via the active reporter, or via ``logger.debug`` when standalone."""
    reporter = get_active_reporter()
    if reporter is not None:
        reporter.report_fetch(
            resource_type=resource_type,
            target=target,
            fetch_type=fetch_type,
            status=status,
            duration_s=duration_s,
            dist_name=dist_name,
            detail=detail,
            level=level,
        )
    else:
        logger.debug(
            format_fetch_event(
                resource_type=resource_type,
                target=target,
                fetch_type=fetch_type,
                status=status,
                duration_s=duration_s,
                dist_name=dist_name,
                detail=detail,
            ).strip()
        )


@contextmanager
def report_sub_fetch(resource_type, target, fetch_type, detail=None):
    """Time a sub-resource fetch inside a provider and emit standardized debug output."""
    reporter = get_active_reporter()
    if reporter is not None:
        with reporter.sub_fetch(resource_type, target, fetch_type, detail=detail) as ctx:
            yield ctx
        return
    ctx = _FetchContext(target=target, status='ok', detail=detail)
    start = time.perf_counter()
    try:
        yield ctx
    except BaseException:
        if ctx.status == 'ok':
            ctx.status = 'failed'
        raise
    finally:
        dur = time.perf_counter() - start
        logger.debug(
            format_fetch_event(
                resource_type=resource_type,
                target=ctx.target,
                fetch_type=fetch_type,
                status=ctx.status,
                duration_s=dur,
                detail=ctx.detail,
            ).strip()
        )


def report_instant_sub_fetch(resource_type, target, fetch_type, status='ok', detail=None):
    """Emit a standardized debug report for an instantaneous sub-resource fetch."""
    reporter = get_active_reporter()
    if reporter is not None:
        reporter.instant_sub_fetch(
            resource_type, target, fetch_type, status=status, detail=detail
        )
        return
    logger.debug(
        format_fetch_event(
            resource_type=resource_type,
            target=target,
            fetch_type=fetch_type,
            status=status,
            duration_s=0.0,
            detail=detail,
        ).strip()
    )
