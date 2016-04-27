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
import shutil
import tempfile

from rosdistro import logger
from rosdistro.vcs import Git


workspace_base = '/tmp/rosdistro-workspace'


def git_manifest_provider(_dist_name, repo, pkg_name):
    assert repo.version
    try:
        release_tag = repo.get_release_tag(pkg_name)
        package_xml = _get_package_xml(repo.url, release_tag)
        return package_xml
    except Exception as e:
        raise RuntimeError('Unable to fetch package.xml: %s' % e)


def _get_package_xml(url, tag):
    base = tempfile.mkdtemp('rosdistro')
    try:
        git = Git(base)
        if git.version_gte('1.8.0'):
            # Directly clone the required tag with least amount of additional history. This behaviour
            # has been available since git 1.8.0:
            # https://git.kernel.org/cgit/git/git.git/tree/Documentation/git-clone.txt?h=v1.8.0#n158
            result = git.command('clone', url, base, '--depth', '1', '--branch', tag)
            if result['returncode'] != 0:
                raise RuntimeError('Could not clone repository "%s" at tag "%s"' % (url, tag))
        else:
            # Old git doesn't support cloning a tag directly, so check it out after a full clone.
            git = Git(base)
            result = git.command('clone', url, base)
            if result['returncode'] != 0:
                raise RuntimeError('Could not clone repository "%s"' % url)

            result = git.command('tag', '-l')
            if result['returncode'] != 0:
                raise RuntimeError('Could not get tags of repository "%s"' % url)

            if tag not in result['output'].splitlines():
                raise RuntimeError('Specified tag "%s" is not a git tag of repository "%s"' % (tag, url))

            result = git.command('checkout', tag)
            if result['returncode'] != 0:
                raise RuntimeError('Could not checkout tag "%s" of repository "%s"' % (tag, url))

        filename = os.path.join(base, 'package.xml')
        if not os.path.exists(filename):
            raise RuntimeError('Could not find package.xml in repository "%s"' % url)
        with open(filename, 'r') as f:
            return f.read()
    finally:
        shutil.rmtree(base)
