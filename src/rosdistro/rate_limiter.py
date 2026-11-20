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
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of Open Source Robotics Foundation, Inc. nor the
#    names of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written permission.
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

"""Host-aware rate limiting, pacing, and coordinated backoff across threads."""

from contextlib import contextmanager
import datetime
import email.utils
import os
import random
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse

from . import logger
from .cache_profiler import sanitize_url_for_trace, trace_operation
from .manifest_provider import get_http_header

DEFAULT_RETRY_MAX = 30.0
DEFAULT_MAX_ADAPTIVE_INTERVAL = 10.0

DEFAULT_HTTP_HOST_INTERVALS = {
    'api.github.com': 0.25,
    'raw.githubusercontent.com': 0.05,
    'gitlab.com': 0.2,
}
DEFAULT_HTTP_INTERVAL = 0.1
DEFAULT_GIT_INTERVAL = 0.2

DEFAULT_HTTP_HOST_CONCURRENCY = {
    'api.github.com': 4,
    'raw.githubusercontent.com': 8,
    'gitlab.com': 4,
}
DEFAULT_HTTP_CONCURRENCY = 4

_LOCAL_HOSTS = frozenset(('', 'localhost', '127.0.0.1', '::1'))
_WINDOWS_DRIVE_RE = re.compile(r'^[a-zA-Z]:[\\/]')
_GIT_RATE_LIMIT_PATTERNS = (
    'returned error: 429',
    'http 429',
    'status 429',
    'too many requests',
    'rate limit',
    'ratelimit',
    'secondary rate limit',
    'abuse detection',
)

_active_limiter_local = threading.local()


class HostRateLimitError(RuntimeError):
    """Raised when a host is already rate-limited beyond the caller's retry budget."""

    def __init__(self, host, retry_after, reason=None, status_code=429):
        self.host = host
        self.retry_after = float(retry_after)
        self.reason = reason
        self.status_code = status_code
        super(HostRateLimitError, self).__init__(
            reason or 'Rate limit active for %s (resets in %.0f seconds); skipping request' % (host, self.retry_after)
        )


def get_retry_max(override=None):
    """Return the configured per-request retry/backoff budget in seconds."""
    if override is not None:
        return max(0.0, float(override))
    raw = os.getenv('ROSDISTRO_RETRY_MAX')
    return DEFAULT_RETRY_MAX if not raw else max(0.0, float(raw))


def extract_host(url):
    """Return lowercase remote hostname for *url*, or None for local/file/loopback targets."""
    if url is None:
        return None
    raw = (url.get_full_url() if hasattr(url, 'get_full_url') else str(url)).strip()
    if not raw or _WINDOWS_DRIVE_RE.match(raw) or raw.startswith(('/', './', '../')):
        return None
    if '://' in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() == 'file':
            return None
        host = (parsed.hostname or '').lower()
        return None if host in _LOCAL_HOSTS else host
    if ':' in raw and '/' not in raw.split(':', 1)[0]:
        host = raw.split(':', 1)[0].rsplit('@', 1)[-1].strip('[]').lower()
        return None if host in _LOCAL_HOSTS else host
    return None


def _clean_header(headers, name):
    val = get_http_header(headers, name)
    if val is None or isinstance(val, bool) or not isinstance(val, (str, int, float)):
        return None
    return str(val).strip()


def parse_retry_after(value, now_wall=None):
    """Parse an HTTP Retry-After header (seconds or HTTP-date) into delay seconds."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, dt.timestamp() - (time.time() if now_wall is None else now_wall))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_rate_limit_reset(value, now_wall=None):
    """Parse X-RateLimit-Reset / RateLimit-Reset (epoch timestamp or delta seconds)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num >= 1_000_000_000.0:
        return num - (time.time() if now_wall is None else now_wall)
    return num


def is_git_rate_limit_error(error_text):
    """Return True if git stderr text indicates HTTP 429 or remote rate-limiting."""
    if not error_text:
        return False
    lower = str(error_text).lower()
    return any(pat in lower for pat in _GIT_RATE_LIMIT_PATTERNS)


def _default_jitter(delay):
    return delay * random.uniform(0.85, 1.15) if delay > 0 else 0.0


