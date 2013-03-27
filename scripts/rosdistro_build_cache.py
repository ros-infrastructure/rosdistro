#!/usr/bin/env python

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
import yaml

from rosdistro import get_cached_release, get_release_file_data, get_index, get_release_cache, logger
from rosdistro.release_cache import ReleaseCache

logging.basicConfig(level=logging.INFO)

DEST_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'examples', 'convert'))


def parse_args(args=sys.argv[1:]):
    parser = argparse.ArgumentParser(
        description='Build the cache for rosdistro release distributions.'
    )
    add = parser.add_argument
    add('index_url',
        help='The url of the index file'
    )
    add('dist_names', nargs='*',
        help='The names of the distributions (default: all)'
    )
    add('--debug', action='store_true', default=False,
        help='Output debug messages'
    )
    add('--preclean', action='store_true', default=False,
        help='Build the cache from scratch instead of reusing cached data'
    )
    return parser.parse_args(args)


def main():
    args = parse_args()

    if args.debug:
        logger.level = logging.DEBUG

    # get index
    index = get_index(args.index_url)

    if not args.dist_names:
        args.dist_names = sorted(index.distributions.keys())

    errors = False
    for dist_name in args.dist_names:
        missing_package_xmls = []
        print('Build cache for "%s"' % dist_name)
        cache = None
        try:
            if not args.preclean:
                print('- trying to use local cache')
                if os.path.exists(dist_name + '-cache.yaml'):
                    with open(dist_name + '-cache.yaml', 'r') as f:
                        yaml_str = f.read()
                    data = yaml.load(yaml_str)
                    cache = ReleaseCache(dist_name, data)
                if not cache:
                    print('- trying to fetch cache')
                    # get release cache
                    cache = get_release_cache(index, dist_name)
                # get current release file
                rel_file_data = get_release_file_data(index, dist_name, 'release')
                # update cache with current release file
                cache.update_distribution(rel_file_data)
        except:
            print('- failed to fetch old cache')
        if cache:
            print('- update cache')
        else:
            print('- build cache from scratch')
            # get empty cache with release file
            rel_file_data = get_release_file_data(index, dist_name, 'release')
            cache = ReleaseCache(dist_name, rel_file_data=rel_file_data)

        # get distribution
        dist = get_cached_release(index, dist_name, cache=cache, allow_lazy_load=True)

        # fetch all manifests
        print('- fetch missing manifests')
        for pkg_name in sorted(dist.packages.keys()):
            repo = dist.repositories[dist.packages[pkg_name].repository_name]
            if repo.version is None:
                if args.debug:
                    print('  - skip "%s"' % pkg_name)
                continue
            if args.debug:
                print('  - fetch "%s"' % pkg_name)
            else:
                sys.stdout.write('.')
                sys.stdout.flush()
            package_xml = dist.get_package_xml(pkg_name)
            if not package_xml:
                missing_package_xmls.append(pkg_name)

        if not args.debug:
            print('')

        if missing_package_xmls:
            print('- missing package xml files: %s' % ', '.join(missing_package_xmls))
            errors = True
        else:
            # write the cache
            with open(dist_name + '-cache.yaml', 'w') as f:
                print('- write cache file "%s-cache.yaml"' % dist_name)
                yaml.dump(cache.get_data(), f)
    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
