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

import difflib
import yaml

from . import get_index, get_release_file, get_test_file
from .loader import load_url


def verify_files_parsable(index_url):
    index = get_index(index_url)
    for dist_name in sorted(index.distributions):
        if dist_name not in ['groovy']:
            continue
        dist = index.distributions[dist_name]
        if 'release' in dist:
            url = dist['release']
            yaml_str = load_url(url)
            dist_file = get_release_file(index, dist_name)
            if yaml_str != _to_yaml(dist_file.get_data()):
                diff = difflib.unified_diff(
                    yaml_str.splitlines(), _to_yaml(dist_file.get_data()).splitlines(),
                    'release.org', 'release.load-and-save',
                    n=0, lineterm='')
                for line in diff:
                    print(line)
                assert False
        if 'test' in dist:
            url = dist['test']
            yaml_str = load_url(url)
            dist_file = get_test_file(index, dist_name)
            if yaml_str != _to_yaml(dist_file.get_data()):
                diff = difflib.unified_diff(
                    yaml_str.splitlines(), _to_yaml(dist_file.get_data()).splitlines(),
                    'test.org', 'test.load-and-save',
                    n=0, lineterm='')
                for line in diff:
                    print(line)
                assert False


def _to_yaml(data):
    yaml_str = yaml.dump(data, default_flow_style=False)
    yaml_str = yaml_str.replace(': null', ':')
    yaml_str = yaml_str.replace(': {}', ':')
    return yaml_str
