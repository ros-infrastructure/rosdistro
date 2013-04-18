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


class SourceBuildFile(object):

    _type = 'source-build'

    def __init__(self, name, data):
        assert 'type' in data and data['type'] == SourceBuildFile._type
        assert 'version' in data and int(data['version']) == 1
        self.version = data['version']

        self.name = name

        self.repository_whitelist = []
        if 'repository_whitelist' in data:
            self.repository_whitelist = data['repository_whitelist']
            assert isinstance(self.repository_whitelist, list)
        self.repository_blacklist = []
        if 'repository_blacklist' in data:
            self.repository_blacklist = data['repository_blacklist']
            assert isinstance(self.repository_blacklist, list)

        self.notify_emails = []
        self.notify_maintainers = None
        self.notify_committers = None
        if 'notifications' in data:
            if 'emails' in data['notifications']:
                self.notify_emails = data['notifications']['emails']
                assert isinstance(self.notify_emails, list)
            if 'maintainers' in data['notifications'] and data['notifications']['maintainers']:
                self.notify_maintainers = True
            if 'committers' in data['notifications'] and data['notifications']['committers']:
                self.notify_committers = True

        assert 'targets' in data
        self.targets = {}
        for platform_name in data['targets'].keys():
            platform_name = str(platform_name)
            assert platform_name not in self.targets
            self.targets[platform_name] = []
            for arch_name in data['targets'][platform_name]:
                self.targets[platform_name].append(arch_name)

        assert 'jenkins_url' in data
        self.jenkins_url = str(data['jenkins_url'])
        self.jenkins_job_timeout = None
        if 'jenkins_job_timeout' in data:
            self.jenkins_job_timeout = int(data['jenkins_job_timeout'])

        assert 'apt_mirrors' in data
        self.apt_mirrors = data['apt_mirrors']
        assert isinstance(self.apt_mirrors, list)

    def get_data(self):
        data = {}
        data['type'] = SourceBuildFile._type
        data['version'] = 1
        if self.repository_whitelist:
            data['repository_whitelist'] = self.repository_whitelist
        if self.repository_blacklist:
            data['repository_blacklist'] = self.repository_blacklist

        if self.notify_emails or self.notify_maintainers or self.notify_committers:
            data['notifications'] = {}
            if self.notify_emails:
                data['notifications']['emails'] = self.notify_emails
            if self.notify_maintainers is not None:
                data['notifications']['maintainers'] = bool(self.notify_maintainers)
            if self.notify_committers is not None:
                data['notifications']['committers'] = bool(self.notify_committers)

        data['targets'] = {}
        for platform_name in self.targets:
            data['targets'][platform_name] = self.targets[platform_name]

        data['jenkins_url'] = self.jenkins_url
        if self.jenkins_job_timeout:
            data['jenkins_job_timeout'] = self.jenkins_job_timeout

        data['apt_mirrors'] = self.apt_mirrors

        return data
