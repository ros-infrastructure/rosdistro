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
import types
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse


class Index(object):

    def __init__(self, data, base_url):
        assert data['type'] == 'index'
        self.version = int(data['version'])
        assert self.version == 1

        self.distributions = {}
        for distro_name in sorted(data['distributions']):
            self.distributions[distro_name] = {}
            distro_data = data['distributions'][distro_name]
            for key in distro_data:
                if key in ['release', 'release_cache', 'test', 'doc_folder']:
                    list_value = False
                elif key in ['release_build', 'test_build']:
                    list_value = True
                else:
                    assert False, 'unknown key "%s"' % key

                self.distributions[distro_name][key] = []
                value = distro_data[key]
                if list_value != isinstance(value, types.ListType):
                    assert False, 'wrong type of key "%s"' % key

                if not list_value:
                    value = [value]
                for v in value:
                    parts = urlparse(v)
                    if not parts[0]:  # schema
                        v = os.path.join(base_url, v)
                    self.distributions[distro_name][key].append(v)
                if not list_value:
                    self.distributions[distro_name][key] = self.distributions[distro_name][key][0]
