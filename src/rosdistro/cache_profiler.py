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
#  * Redistributions in binary form must reproduce the above copyright
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

"""OpenTelemetry-backed hierarchical profiling and tracing for cache builds."""

from contextlib import contextmanager
import heapq
import json
import os
import random
import threading
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Status, StatusCode
    HAS_OTEL_API = True
except ImportError:
    otel_context = otel_trace = Status = StatusCode = None
    HAS_OTEL_API = False

try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    HAS_OTEL_SDK = True
except ImportError:
    Resource = TracerProvider = None
    SpanProcessor = object
    HAS_OTEL_SDK = False

from .fetch_reporter import (
    FetchReporter,
    format_duration,
    format_phase_summary,
    use_reporter,
)

_ATTR_KIND = 'rosdistro.kind'
_ATTR_RESOURCE = 'rosdistro.resource_type'
_ATTR_FETCH_TYPE = 'rosdistro.fetch_type'
_ATTR_TARGET = 'rosdistro.target'
_ATTR_DISTRO = 'rosdistro.distro'
_ATTR_STATUS = 'rosdistro.status'
_ATTR_DETAIL = 'rosdistro.detail'
_ATTR_DEFERRED = 'rosdistro.deferred_label'

_MUTABLE_ATTRS = {
    'fetch_type': _ATTR_FETCH_TYPE,
    'status': _ATTR_STATUS,
    'detail': _ATTR_DETAIL,
    'target': _ATTR_TARGET,
}

_active_profiler_local = threading.local()


def sanitize_url_for_trace(url):
    """Strip any embedded credentials from *url* before recording in traces."""
    if not isinstance(url, str) or '://' not in url:
        return str(url) if url is not None else ''
    try:
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ''
        netloc = '%s:%d' % (host, parts.port) if parts.port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


def _build_attributes(kind, resource_type, fetch_type, target, dist_name, status, detail, deferred_label=False):
    raw = (
        (_ATTR_KIND, kind),
        (_ATTR_RESOURCE, resource_type),
        (_ATTR_FETCH_TYPE, fetch_type),
        (_ATTR_TARGET, target),
        (_ATTR_DISTRO, dist_name),
        (_ATTR_STATUS, status),
        (_ATTR_DETAIL, detail),
        (_ATTR_DEFERRED, True if deferred_label else None),
    )
    return {k: v for k, v in raw if v is not None}


def _frame_label(span):
    """Build a folded-stack / trace label from an OpenTelemetry span."""
    attrs = span.attributes or {}
    kind = attrs.get(_ATTR_KIND, 'operation')
    if kind in ('root', 'distro', 'phase'):
        return span.name
    if kind == 'provider':
        return '%s [%s]' % (span.name, attrs.get(_ATTR_STATUS) or 'ok')
    fetch_type = attrs.get(_ATTR_FETCH_TYPE)
    base = attrs.get(_ATTR_RESOURCE) or span.name if kind in ('fetch', 'sub_fetch') else span.name
    return '%s [via=%s]' % (base, fetch_type) if fetch_type else base


def _write_text(path, content):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


class FetchStats(object):
    """Aggregated timing and status statistics for a profiling bucket."""

    MAX_SAMPLES = 10000
    __slots__ = ('count', 'total_s', 'self_s', 'min_s', 'max_s', 'statuses', '_samples', '_rng')

    def __init__(self):
        self.count = 0
        self.total_s = 0.0
        self.self_s = 0.0
        self.min_s = None
        self.max_s = 0.0
        self.statuses = {}
        self._samples = []
        self._rng = random.Random(0)

    def add(self, duration_s, self_s=None, status=None):
        self.count += 1
        self.total_s += duration_s
        if self_s is not None:
            self.self_s += self_s
        self.min_s = duration_s if self.min_s is None else min(self.min_s, duration_s)
        self.max_s = max(self.max_s, duration_s)
        if status:
            self.statuses[status] = self.statuses.get(status, 0) + 1
        if len(self._samples) < self.MAX_SAMPLES:
            self._samples.append(duration_s)
        else:
            j = self._rng.randint(0, self.count - 1)
            if j < self.MAX_SAMPLES:
                self._samples[j] = duration_s

    def to_dict(self, total_run_s=None):
        ordered = sorted(self._samples)
        pct = (100.0 * self.total_s / total_run_s) if (total_run_s and total_run_s > 0) else 0.0
        return {
            'count': self.count,
            'total_s': round(self.total_s, 6),
            'self_s': round(self.self_s, 6),
            'pct_of_total': round(pct, 2),
            'avg_s': round(self.total_s / self.count if self.count else 0.0, 6),
            'min_s': round(self.min_s if self.min_s is not None else 0.0, 6),
            'p50_s': round(_percentile(ordered, 50.0), 6),
            'p95_s': round(_percentile(ordered, 95.0), 6),
            'max_s': round(self.max_s, 6),
            'statuses': dict(self.statuses),
        }


