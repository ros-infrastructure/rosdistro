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


class BuildFile(object):

    def __init__(self, name, data):
        assert data['type'] == 'build'
        self.version = int(data['version'])
        assert self.version == 1

        self.name = name

        self.package_whitelist = []
        if 'package_whitelist' in data:
            self.package_whitelist = data['package_whitelist']
            assert isinstance(self.package_whitelist, list)
        self.package_blacklist = []
        if 'package_blacklist' in data:
            self.package_blacklist = data['package_blacklist']
            assert isinstance(self.package_blacklist, list)

        self.notify_emails = []
        self.notify_maintainers = False
        self.notift_committers = False
        if 'notifications' in data:
            if 'emails' in data['notifications']:
                self.notify_emails = data['notifications']['emails']
                assert isinstance(self.notify_emails, list)
            if 'maintainers' in data['notifications'] and data['notifications']['maintainers']:
                self.notify_maintainers = True
            if 'committers' in data['notifications'] and data['notifications']['committers']:
                self.notift_committers = True

        assert 'targets' in data
        assert data['targets']
        self.targets = {}
        for platform_name in data['targets']:
            platform_name = str(platform_name)
            assert platform_name not in self.targets
            self.targets[platform_name] = []
            for arch_name in data['targets'][platform_name]:
                self.targets[platform_name].append(arch_name)

        assert 'jenkins_url' in data
        self.jenkins_url = str(data['jenkins_url'])
        self.apt_mirrors = []
        if 'apt_mirrors' in data:
            self.apt_mirrors = data['apt_mirrors']
            assert isinstance(self.apt_mirrors, list)
        self.apt_target_repository = None
        if 'apt_target_repository' in data:
            self.apt_target_repository = str(data['apt_target_repository'])
            if not self.apt_mirrors:
                self.apt_mirrors.append(self.apt_target_repository)
        assert self.apt_mirrors

        self.sync_package_count = None
        self.sync_packages = []
        if 'sync' in data:
            if 'package_count' in data['sync']:
                self.sync_package_count = int(data['sync']['package_count'])
            if 'packages' in data['sync']:
                self.notify_maintainers = data['sync']['packages']
                assert isinstance(self.sync_packages, list)

    def get_data(self):
        data = {}
        data['type'] = 'build'
        data['version'] = 1
        if self.package_whitelist:
            assert isinstance(self.notify_emails, list)
            data['package_whitelist'] = self.package_whitelist
        data['package_blacklist'] = self.package_blacklist

        if self.notify_emails or self.notify_maintainers or self.notift_committers:
            data['notifications'] = {}
            if self.notify_emails:
                assert isinstance(self.notify_emails, list)
                data['notifications']['emails'] = self.notify_emails
            if self.notify_maintainers is not None:
                data['notifications']['maintainers'] = bool(self.notify_maintainers)
            if self.notift_committers is not None:
                data['notifications']['committers'] = bool(self.notift_committers)

        data['targets'] = {}
        for platform_name in self.targets:
            assert isinstance(self.targets[platform_name], list)
            data['targets'][platform_name] = self.targets[platform_name]

        data['jenkins_url'] = self.jenkins_url
        if self.apt_mirrors:
            data['apt_mirrors'] = []
            if len(self.apt_mirrors) != 1 or self.apt_mirrors[0] != self.apt_target_repository:
                for mirror in self.apt_mirrors:
                    data['apt_mirrors'].append(mirror)
        if self.apt_target_repository:
            data['apt_target_repository'] = self.apt_target_repository

        return data
