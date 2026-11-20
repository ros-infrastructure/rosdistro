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

import argparse
import logging
import os
import sys

from rosdistro import logger
from rosdistro.cache_profiler import CacheProfiler, use_profiler
from rosdistro.distribution_cache import CacheConfig
from rosdistro.distribution_cache_generator import (
    DEFAULT_SOURCE_WORKERS,
    generate_distribution_caches,
)

logging.basicConfig(level=logging.INFO)


def _non_negative_int(value):
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    if parsed < 0:
        raise argparse.ArgumentTypeError('must be >= 0 (got %d)' % parsed)
    return parsed


def _positive_int(value):
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be >= 1, got %r' % (value,))
    return parsed


def parse_args(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(
        description='Build the cache for rosdistro distributions.'
    )
    add = parser.add_argument
    add(
        'index',
        help='The path or url of the index.yaml file (must be either a local file or a file:// url)')
    add(
        'dist_names', nargs='*',
        help='The names of the distributions (default: all)')
    add(
        '--debug', action='store_true', default=False,
        help='Output debug messages')
    add(
        '--preclean', action='store_true', default=False,
        help='Build the cache from scratch instead of reusing cached data')
    add(
        '--ignore-local', action='store_true', default=False,
        help='Ignore locally available cache')
    add(
        '-c', '--config', default=None,
        help='Path to a YAML configuration file (or preset "minimal"/"extended") specifying cache elements to index')
    add(
        '--extended', action='store_true', default=False,
        help='Use the default extended cache configuration when --config is not specified')
    add(
        '--include-source', action='store_true', default=False,
        help='Deprecated: use --config instead. Also include source repository resources in the cache')
    add(
        '--limit', type=int, default=None,
        help='Limit the number of packages/repositories to scan (useful for testing)')
    add(
        '--min-update-delta', type=_non_negative_int, default=None, metavar='SECONDS',
        help='Minimum age in seconds before cached source repositories are revalidated '
             '(0 forces revalidation; overrides config value, default: %d)'
             % CacheConfig.DEFAULT_MIN_UPDATE_DELTA)
    add(
        '--strict', action='store_true', default=False,
        help='Fail and skip writing a distribution cache if any release package.xml is missing, invalid, or has a version mismatch')
    add(
        '-j', '--jobs', type=_positive_int, default=None, metavar='N',
        help='Number of parallel worker threads for source repository queries '
             '(default: ROSDISTRO_SOURCE_WORKERS or %d)' % DEFAULT_SOURCE_WORKERS)
    add(
        '--profile', action='store_true', default=False,
        help='Collect OpenTelemetry profiling timings and print a timing summary report')
    add(
        '--profile-folded', default=None, metavar='PATH',
        help='Write Brendan Gregg folded stacks to PATH (implies --profile; view in speedscope.app or render with flamegraph.pl / inferno-flamegraph)')
    add(
        '--profile-json', default=None, metavar='PATH',
        help='Write structured JSON profiling report and Chrome trace events to PATH (implies --profile; view in ui.perfetto.dev or speedscope.app)')
    add(
        '--profile-dir', default=None, metavar='DIR',
        help='Write folded stacks, Chrome trace JSON, and text summary into DIR (implies --profile)')
    return parser.parse_args(args)


def main(args=None):
    args = parse_args(args)

    if args.debug:
        logger.level = logging.DEBUG

    config = args.config if args.config is not None else ('extended' if args.extended else None)

    profile_dir = args.profile_dir or os.getenv('ROSDISTRO_PROFILE_DIR') or None
    env_profile = os.getenv('ROSDISTRO_PROFILE', '').lower() in ('1', 'true', 'yes', 'on')
    profiling_enabled = bool(
        args.profile
        or env_profile
        or args.profile_folded
        or args.profile_json
        or profile_dir
    )
    record_trace_events = bool(args.profile_json or profile_dir)
    try:
        profiler = CacheProfiler(
            enabled=True,
            record_spans=profiling_enabled,
            record_trace_events=record_trace_events,
            debug=args.debug,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    rc = None
    try:
        with use_profiler(profiler):
            with profiler.span('build_cache', kind='root'):
                try:
                    generate_distribution_caches(
                        args.index, dist_names=args.dist_names, preclean=args.preclean,
                        ignore_local=args.ignore_local, include_source=args.include_source,
                        debug=args.debug, limit=args.limit, config=config,
                        output_dir='.', strict=args.strict,
                        min_update_delta=args.min_update_delta, max_workers=args.jobs)
                except (RuntimeError, ValueError) as e:
                    print(str(e), file=sys.stderr)
                    rc = 1
                    return rc
    finally:
        if profiling_enabled:
            print(profiler.format_summary(), flush=True)
            wrote_trace = False
            if profile_dir:
                written = profiler.write_all(profile_dir)
                print('- wrote profiling artifacts to "%s":' % profile_dir, flush=True)
                for kind, path in sorted(written.items()):
                    print('  - %s: %s' % (kind, path), flush=True)
                wrote_trace = True
            if args.profile_folded:
                profiler.write_folded_stacks(args.profile_folded)
                print('- wrote folded stacks "%s"' % args.profile_folded, flush=True)
                wrote_trace = True
            if args.profile_json:
                profiler.write_json_report(args.profile_json)
                print('- wrote JSON profile trace "%s"' % args.profile_json, flush=True)
                wrote_trace = True
            if wrote_trace:
                print(
                    '- view flame graph/trace at https://www.speedscope.app/ or https://ui.perfetto.dev/ '
                    '(or render .folded with flamegraph.pl / inferno-flamegraph)',
                    flush=True,
                )
        profiler.shutdown()
    return rc
if __name__ == '__main__':
    sys.exit(main())
