# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Open Source Robotics Foundation, Inc.
# Copyright (c) 2016, Clearpath Robotics
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
import re
import subprocess

from distutils.version import LooseVersion

from . import logger


class Git(object):
    _client_executable = None
    _client_version = None

    def __init__(self, cwd=None):
        self.cwd = cwd
        if not self._client_executable:
            self.__class__._client_executable = _find_executable('git')

    def command(self, *args):
        assert self._client_executable is not None, "'git' not found"
        result = _run_command(
            (self._client_executable,) + args,
            self.cwd,
            env=non_interactive_git_env())
        if result['returncode'] != 0:
            error_output = result.get('error') or result.get('output')
            if error_output:
                logger.debug(
                    'Git command "%s" failed with return code %s:\n%s' %
                    (result['cmd'], result['returncode'], error_output))
            else:
                logger.debug(
                    'Git command "%s" failed with return code %s' %
                    (result['cmd'], result['returncode']))
        return result

    @classmethod
    def version_gte(cls, version):
        if not cls._client_version:
            result = cls().command('--version')
            cls._client_version = result['output'].split()[-1]
        return LooseVersion(cls._client_version) >= LooseVersion(version)


def non_interactive_git_env(base_env=None):
    env = (base_env if base_env is not None else os.environ).copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env.setdefault('GCM_INTERACTIVE', 'never')
    env['SSH_ASKPASS_REQUIRE'] = 'never'
    env.pop('SSH_ASKPASS', None)
    return env


def ref_is_hash(ref):
    return re.match('^[0-9a-f]{40}$', ref) is not None


def _run_command(cmd, cwd=None, env=None):
    result = {'cmd': ' '.join(cmd), 'cwd': cwd}
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        output, error = proc.communicate()
        result['output'] = output.rstrip()
        result['error'] = error.rstrip()
        result['returncode'] = proc.returncode
    except subprocess.CalledProcessError as e:
        result['output'] = e.output
        result['error'] = getattr(e, 'stderr', '') or ''
        result['returncode'] = e.returncode
    if not isinstance(result['output'], str):
        result['output'] = result['output'].decode('utf-8', errors='replace')
    if not isinstance(result['error'], str):
        result['error'] = result['error'].decode('utf-8', errors='replace')
    return result


def _find_executable(file_name):
    pathext = ['']
    if os.name == 'nt':
        # https://docs.microsoft.com/en-us/windows-server/administration/windows-commands/start#remarks
        # mimic the behavior how CMD.exe searching for a command without the extension specified.
        pathext = pathext + os.getenv('PATHEXT').split(os.path.pathsep)
    for path in os.getenv('PATH').split(os.path.pathsep):
        for ext in pathext:
            file_path = os.path.join(path, file_name + ext)
            if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                return file_path
    return None