class _HostBucket(object):
    def __init__(self, base_interval, max_concurrent=None, max_interval=DEFAULT_MAX_ADAPTIVE_INTERVAL):
        self.lock = threading.Lock()
        self.base_interval = max(0.0, float(base_interval))
        self.current_interval = self.base_interval
        self.max_interval = max(self.base_interval, float(max_interval))
        self.next_slot_time = 0.0
        self.blocked_until = 0.0
        self.block_reason = None
        self.block_status_code = 429
        self.consecutive_limits = 0
        self.warned_long_block = False
        self.semaphore = (
            threading.BoundedSemaphore(max_concurrent)
            if max_concurrent is not None and max_concurrent > 0
            else None
        )


class RateLimiter(object):
    """Thread-safe per-(protocol, host) pacing, concurrency cap, and coordinated backoff."""

    def __init__(
        self,
        http_host_intervals=None,
        http_host_concurrency=None,
        default_http_interval=DEFAULT_HTTP_INTERVAL,
        default_git_interval=DEFAULT_GIT_INTERVAL,
        default_http_concurrency=DEFAULT_HTTP_CONCURRENCY,
        max_adaptive_interval=DEFAULT_MAX_ADAPTIVE_INTERVAL,
        monotonic_fn=None,
        wall_time_fn=None,
        jitter_fn=None,
    ):
        self._lock = threading.Lock()
        self._buckets = {}
        raw_interval = os.getenv('ROSDISTRO_HOST_MIN_INTERVAL')
        self._env_interval = max(0.0, float(raw_interval)) if raw_interval else None
        raw_conc = os.getenv('ROSDISTRO_MAX_CONCURRENT_PER_HOST')
        self._env_conc = int(raw_conc) if raw_conc and int(raw_conc) > 0 else None

        self._http_intervals = {k.lower(): v for k, v in DEFAULT_HTTP_HOST_INTERVALS.items()}
        if http_host_intervals:
            self._http_intervals.update({k.lower(): max(0.0, float(v)) for k, v in http_host_intervals.items()})
        self._http_concurrency = {k.lower(): v for k, v in DEFAULT_HTTP_HOST_CONCURRENCY.items()}
        if http_host_concurrency:
            self._http_concurrency.update({k.lower(): v for k, v in http_host_concurrency.items()})

        self._default_http_interval = max(0.0, float(default_http_interval))
        self._default_git_interval = max(0.0, float(default_git_interval))
        self._default_http_concurrency = default_http_concurrency
        self._max_adaptive_interval = max(0.0, float(max_adaptive_interval))
        self._monotonic = monotonic_fn or time.monotonic
        self._wall_time = wall_time_fn or time.time
        self._jitter = jitter_fn or _default_jitter

    def _get_bucket(self, protocol, host):
        norm = host.lower()
        key = (protocol, norm)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if self._env_interval is not None:
                    interval = self._env_interval
                elif protocol == 'http':
                    interval = self._http_intervals.get(norm, self._default_http_interval)
                else:
                    interval = self._default_git_interval

                if self._env_conc is not None:
                    conc = self._env_conc
                elif protocol == 'http':
                    conc = self._http_concurrency.get(norm, self._default_http_concurrency)
                else:
                    conc = None

                bucket = _HostBucket(interval, conc, self._max_adaptive_interval)
                self._buckets[key] = bucket
            return bucket

    @contextmanager
    def request_slot(
        self, protocol, host, remaining_budget=None, sleep_fn=None,
        fetch_type=None, target=None, min_monotonic=0.0,
    ):
        """Acquire pacing + concurrency slot; yields backoff seconds waited."""
        if not host:
            yield 0.0
            return

        bucket = self._get_bucket(protocol, host)
        sleeper = sleep_fn or time.sleep
        budget = remaining_budget
        waited_until = float(min_monotonic or 0.0)
        backoff_waited = 0.0

        while True:
            with bucket.lock:
                effective_now = max(self._monotonic(), waited_until)
                is_backoff = bucket.blocked_until > effective_now
                if is_backoff and budget is not None and (bucket.blocked_until - effective_now) > budget:
                    raise HostRateLimitError(
                        host, bucket.blocked_until - effective_now,
                        reason=bucket.block_reason, status_code=bucket.block_status_code,
                    )
                effective_start = max(effective_now, bucket.blocked_until, bucket.next_slot_time)
                wait = effective_start - effective_now
                if is_backoff and budget is not None and wait > budget:
                    raise HostRateLimitError(
                        host, wait, reason=bucket.block_reason, status_code=bucket.block_status_code,
                    )
                bucket.next_slot_time = effective_start + bucket.current_interval

            if wait <= 0:
                break

            op_name = '%s:%s' % (protocol, 'rate_limit_sleep' if is_backoff else 'rate_limit_pace')
            with trace_operation(op_name, fetch_type=fetch_type, target=target):
                sleeper(wait)
            if is_backoff:
                backoff_waited += wait
                if budget is not None:
                    budget = max(0.0, budget - wait)

            waited_until = effective_start
            with bucket.lock:
                if bucket.blocked_until <= max(self._monotonic(), waited_until):
                    break

        sem = bucket.semaphore
        if sem is not None and not sem.acquire(blocking=False):
            op_prefix = 'http' if protocol.startswith('http') else protocol
            with trace_operation('%s:rate_limit_wait' % op_prefix, fetch_type=fetch_type, target=target):
                sem.acquire()
        try:
            with bucket.lock:
                effective_now = max(self._monotonic(), waited_until)
                if bucket.blocked_until > effective_now and budget is not None:
                    block_wait = bucket.blocked_until - effective_now
                    if block_wait > budget:
                        raise HostRateLimitError(
                            host, block_wait, reason=bucket.block_reason, status_code=bucket.block_status_code,
                        )
            yield backoff_waited
        finally:
            if sem is not None:
                sem.release()

    def record_success(self, protocol, host):
        if not host:
            return
        bucket = self._get_bucket(protocol, host)
        with bucket.lock:
            bucket.consecutive_limits = 0
            if bucket.current_interval > bucket.base_interval:
                bucket.current_interval = max(bucket.base_interval, bucket.current_interval * 0.85)
            if bucket.blocked_until <= self._monotonic():
                bucket.warned_long_block = False

    def record_response_headers(self, protocol, host, headers):
        if not host:
            return
        self.record_success(protocol, host)
        if not headers:
            return
        remaining = _clean_header(headers, 'X-RateLimit-Remaining') or _clean_header(headers, 'RateLimit-Remaining')
        if remaining != '0':
            return
        reset_raw = _clean_header(headers, 'X-RateLimit-Reset') or _clean_header(headers, 'RateLimit-Reset')
        reset_wait = parse_rate_limit_reset(reset_raw, now_wall=self._wall_time())
        if reset_wait is not None and reset_wait > 0:
            bucket = self._get_bucket(protocol, host)
            with bucket.lock:
                until = self._monotonic() + reset_wait + 0.5
                if until > bucket.blocked_until:
                    bucket.blocked_until = until
                    bucket.next_slot_time = max(bucket.next_slot_time, until)
                    bucket.block_reason = 'Rate limit exhausted for %s (resets in %.0f seconds)' % (host, reset_wait)
                    bucket.block_status_code = 403

    def record_rate_limit(
        self, protocol, host, retry_after=None, reset_wait=None,
        remaining_budget=None, status_code=429, reason=None,
        backoff_base=1.0, backoff_factor=1.5,
    ):
        """Record a rate limit and return ``(delay_s_or_none, should_warn, blocked_until)``."""
        if not host:
            return None, False, 0.0
        bucket = self._get_bucket(protocol, host)
        with bucket.lock:
            now = self._monotonic()
            bucket.consecutive_limits += 1
            bucket.current_interval = min(bucket.max_interval, max(bucket.current_interval * 2.0, 0.5))

            explicit_wait = None
            if retry_after is not None:
                explicit_wait = max(0.0, float(retry_after))
            elif reset_wait is not None and reset_wait > 0:
                explicit_wait = float(reset_wait) + 0.5

            if explicit_wait is not None:
                if explicit_wait > 0:
                    until = now + explicit_wait
                    if until > bucket.blocked_until:
                        bucket.blocked_until = until
                        bucket.next_slot_time = max(bucket.next_slot_time, until)
                    bucket.block_reason = reason or (
                        'Rate limit active for %s (resets in %.0f seconds)' % (host, explicit_wait)
                    )
                    bucket.block_status_code = status_code
                if remaining_budget is not None and explicit_wait > remaining_budget:
                    should_warn = not bucket.warned_long_block
                    bucket.warned_long_block = True
                    return None, should_warn, bucket.blocked_until
                delay = explicit_wait if explicit_wait > 0 else backoff_base
                if remaining_budget is not None:
                    delay = min(delay, remaining_budget)
                return delay, False, bucket.blocked_until

            if remaining_budget is not None and remaining_budget <= 0:
                return None, False, bucket.blocked_until

            raw_delay = backoff_base * (backoff_factor ** (bucket.consecutive_limits - 1))
            delay = self._jitter(min(raw_delay, max(backoff_base, bucket.max_interval * 3.0)))
            if remaining_budget is not None:
                delay = min(delay, remaining_budget)
            if delay <= 0:
                return None, False, bucket.blocked_until
            until = now + delay
            if until > bucket.blocked_until:
                bucket.blocked_until = until
                bucket.next_slot_time = max(bucket.next_slot_time, until)
            bucket.block_reason = reason
            bucket.block_status_code = status_code
            return delay, False, bucket.blocked_until


