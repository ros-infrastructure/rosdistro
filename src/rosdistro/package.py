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

from .status import valid_statuses


class Package:

    def __init__(self, name, repository_name, data=None, unary_repo=False):
        data = data if data is not None else {}
        self.name = name
        self.repository_name = repository_name
        self.subfolder = data.get('subfolder', '.' if unary_repo else name)
        self.status = data.get('status', None)
        if self.status is not None:
            assert self.status in valid_statuses, "Package '%s' has invalid status '%s', valid statuses are: " % (self.name, self.status, ', '.join(valid_statuses))
        self.status_description = data.get('status_description', None)

    def get_data(self, unary_repo=False, repo_status=None, repo_status_description=None):
        data = {}
        # suppress subfolder if it:
        # - is not a unary and the subfolder equals package name or
        # - is a unary and the subfolder is '.'
        if (not unary_repo and self.subfolder != self.name) or (unary_repo and self.subfolder != '.'):
            data['subfolder'] = str(self.subfolder)
        if self.status is not None and self.status != repo_status:
            assert self.status in valid_statuses, "Package '%s' has invalid status '%s', valid statuses are: " % (self.name, self.status, ', '.join(valid_statuses))
            data['status'] = str(self.status)
        if self.status_description is not None and self.status_description != repo_status_description:
            data['status_description'] = str(self.status_description)
        return data
