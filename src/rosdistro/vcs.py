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

from itertools import takewhile
import os
import re
import signal
import subprocess
import threading

from packaging.version import parse, InvalidVersion
try:
    from packaging.version import LegacyVersion
    packaging_lte_22 = True
except ImportError:
    packaging_lte_22 = False


def _version_gte(version: str, required_version: str) -> bool:
    """Check if a version string is greater than or equal to a required version.

    Args:
        version: The version string to check.
        required_version: The required version string.

    Returns:
        True if the version is greater than or equal to the required version, False otherwise.
    """
    try:
        parsed_version = parse(version)
        if packaging_lte_22:
            # In packaging 22.0 and earlier, parse() returns a LegacyVersion for non-standard version strings,
            # which will compare greater than any valid version. We want to raise an error instead.
            if isinstance(parsed_version, LegacyVersion):
                raise InvalidVersion
    except InvalidVersion:
        version = '.'.join(
            x for x in takewhile(str.isdigit, version.split('.'))
        )
        parsed_version = parse(version)

    return parsed_version >= parse(required_version)

from . import logger
from .cache_profiler import sanitize_url_for_trace, trace_operation

DEFAULT_GIT_TIMEOUT = 120.0
DEFAULT_GIT_LS_REMOTE_TIMEOUT = 60.0
DEFAULT_GIT_LOW_SPEED_LIMIT = 1000
DEFAULT_GIT_LOW_SPEED_TIME = 30


def get_git_timeout(subcommand=None):
    """Return the configured git subprocess timeout in seconds, or None if disabled."""
    if subcommand == 'ls-remote':
        raw = os.getenv('ROSDISTRO_GIT_LS_REMOTE_TIMEOUT')
        if raw is not None and raw != '':
            timeout = float(raw)
            return timeout if timeout > 0 else None
    raw = os.getenv('ROSDISTRO_GIT_TIMEOUT')
    if raw is not None and raw != '':
        timeout = float(raw)
        return timeout if timeout > 0 else None
    if subcommand == 'ls-remote':
        return DEFAULT_GIT_LS_REMOTE_TIMEOUT
    return DEFAULT_GIT_TIMEOUT


class Git(object):
    _client_executable = None
    _client_version = None
    _init_lock = threading.RLock()

    def __init__(self, cwd=None, timeout=None):
        self.cwd = cwd
        self.timeout = timeout
        if not self._client_executable:
            with self.__class__._init_lock:
                if not self.__class__._client_executable:
                    self.__class__._client_executable = _find_executable('git')

    def command(self, *args, timeout=None):
        assert self._client_executable is not None, "'git' not found"
        if timeout is not None:
            effective_timeout = timeout if timeout > 0 else None
        elif self.timeout is not None:
            effective_timeout = self.timeout if self.timeout > 0 else None
        else:
            effective_timeout = get_git_timeout(args[0] if args else None)
        subcmd = args[0] if args else 'unknown'
        target = None
        if len(args) > 1 and isinstance(args[1], str):
            target = sanitize_url_for_trace(args[1])
        with trace_operation('git:%s' % subcmd, target=target) as span:
            result = _run_command(
                (self._client_executable,) + args,
                self.cwd,
                env=non_interactive_git_env(),
                timeout=effective_timeout)
            if result['returncode'] != 0:
                span.status = 'timeout' if result.get('timed_out') else 'failed'
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
            with cls._init_lock:
                if not cls._client_version:
                    result = cls().command('--version')
                    cls._client_version = result['output'].split()[-1]
        return _version_gte(cls._client_version, version)


def non_interactive_git_env(base_env=None):
    env = (base_env if base_env is not None else os.environ).copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env.setdefault('GCM_INTERACTIVE', 'never')
    env['SSH_ASKPASS_REQUIRE'] = 'never'
    env.pop('SSH_ASKPASS', None)
    env.setdefault('GIT_HTTP_LOW_SPEED_LIMIT', str(DEFAULT_GIT_LOW_SPEED_LIMIT))
    env.setdefault('GIT_HTTP_LOW_SPEED_TIME', str(DEFAULT_GIT_LOW_SPEED_TIME))
    env.setdefault('GIT_LFS_SKIP_SMUDGE', '1')
    if 'GIT_SSH' not in env and 'GIT_SSH_COMMAND' not in env:
        env['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes -o ConnectTimeout=30'
    return env


def normalize_git_url(url):
    if url and url[:6].lower() == 'file:/' and url[:7].lower() != 'file://':
        return 'file://' + url[5:]
    return url


def ref_is_hash(ref):
    return bool(ref) and re.match('^[0-9a-f]{40}$', ref) is not None


def _kill_process_tree(proc):
    if os.name == 'nt':
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_command(cmd, cwd=None, env=None, timeout=None):
    result = {'cmd': ' '.join(cmd), 'cwd': cwd}
    popen_kwargs = {
        'cwd': cwd,
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'env': env,
    }
    if os.name != 'nt':
        popen_kwargs['start_new_session'] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            output, error = proc.communicate(timeout=timeout)
            result['output'] = output.rstrip()
            result['error'] = error.rstrip()
            result['returncode'] = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                output, error = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                output, error = b'', b''
            result['output'] = output.rstrip()
            err_text = error.rstrip()
            if not isinstance(err_text, str):
                err_text = err_text.decode('utf-8', errors='replace')
            timeout_msg = 'Command "%s" timed out after %g seconds' % (result['cmd'], timeout)
            result['error'] = '%s: %s' % (timeout_msg, err_text) if err_text else timeout_msg
            result['returncode'] = 124
            result['timed_out'] = True
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