def get_active_rate_limiter():
    """Return the active RateLimiter for the current thread, or None."""
    return getattr(_active_limiter_local, 'limiter', None)


@contextmanager
def use_rate_limiter(limiter):
    """Install *limiter* as the active RateLimiter for the current thread."""
    prev = getattr(_active_limiter_local, 'limiter', None)
    _active_limiter_local.limiter = limiter
    try:
        yield limiter
    finally:
        _active_limiter_local.limiter = prev


def open_url_with_backoff(
    req,
    *,
    urlopen_fn,
    fetch_type,
    timeout,
    retry_max,
    is_retryable_fn,
    sleep_fn=time.sleep,
    remaining_header='X-RateLimit-Remaining',
    reset_header='X-RateLimit-Reset',
    warn_fn=None,
    rate_limit_hint=None,
    provider_label=None,
    deadline=None,
):
    """Execute *urlopen_fn(req, timeout=timeout)* with host-aware pacing and rate-limit backoff.

    Returns ``(response, slot_cm)``; the caller must close *response* and exit *slot_cm* (if not None).
    """
    raw_url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
    url_str = sanitize_url_for_trace(raw_url)
    host = extract_host(raw_url)
    limiter = get_active_rate_limiter()
    label = provider_label or fetch_type.capitalize()
    remaining_budget = max(0.0, float(retry_max))
    backoff = 1.0
    min_monotonic = 0.0

    while True:
        if deadline is not None:
            remaining_budget = min(remaining_budget, max(0.0, deadline - time.monotonic()))
        slot_cm = (
            limiter.request_slot(
                'http', host, remaining_budget=remaining_budget, sleep_fn=sleep_fn,
                fetch_type=fetch_type, target=url_str, min_monotonic=min_monotonic,
            )
            if limiter is not None and host is not None else None
        )
        backoff_waited = slot_cm.__enter__() if slot_cm is not None else 0.0
        try:
            remaining_budget = max(0.0, remaining_budget - backoff_waited)
            with trace_operation('http:get', fetch_type=fetch_type, target=url_str):
                resp = urlopen_fn(req, timeout=timeout)
            if limiter is not None and host is not None:
                limiter.record_response_headers('http', host, getattr(resp, 'headers', None))
            return resp, slot_cm
        except HTTPError as e:
            if slot_cm is not None:
                slot_cm.__exit__(type(e), e, e.__traceback__)
            headers = getattr(e, 'headers', None)
            now_wall = limiter._wall_time() if limiter is not None else None
            retry_after = parse_retry_after(_clean_header(headers, 'Retry-After'), now_wall=now_wall)
            if not is_retryable_fn(e, retry_after):
                raise
            rem_hdr = _clean_header(headers, remaining_header)
            reset_wait = (
                parse_rate_limit_reset(_clean_header(headers, reset_header), now_wall=now_wall)
                if rem_hdr == '0' else None
            )
            wait_hint = retry_after if retry_after is not None else reset_wait
            reason = None
            if wait_hint is not None and wait_hint > remaining_budget:
                reason = (
                    '%s rate limit exceeded for %s (resets in %.0f seconds, '
                    'exceeds retry budget %.0f seconds); failing fast so fallback providers can run.%s'
                    % (
                        label, url_str, wait_hint, remaining_budget,
                        (' ' + rate_limit_hint) if rate_limit_hint else '',
                    )
                )
            if limiter is not None and host is not None:
                delay, should_warn, blocked_until = limiter.record_rate_limit(
                    'http', host, retry_after=retry_after, reset_wait=reset_wait,
                    remaining_budget=remaining_budget, status_code=e.code,
                    reason=reason, backoff_base=backoff, backoff_factor=1.5,
                )
                if delay is None or delay <= 0:
                    if should_warn and reason and warn_fn is not None:
                        warn_fn(reason)
                    elif reason:
                        logger.debug(reason)
                    raise
                min_monotonic = max(min_monotonic, blocked_until)
            else:
                if remaining_budget <= 0:
                    raise
                if retry_after is not None:
                    if retry_after > remaining_budget:
                        if reason and warn_fn is not None:
                            warn_fn(reason)
                        raise
                    delay = retry_after if retry_after > 0 else min(backoff, remaining_budget)
                elif reset_wait is not None:
                    if reset_wait > remaining_budget:
                        if reason and warn_fn is not None:
                            warn_fn(reason)
                        raise
                    delay = min(reset_wait + 0.5, remaining_budget) if reset_wait > 0 else min(backoff, remaining_budget)
                else:
                    delay = min(backoff, remaining_budget)
                if delay <= 0:
                    raise
            logger.debug(
                'Fetch of %s failed with %d, assuming rate limit, retrying after period %.1f seconds.'
                % (url_str, e.code, delay)
            )
            with trace_operation('http:rate_limit_sleep', fetch_type=fetch_type, target=url_str):
                sleep_fn(delay)
            remaining_budget = max(0.0, remaining_budget - delay)
            backoff *= 1.5
        except BaseException as e:
            if slot_cm is not None:
                slot_cm.__exit__(type(e), e, e.__traceback__)
            raise


