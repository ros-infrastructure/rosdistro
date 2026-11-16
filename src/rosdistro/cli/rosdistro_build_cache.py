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
import gzip
import logging
import sys

from rosdistro import logger
from rosdistro.distribution_cache_generator import CacheYamlDumper, generate_distribution_caches

import yaml

logging.basicConfig(level=logging.INFO)


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
    return parser.parse_args(args)


def main(args=None):
    args = parse_args(args)

    if args.debug:
        logger.level = logging.DEBUG

    config = args.config if args.config is not None else ('extended' if args.extended else None)

    try:
        caches = generate_distribution_caches(
            args.index, dist_names=args.dist_names, preclean=args.preclean,
            ignore_local=args.ignore_local, include_source=args.include_source,
            debug=args.debug, limit=args.limit, config=config)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    for dist_name, cache in caches.items():
        if args.dist_names and dist_name not in args.dist_names:
            continue
        dumper = CacheYamlDumper.with_min_length(cache.config.yaml_alias_min_length)

        # Write the legacy Format 2 minimal cache
        legacy_data = yaml.dump(
            cache.get_legacy_data(include_source=args.include_source), Dumper=dumper
        ).encode()
        with open('%s-cache.yaml' % dist_name, 'wb') as f:
            print('- write cache file "%s-cache.yaml"' % dist_name)
            f.write(legacy_data)
        with gzip.open('%s-cache.yaml.gz' % dist_name, 'wb') as f:
            print('- write compressed cache file "%s-cache.yaml.gz"' % dist_name)
            f.write(legacy_data)

        # Write the extended Format 3 cache when --config or --extended is specified
        if config is not None:
            extended_data = yaml.dump(cache.get_data(version=3), Dumper=dumper).encode()
            with open('%s-extended-cache.yaml' % dist_name, 'wb') as f:
                print('- write extended cache file "%s-extended-cache.yaml"' % dist_name)
                f.write(extended_data)
            with gzip.open('%s-extended-cache.yaml.gz' % dist_name, 'wb') as f:
                print('- write compressed extended cache file "%s-extended-cache.yaml.gz"' % dist_name)
                f.write(extended_data)


if __name__ == '__main__':
    sys.exit(main())