class _SpanHandle(object):
    """Handle updating OpenTelemetry span attributes while tracking wall duration."""

    __slots__ = ('_otel_span', 'fetch_type', 'status', 'detail', 'target', 'dist_name', 'duration_s')

    def __init__(self, otel_span=None, fetch_type=None, status='ok', detail=None, target=None, dist_name=None):
        object.__setattr__(self, '_otel_span', otel_span)
        object.__setattr__(self, 'fetch_type', fetch_type)
        object.__setattr__(self, 'status', status)
        object.__setattr__(self, 'detail', detail)
        object.__setattr__(self, 'target', target)
        object.__setattr__(self, 'dist_name', dist_name)
        object.__setattr__(self, 'duration_s', 0.0)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        attr_key = _MUTABLE_ATTRS.get(name)
        otel_span = self._otel_span
        if attr_key and otel_span is not None and value is not None:
            otel_span.set_attribute(attr_key, value)
            if name == 'status' and HAS_OTEL_API and value in ('failed', 'timeout'):
                otel_span.set_status(Status(StatusCode.ERROR, str(value)))


class RosdistroSpanProcessor(SpanProcessor):
    """OpenTelemetry SpanProcessor aggregating rosdistro timings, folded stacks, and Chrome traces."""

    def __init__(self, record_trace_events=False, slowest_limit=25):
        self.record_trace_events = bool(record_trace_events)
        self.slowest_limit = max(0, int(slowest_limit))
        self._lock = threading.Lock()
        self._origin_ns = None
        self._last_end_ns = None

        self._active = {}
        self._children_dur_ns = {}
        self._span_tid = {}
        self._pending_children = {}

        self._folded_stacks = {}
        self._trace_events = []
        self._slowest_heap = []
        self._slowest_seq = 0

        self._by_phase = {}
        self._by_distro = {}
        self._by_resource_type = {}
        self._by_fetch_type = {}
        self._by_resource_fetch = {}
        self._by_sub_resource_fetch = {}
        self._by_operation = {}
        self._distros_seen = []

    def on_start(self, span, parent_context=None):
        span_id = span.context.span_id
        with self._lock:
            self._active[span_id] = span
            self._children_dur_ns[span_id] = 0
            self._span_tid[span_id] = threading.get_ident()
            if self._origin_ns is None and span.start_time is not None:
                self._origin_ns = span.start_time

    def on_end(self, span):
        span_id = span.context.span_id
        parent_id = span.parent.span_id if span.parent is not None else None
        start_ns = span.start_time or 0
        end_ns = span.end_time or start_ns
        dur_ns = max(0, end_ns - start_ns)

        with self._lock:
            self._active.pop(span_id, None)
            children_ns = self._children_dur_ns.pop(span_id, 0)
            tid = self._span_tid.pop(span_id, threading.get_ident())
            if parent_id in self._children_dur_ns:
                self._children_dur_ns[parent_id] += dur_ns
            self_ns = max(0, dur_ns - children_ns)
            if self._origin_ns is None:
                self._origin_ns = start_ns
            self._last_end_ns = max(self._last_end_ns or 0, end_ns)

            ancestors = []
            has_deferred = False
            cur = parent_id
            while cur in self._active:
                anc = self._active[cur]
                ancestors.append(anc)
                if anc.attributes and anc.attributes.get(_ATTR_DEFERRED):
                    has_deferred = True
                cur = anc.parent.span_id if anc.parent is not None else None

            if parent_id is not None and has_deferred:
                self._pending_children.setdefault(parent_id, []).append((span, self_ns, tid))
            else:
                prefix = tuple(_frame_label(a) for a in reversed(ancestors))
                self._flush_subtree_locked(span, self_ns, tid, prefix)

    def shutdown(self):
        with self._lock:
            self._active.clear()
            self._children_dur_ns.clear()
            self._span_tid.clear()
            self._pending_children.clear()

    def force_flush(self, timeout_millis=30000):
        return True

    @staticmethod
    def _bucket(mapping, key):
        return mapping.setdefault(key, FetchStats())

    def _record_stats_locked(self, span, attrs, frame, dur_s, self_s):
        kind = attrs.get(_ATTR_KIND, 'operation')
        status = attrs.get(_ATTR_STATUS)
        if kind == 'distro':
            dname = attrs.get(_ATTR_DISTRO) or span.name
            if dname not in self._distros_seen:
                self._distros_seen.append(dname)
            self._bucket(self._by_distro, dname).add(dur_s, self_s=self_s, status=status)
        elif kind == 'phase':
            self._bucket(self._by_phase, span.name).add(dur_s, self_s=self_s, status=status)
        elif kind == 'fetch':
            res = attrs.get(_ATTR_RESOURCE) or span.name
            ft = attrs.get(_ATTR_FETCH_TYPE) or 'unknown'
            self._bucket(self._by_resource_type, res).add(dur_s, self_s=self_s, status=status)
            self._bucket(self._by_fetch_type, ft).add(dur_s, self_s=self_s, status=status)
            self._bucket(self._by_resource_fetch, (res, ft)).add(dur_s, self_s=self_s, status=status)
            self._record_slowest_locked(attrs, res, ft, status, dur_s)
        elif kind == 'sub_fetch':
            res = attrs.get(_ATTR_RESOURCE) or span.name
            ft = attrs.get(_ATTR_FETCH_TYPE) or 'unknown'
            self._bucket(self._by_sub_resource_fetch, (res, ft)).add(dur_s, self_s=self_s, status=status)
        elif kind in ('operation', 'provider'):
            op_key = frame if kind == 'provider' else span.name
            self._bucket(self._by_operation, op_key).add(dur_s, self_s=self_s, status=status)

    def _record_slowest_locked(self, attrs, res, ft, status, dur_s):
        if self.slowest_limit <= 0:
            return
        if ft in (
            'cache', 'skipped', 'ttl_skip', 'pinned_hash', 'known_remote_ref', 'github_graphql'
        ) and dur_s < 0.005:
            return
        self._slowest_seq += 1
        entry = (dur_s, self._slowest_seq, {
            'duration_s': round(dur_s, 6),
            'dist_name': attrs.get(_ATTR_DISTRO) or '',
            'resource_type': res,
            'fetch_type': ft,
            'status': status or 'ok',
            'target': attrs.get(_ATTR_TARGET) or '',
            'detail': attrs.get(_ATTR_DETAIL) or '',
        })
        if len(self._slowest_heap) < self.slowest_limit:
            heapq.heappush(self._slowest_heap, entry)
        elif entry[0] > self._slowest_heap[0][0]:
            heapq.heapreplace(self._slowest_heap, entry)

    def _flush_subtree_locked(self, span, self_ns, tid, prefix_frames):
        frame = _frame_label(span)
        cur_stack = prefix_frames + (frame,)
        if self_ns > 0:
            self._folded_stacks[cur_stack] = self._folded_stacks.get(cur_stack, 0) + self_ns

        attrs = span.attributes or {}
        start_ns = span.start_time or 0
        dur_ns = max(0, (span.end_time or start_ns) - start_ns)
        self._record_stats_locked(span, attrs, frame, dur_ns / 1e9, self_ns / 1e9)

        if self.record_trace_events:
            origin = self._origin_ns if self._origin_ns is not None else start_ns
            self._trace_events.append((
                frame, attrs.get(_ATTR_KIND, 'operation'), max(0, (start_ns - origin) // 1000),
                dur_ns // 1000, tid, attrs.get(_ATTR_RESOURCE), attrs.get(_ATTR_FETCH_TYPE),
                attrs.get(_ATTR_TARGET), attrs.get(_ATTR_DISTRO), attrs.get(_ATTR_STATUS),
                attrs.get(_ATTR_DETAIL),
            ))

        for child_span, child_self_ns, child_tid in self._pending_children.pop(span.context.span_id, ()):
            self._flush_subtree_locked(child_span, child_self_ns, child_tid, cur_stack)

    def total_duration_s(self):
        with self._lock:
            folded_ns = sum(self._folded_stacks.values())
            if folded_ns > 0:
                return folded_ns / 1e9
            if self._origin_ns is not None and self._last_end_ns is not None:
                return max(0, self._last_end_ns - self._origin_ns) / 1e9
            return 0.0

    def wall_duration_s(self):
        with self._lock:
            if self._origin_ns is not None and self._last_end_ns is not None:
                return max(0, self._last_end_ns - self._origin_ns) / 1e9
            return 0.0

    def get_folded_stacks(self):
        """Return dict mapping ``'frame1;frame2;...'`` -> integer microseconds."""
        with self._lock:
            result = {}
            for stack_tuple, ns in sorted(self._folded_stacks.items()):
                if ns > 0:
                    key = ';'.join(stack_tuple)
                    result[key] = result.get(key, 0) + max(1, int(round(ns / 1000.0)))
            return result

    def to_dict(self, include_trace_events=True):
        total_s = self.total_duration_s()
        wall_s = self.wall_duration_s()
        wall_denom = wall_s if wall_s > 0 else total_s

        def _sorted_map(mapping, denom=total_s):
            return {k: v.to_dict(denom) for k, v in sorted(mapping.items(), key=lambda kv: -kv[1].total_s)}

        def _sorted_pairs(mapping):
            return [
                dict(resource_type=k[0], fetch_type=k[1], **v.to_dict(total_s))
                for k, v in sorted(mapping.items(), key=lambda kv: -kv[1].total_s)
            ]

        with self._lock:
            summary = {
                'total_duration_s': round(total_s, 6),
                'wall_duration_s': round(wall_s, 6),
                'distros': list(self._distros_seen),
                'by_phase': _sorted_map(self._by_phase, denom=wall_denom),
                'by_distro': _sorted_map(self._by_distro, denom=wall_denom),
                'by_fetch_type': _sorted_map(self._by_fetch_type),
                'by_resource_type': _sorted_map(self._by_resource_type),
                'by_resource_and_fetch_type': _sorted_pairs(self._by_resource_fetch),
                'by_sub_resource_and_fetch_type': _sorted_pairs(self._by_sub_resource_fetch),
                'by_operation': _sorted_map(self._by_operation),
                'slowest_events': [item[2] for item in sorted(self._slowest_heap, key=lambda x: -x[0])],
            }
            trace_events = []
            if include_trace_events:
                pid = os.getpid()
                arg_keys = ('resource_type', 'fetch_type', 'target', 'distro', 'status', 'detail')
                for ev in self._trace_events:
                    trace_events.append({
                        'name': ev[0], 'cat': ev[1], 'ph': 'X', 'ts': ev[2], 'dur': ev[3],
                        'pid': pid, 'tid': ev[4],
                        'args': {k: v for k, v in zip(arg_keys, ev[5:]) if v},
                    })

        summary['folded_stacks'] = self.get_folded_stacks()
        return {'traceEvents': trace_events, 'rosdistroProfile': summary}

    def format_summary(self):
        """Render a human-readable multi-table profiling summary report."""
        data = self.to_dict(include_trace_events=False)['rosdistroProfile']
        total_s = data['total_duration_s']
        wall_s = data.get('wall_duration_s', total_s)
        total_line = 'Total profiled time: %s' % format_duration(total_s)
        if wall_s > 0 and total_s > wall_s * 1.05:
            total_line += ' (wall: %s)' % format_duration(wall_s)
        lines = [
            '=' * 88,
            'ROSDISTRO CACHE PROFILING SUMMARY',
            '=' * 88,
            total_line,
        ]
        if data['distros']:
            lines.append('Distros: %s' % ', '.join(data['distros']))

        def _append_table(title, rows, key_header):
            if not rows:
                return
            lines.extend([
                '', title, '-' * 88,
                '%-36s %7s %10s %6s %9s %9s %9s' % (key_header, 'Count', 'Total', '%Tot', 'Avg', 'P95', 'Max'),
                '-' * 88,
            ])
            for label, st in rows:
                disp = label if len(label) <= 36 else label[:33] + '...'
                lines.append(
                    '%-36s %7d %10s %5.1f%% %9s %9s %9s' % (
                        disp, st['count'], format_duration(st['total_s']), st['pct_of_total'],
                        format_duration(st['avg_s']), format_duration(st['p95_s']), format_duration(st['max_s']),
                    )
                )

        _append_table('By Phase', list(data['by_phase'].items()), 'Phase')
        _append_table('By Distro', list(data['by_distro'].items()), 'Distro')
        _append_table('By Fetch Type (Top-Level Fetches)', list(data['by_fetch_type'].items()), 'Fetch Type')
        _append_table('By Resource Type (Top-Level Fetches)', list(data['by_resource_type'].items()), 'Resource Type')
        _append_table(
            'By Resource Type x Fetch Type (Top-Level)',
            [('%s [%s]' % (r['resource_type'], r['fetch_type']), r) for r in data['by_resource_and_fetch_type']],
            'Resource [Fetch Type]',
        )
        _append_table(
            'By Sub-Resource x Fetch Type (Inside Providers)',
            [('%s [%s]' % (r['resource_type'], r['fetch_type']), r) for r in data['by_sub_resource_and_fetch_type']],
            'Sub-Resource [Fetch Type]',
        )
        _append_table('By Underlying Operation', list(data['by_operation'].items()), 'Operation')

        if data['slowest_events']:
            lines.extend(['', 'Top Slowest Individual Fetches', '-' * 88])
            for idx, ev in enumerate(data['slowest_events'], 1):
                detail_str = ' (%s)' % ev['detail'] if ev['detail'] else ''
                distro_str = '[%s] ' % ev['dist_name'] if ev['dist_name'] else ''
                lines.append(
                    '%2d. %10s  %s[%s] %s via=%s [%s]%s' % (
                        idx, format_duration(ev['duration_s']), distro_str, ev['resource_type'],
                        ev['target'], ev['fetch_type'], ev['status'], detail_str,
                    )
                )

        lines.append('=' * 88)
        return '\n'.join(lines) + '\n'


@contextmanager
def _noop_ctx():
    yield


@contextmanager
def _run_span(tracer, name, attrs, fetch_type=None, status='ok', detail=None, target=None, dist_name=None):
    start_s = time.perf_counter()
    otel_ctx = tracer.start_as_current_span(name, attributes=attrs) if tracer is not None else _noop_ctx()
    with otel_ctx as otel_span:
        handle = _SpanHandle(
            otel_span, fetch_type=fetch_type, status=status,
            detail=detail, target=target, dist_name=dist_name,
        )
        try:
            yield handle
        except BaseException:
            if handle.status == 'ok':
                handle.status = 'failed'
            raise
        finally:
            handle.duration_s = max(0.0, time.perf_counter() - start_s)


class CacheProfiler(FetchReporter):
    """OpenTelemetry-backed hierarchical profiler layered on FetchReporter."""

    def __init__(
        self, enabled=True, record_spans=True, record_trace_events=False,
        debug=False, console_stream=None, slowest_limit=25, tracer_provider=None,
    ):
        super(CacheProfiler, self).__init__(debug=debug, console_stream=console_stream)
        self.enabled = bool(enabled)
        self.record_spans = bool(enabled and record_spans)
        self.record_trace_events = bool(enabled and record_trace_events)

        self._processor = None
        self._tracer_provider = None
        self._owns_provider = False
        self._tracer = None

        if self.record_spans:
            if not (HAS_OTEL_API and HAS_OTEL_SDK):
                raise RuntimeError(
                    "Profiling requires OpenTelemetry. Install with: pip install 'rosdistro[profile]' "
                    '(or: pip install opentelemetry-api opentelemetry-sdk)'
                )
            self._processor = RosdistroSpanProcessor(
                record_trace_events=self.record_trace_events, slowest_limit=slowest_limit,
            )
            if tracer_provider is None:
                self._tracer_provider = TracerProvider(
                    resource=Resource.create({'service.name': 'rosdistro'}), shutdown_on_exit=False,
                )
                self._owns_provider = True
            else:
                self._tracer_provider = tracer_provider
            self._tracer_provider.add_span_processor(self._processor)
            self._tracer = self._tracer_provider.get_tracer('rosdistro')
        elif self.enabled and HAS_OTEL_API:
            # Forward spans to global OTel tracer (no-op unless externally configured, e.g. opentelemetry-instrument).
            self._tracer = otel_trace.get_tracer('rosdistro')

    @property
    def tracer(self):
        return self._tracer

    @property
    def tracer_provider(self):
        return self._tracer_provider

    @property
    def span_processor(self):
        return self._processor

    def has_active_span(self):
        return bool(HAS_OTEL_API and otel_trace.get_current_span().is_recording())

    def capture_thread_context(self):
        reporter_ctx = super(CacheProfiler, self).capture_thread_context()
        otel_ctx = (
            otel_context.get_current()
            if self.enabled and HAS_OTEL_API and otel_context is not None
            else None
        )
        return (reporter_ctx, otel_ctx)

    @contextmanager
    def attach_thread_context(self, captured):
        reporter_ctx, otel_ctx = captured
        token = None
        if otel_ctx is not None and HAS_OTEL_API and otel_context is not None:
            token = otel_context.attach(otel_ctx)
        try:
            with use_profiler(self), super(CacheProfiler, self).attach_thread_context(reporter_ctx):
                yield
        finally:
            if token is not None and HAS_OTEL_API and otel_context is not None:
                otel_context.detach(token)

    def wall_duration_s(self):
        return self._processor.wall_duration_s() if self._processor is not None else 0.0

    def shutdown(self):
        if self._tracer_provider is not None:
            if self._owns_provider:
                self._tracer_provider.shutdown()
            else:
                self._tracer_provider.force_flush()

    @contextmanager
    def span(
        self, name, kind='operation', resource_type=None, fetch_type=None,
        target=None, dist_name=None, status='ok', detail=None, deferred_label=False,
    ):
        """Time a hierarchical OpenTelemetry span while maintaining FetchReporter scopes."""
        effective_dist = dist_name or self.current_dist_name()
        attrs = _build_attributes(
            kind, resource_type or name, fetch_type, target, effective_dist, status, detail, deferred_label,
        )
        dist_ctx = self.distro_scope(dist_name) if dist_name else _noop_ctx()
        fetch_ctx = self.fetch_scope() if kind == 'fetch' else _noop_ctx()
        with dist_ctx, fetch_ctx, _run_span(
            self._tracer if self.enabled else None, name, attrs,
            fetch_type=fetch_type, status=status, detail=detail, target=target, dist_name=effective_dist,
        ) as handle:
            yield handle

    def record_instant_fetch(
        self, resource_type, target, fetch_type, status='ok',
        dist_name=None, detail=None, kind='sub_fetch', emit_console=False,
    ):
        """Record a zero-duration OpenTelemetry span (e.g. derived asset list or skipped item)."""
        effective_dist = dist_name or self.current_dist_name()
        if self.enabled and self._tracer is not None:
            attrs = _build_attributes(kind, resource_type, fetch_type, target, effective_dist, status, detail)
            otel_span = self._tracer.start_span(resource_type, attributes=attrs)
            otel_span.end(end_time=getattr(otel_span, 'start_time', None))
        if emit_console:
            self.report_fetch(
                resource_type=resource_type, target=target, fetch_type=fetch_type,
                status=status, duration_s=0.0, dist_name=effective_dist, detail=detail,
                level='top' if kind == 'fetch' else 'sub',
            )

    @contextmanager
    def sub_fetch(self, resource_type, target, fetch_type, detail=None):
        """Override FetchReporter.sub_fetch to also record an OpenTelemetry sub_fetch span."""
        sp = None
        try:
            with self.span(
                resource_type, kind='sub_fetch', resource_type=resource_type,
                fetch_type=fetch_type, target=target, detail=detail,
            ) as sp:
                yield sp
        finally:
            if sp is not None:
                self.report_fetch(
                    resource_type=resource_type, target=sp.target, fetch_type=fetch_type,
                    status=sp.status, duration_s=sp.duration_s, dist_name=sp.dist_name,
                    detail=sp.detail, level='sub',
                )

    def instant_sub_fetch(self, resource_type, target, fetch_type, status='ok', detail=None):
        """Override FetchReporter.instant_sub_fetch to also record a sub_fetch span."""
        self.record_instant_fetch(
            resource_type, target, fetch_type, status=status, detail=detail,
            kind='sub_fetch', emit_console=True,
        )

    def format_phase_summary(self, phase_label, counts_by_fetch, durations_by_fetch, total_s, dist_name=None):
        return format_phase_summary(phase_label, counts_by_fetch, durations_by_fetch, total_s, dist_name=dist_name)

    def total_duration_s(self):
        return self._processor.total_duration_s() if self._processor is not None else 0.0

    def get_folded_stacks(self):
        return self._processor.get_folded_stacks() if self._processor is not None else {}

    def format_folded_stacks(self):
        folded = self.get_folded_stacks()
        return '\n'.join('%s %d' % (stack, us) for stack, us in sorted(folded.items())) + ('\n' if folded else '')

    def write_folded_stacks(self, path):
        _write_text(path, self.format_folded_stacks())

    def to_dict(self, include_trace_events=True):
        if self._processor is None:
            return {'traceEvents': [], 'rosdistroProfile': {}}
        return self._processor.to_dict(include_trace_events=include_trace_events)

    def write_json_report(self, path, include_trace_events=True):
        _write_text(path, json.dumps(self.to_dict(include_trace_events=include_trace_events), indent=2))

    def format_summary(self):
        return self._processor.format_summary() if self._processor is not None else ''

    def write_all(self, output_dir):
        """Write folded stacks, Chrome Trace JSON, and text summary into *output_dir*."""
        os.makedirs(output_dir, exist_ok=True)
        paths = {
            'folded': os.path.join(output_dir, 'profile-stacks.folded'),
            'json': os.path.join(output_dir, 'profile-trace.json'),
            'summary': os.path.join(output_dir, 'profile-summary.txt'),
        }
        self.write_folded_stacks(paths['folded'])
        self.write_json_report(paths['json'])
        _write_text(paths['summary'], self.format_summary())
        return paths


def get_active_profiler():
    """Return the active CacheProfiler for the current thread, or None."""
    return getattr(_active_profiler_local, 'profiler', None)


@contextmanager
def use_profiler(profiler):
    """Install *profiler* as both the active CacheProfiler and FetchReporter for the current thread."""
    prev = getattr(_active_profiler_local, 'profiler', None)
    _active_profiler_local.profiler = profiler
    try:
        with use_reporter(profiler):
            yield profiler
    finally:
        _active_profiler_local.profiler = prev


@contextmanager
def trace_span(
    name, kind='operation', resource_type=None, fetch_type=None,
    target=None, dist_name=None, status='ok', detail=None, deferred_label=False,
):
    """Open a span on the active profiler (or global OpenTelemetry tracer if installed)."""
    profiler = get_active_profiler()
    if profiler is not None:
        with profiler.span(
            name, kind=kind, resource_type=resource_type, fetch_type=fetch_type,
            target=target, dist_name=dist_name, status=status, detail=detail, deferred_label=deferred_label,
        ) as sp:
            yield sp
        return
    tracer = otel_trace.get_tracer('rosdistro') if HAS_OTEL_API else None
    attrs = _build_attributes(
        kind, resource_type or name, fetch_type, target, dist_name, status, detail, deferred_label,
    )
    with _run_span(
        tracer, name, attrs, fetch_type=fetch_type, status=status,
        detail=detail, target=target, dist_name=dist_name,
    ) as sp:
        yield sp


@contextmanager
def trace_operation(name, fetch_type=None, target=None, detail=None):
    """Trace an underlying operation (e.g. git:ls-remote, http:get, sanitize:xml)."""
    with trace_span(
        name, kind='operation', resource_type=name, fetch_type=fetch_type, target=target, detail=detail,
    ) as sp:
        yield sp
