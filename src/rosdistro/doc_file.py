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


class DocFile(object):

    _type = 'doc'

    def __init__(self, name, data):
        self.name = name

        assert 'type' in data and data['type'] == DocFile._type
        assert 'version' in data
        assert int(data['version']) == 1, "Unable to handle '%s' format version '%d', please update rosdistro" % (DocFile._type, int(data['version']))
        self.version = data['version']

        self.repositories = {}
        self.repository_dependencies = {}
        if 'repositories' in data:
            for repo_name in data['repositories']:
                repo_data = data['repositories'][repo_name]
                try:
                    repo = Repository(repo_name, repo_data)
                except AssertionError as e:
                    e.args = [("Doc file '%s': %s" % (self.name, a) if i == 0 else a) for i, a in enumerate(e.args)]
                    raise e
                self.repositories[repo_name] = repo
                self.repository_dependencies[repo_name] = []
                if 'depends' in repo_data:
                    for dep in repo_data['depends']:
                        self.repository_dependencies[repo_name].append(dep)
        for repo_name in self.repositories:
            for dep_name in self.repository_dependencies[repo_name]:
                assert dep_name in self.repositories

    def get_data(self):
        data = {}
        data['type'] = DocFile._type
        data['version'] = 1
        data['repositories'] = {}
        for repo_name in sorted(self.repositories):
            repo = self.repositories[repo_name]
            data['repositories'][repo_name] = repo.get_data()
            if self.repository_dependencies[repo_name]:
                data['repositories'][repo_name]['depends'] = self.repository_dependencies[repo_name]
        return data