def run_git_with_backoff(run_fn, url, retry_max=None, sleep_fn=time.sleep, on_retry=None):
    """Run a git command callable with host-aware pacing and HTTP 429/rate-limit backoff."""
    url_str = sanitize_url_for_trace(url)
    host = extract_host(url)
    limiter = get_active_rate_limiter()
    remaining_budget = get_retry_max(retry_max)
    backoff = 1.0
    min_monotonic = 0.0

    while True:
        if limiter is not None and host is not None:
            with limiter.request_slot(
                'git', host, remaining_budget=remaining_budget, sleep_fn=sleep_fn,
                fetch_type='git', target=url_str, min_monotonic=min_monotonic,
            ) as backoff_waited:
                remaining_budget = max(0.0, remaining_budget - backoff_waited)
                result = run_fn()
        else:
            result = run_fn()

        if result['returncode'] == 0:
            if limiter is not None and host is not None:
                limiter.record_success('git', host)
            return result

        if result.get('timed_out') or not is_git_rate_limit_error(result.get('error') or ''):
            return result

        if limiter is not None and host is not None:
            delay, _, blocked_until = limiter.record_rate_limit(
                'git', host, remaining_budget=remaining_budget, status_code=429,
                reason='Git rate limit exceeded for %s' % host,
                backoff_base=backoff, backoff_factor=2.0,
            )
            if delay is None or delay <= 0:
                result['rate_limited'] = True
                return result
            min_monotonic = max(min_monotonic, blocked_until)
        else:
            if remaining_budget <= 0:
                result['rate_limited'] = True
                return result
            delay = min(backoff, remaining_budget)

        logger.debug('Git operation on "%s" hit rate limit, retrying after %.1f seconds.' % (url_str, delay))
        with trace_operation('git:rate_limit_sleep', fetch_type='git', target=url_str):
            sleep_fn(delay)
        remaining_budget = max(0.0, remaining_budget - delay)
        backoff *= 2.0
        if on_retry is not None:
            on_retry()
