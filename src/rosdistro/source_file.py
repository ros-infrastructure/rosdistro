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

from .repository import Repository


class SourceFile(object):

    _type = 'source'

    def __init__(self, name, data):
        self.name = name

        assert 'type' in data, "Expected file type is '%s'" % SourceFile._type
        assert data['type'] == SourceFile._type, "Expected file type is '%s', not '%s'" % (SourceFile._type, data['type'])

        assert 'version' in data, "Source file for '%s' lacks required version information" % self.name
        assert int(data['version']) == 1, "Unable to handle '%s' format version '%d', please update rosdistro" % (SourceFile._type, int(data['version']))
        self.version = int(data['version'])

        self.repositories = {}
        if 'repositories' in data:
            for repo_name in data['repositories']:
                repo_data = data['repositories'][repo_name]
                try:
                    repo = Repository(repo_name, repo_data)
                except AssertionError as e:
                    e.args = [("Source file '%s': %s" % (self.name, a) if i == 0 else a) for i, a in enumerate(e.args)]
                    raise e
                self.repositories[repo_name] = repo

    def get_data(self):
        data = {}
        data['type'] = SourceFile._type
        data['version'] = self.version
        data['repositories'] = {}
        for repo_name in sorted(self.repositories):
            repo = self.repositories[repo_name]
            data['repositories'][repo_name] = repo.get_data()
        return data
