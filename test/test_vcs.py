import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import os
import subprocess
import tempfile
import threading
from unittest.mock import patch

from rosdistro import logger
from rosdistro.common import rmtree
from rosdistro.distribution import Distribution
from rosdistro.distribution_cache import DistributionCache
from rosdistro.manifest_provider.cache import CachedManifestProvider, CachedSourceManifestProvider
from rosdistro.manifest_provider.git import (
    _temp_git_clone,
    git_manifest_provider,
    git_source_manifest_provider,
)
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.repository_specification import RepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification
from rosdistro.vcs import Git, non_interactive_git_env


class _AuthChallengeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        auth = self.headers.get('Authorization', '')
        expected = 'Basic ' + base64.b64encode(b'validuser:validpass').decode()
        if auth == expected:
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-git-upload-pack-advertisement')
            self.end_headers()
            self.wfile.write(b'001e# service=git-upload-pack\n00000000')
            return
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="GitHub"')
        self.end_headers()
        self.wfile.write(b'remote: Repository not found.\n')

    def log_message(self, format, *args):
        pass


class _ListLogHandler(logging.Handler):

    def __init__(self, level=logging.WARNING):
        super(_ListLogHandler, self).__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _capture_logs(level=logging.WARNING):
    handler = _ListLogHandler(level=level)
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@contextmanager
def _auth_challenge_git_server(extra_gitconfig=''):
    server = HTTPServer(('127.0.0.1', 0), _AuthChallengeHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    temp_home = tempfile.mkdtemp('rosdistro_test_home')
    gitconfig_path = os.path.join(temp_home, '.gitconfig')
    with open(gitconfig_path, 'w') as f:
        f.write('[url "http://127.0.0.1:%d/"]\n' % port)
        f.write('\tinsteadOf = https://github.com/\n')
        if extra_gitconfig:
            f.write(extra_gitconfig)

    env_overrides = {
        'HOME': temp_home,
        'XDG_CONFIG_HOME': temp_home,
        'GIT_CONFIG_NOSYSTEM': '1',
        'SSH_ASKPASS': '/bin/sleep',
        'DISPLAY': ':0',
        'no_proxy': '127.0.0.1,localhost',
        'NO_PROXY': '127.0.0.1,localhost',
    }
    try:
        with patch.dict(os.environ, env_overrides, clear=False):
            os.environ.pop('GIT_ASKPASS', None)
            yield port
    finally:
        server.shutdown()
        server.server_close()
        rmtree(temp_home)


def test_non_interactive_git_env():
    base_env = {
        'PATH': '/usr/bin',
        'SSH_ASKPASS': '/usr/bin/ssh-askpass',
    }
    env = non_interactive_git_env(base_env)
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['GCM_INTERACTIVE'] == 'never'
    assert env['SSH_ASKPASS_REQUIRE'] == 'never'
    assert 'SSH_ASKPASS' not in env


def test_git_command_does_not_prompt_on_credential_challenge():
    with _auth_challenge_git_server() as port:
        url = 'http://127.0.0.1:%d/owner/private-repo.git' % port
        with _capture_logs(level=logging.DEBUG) as records:
            result = Git().command('ls-remote', url)

        assert result['returncode'] != 0
        assert result['error']
        assert any(url in r.getMessage() for r in records)


def test_git_command_allows_non_interactive_credential_helper():
    helper_cfg = (
        '[credential]\n'
        '\thelper = "!f() { echo username=validuser; echo password=validpass; }; f"\n'
    )
    with _auth_challenge_git_server(extra_gitconfig=helper_cfg) as port:
        url = 'http://127.0.0.1:%d/owner/private-repo.git' % port
        result = Git().command('ls-remote', url)
        assert result['returncode'] == 0


def test_repository_specification_remote_refs_auth_failure():
    with _auth_challenge_git_server() as port:
        url = 'http://127.0.0.1:%d/owner/private-repo.git' % port
        repo = RepositorySpecification('private_repo', {'url': url})
        try:
            _ = repo.remote_refs
            assert False, 'Expected RuntimeError'
        except RuntimeError as e:
            assert url in str(e)
            assert 'Could not git ls-remote repository' in str(e)
            assert len(str(e)) > len('Could not git ls-remote repository "%s"' % url)


def test_cached_manifest_provider_logs_private_repo_failure():
    with _auth_challenge_git_server():
        dist_file_data = {
            'type': 'distribution',
            'version': 2,
            'repositories': {
                'fmi_adapter': {
                    'release': {
                        'url': 'https://github.com/boschresearch/fmi_adapter_ros2-release.git',
                        'version': '2.1.2-1',
                        'tags': {'release': 'release/rolling/{package}/{version}'},
                    }
                }
            }
        }
        cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
        provider = CachedManifestProvider(cache, Distribution.default_manifest_providers)
        repo = ReleaseRepositorySpecification('fmi_adapter', {
            'url': 'https://github.com/boschresearch/fmi_adapter_ros2-release.git',
            'version': '2.1.2-1',
            'tags': {'release': 'release/rolling/{package}/{version}'},
        })

        with _capture_logs() as records:
            pkg_xml = provider('rolling', repo, 'fmi_adapter')

        assert pkg_xml is None
        error_msgs = [r.getMessage() for r in records if r.levelno == logging.ERROR]
        assert len(error_msgs) == 1
        assert 'Failed to fetch package.xml for package "fmi_adapter"' in error_msgs[0]
        assert 'fmi_adapter_ros2-release.git' in error_msgs[0]
        assert 'github_manifest_provider()' in error_msgs[0]
        assert 'git_manifest_provider()' in error_msgs[0]
        assert 'gitlab_manifest_provider()' not in error_msgs[0]
        assert 'tar_manifest_provider()' not in error_msgs[0]


def test_cached_source_manifest_provider_logs_private_repo_failure():
    with _auth_challenge_git_server() as port:
        url = 'http://127.0.0.1:%d/boschresearch/fmi_adapter_ros2.git' % port
        dist_file_data = {
            'type': 'distribution',
            'version': 2,
            'repositories': {
                'fmi_adapter': {
                    'source': {
                        'type': 'git',
                        'url': url,
                        'version': 'main',
                    }
                }
            }
        }
        cache = DistributionCache('rolling', distribution_file_data=dist_file_data)
        provider = CachedSourceManifestProvider(cache, Distribution.default_source_manifest_providers)
        repo = SourceRepositorySpecification('fmi_adapter', {
            'type': 'git',
            'url': url,
            'version': 'main',
        })

        with _capture_logs() as records:
            repo_cache = provider(repo)

        assert repo_cache is None
        error_msgs = [r.getMessage() for r in records if r.levelno == logging.ERROR]
        assert len(error_msgs) == 1
        assert 'Failed to fetch source package.xml files for repo "fmi_adapter"' in error_msgs[0]
        assert url in error_msgs[0]
        assert 'git_source_manifest_provider()' in error_msgs[0]


def test_distribution_cache_update_logs_private_source_repo():
    with _auth_challenge_git_server() as port:
        url = 'http://127.0.0.1:%d/boschresearch/fmi_adapter_ros2.git' % port
        dist_file_data = [{
            'type': 'distribution',
            'version': 2,
            'repositories': {
                'fmi_adapter': {
                    'source': {
                        'type': 'git',
                        'url': url,
                        'version': 'main',
                    }
                }
            }
        }]
        cache_data = {
            'type': 'cache',
            'version': 2,
            'name': 'rolling',
            'distribution_file': dist_file_data,
            'release_package_xmls': {},
            'source_repo_package_xmls': {
                'fmi_adapter': {
                    '_ref': '0123456789abcdef0123456789abcdef01234567',
                    'fmi_adapter': ['', '<package><name>fmi_adapter</name></package>'],
                }
            }
        }
        cache = DistributionCache('rolling', data=cache_data)
        with _capture_logs() as records:
            cache.update_distribution(dist_file_data)

        assert 'fmi_adapter' not in cache.source_repo_package_xmls
        warning_msgs = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        assert len(warning_msgs) == 1
        assert 'Unable to check hash for branch main of %s' % url in warning_msgs[0]


def test_tar_repo_does_not_invoke_git_providers():
    dist_file_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {}
    }
    cache = DistributionCache('melodic', distribution_file_data=dist_file_data)
    repo = ReleaseRepositorySpecification('genmsg', {
        'url': 'https://github.com/ros-gbp/genmsg-release/archive/release/melodic/genmsg/0.5.16-1.tar.gz',
        'tags': {'release': '{package}-release-release-melodic-{package}-{version}'},
        'version': '0.5.16-1',
        'type': 'tar',
    })
    fake_xml = '<package><name>genmsg</name><version>0.5.16</version></package>'
    with patch('rosdistro.manifest_provider.tar.tar_manifest_provider', return_value=fake_xml) as mock_tar, \
            patch.object(Git, 'command') as mock_git_cmd:
        providers = Distribution.default_manifest_providers[:-1] + [mock_tar]
        provider = CachedManifestProvider(cache, providers)
        with _capture_logs() as records:
            pkg_xml = provider('melodic', repo, 'genmsg')

        assert '<name>genmsg</name>' in pkg_xml
        mock_git_cmd.assert_not_called()
        assert records == []


def _make_pkg_xml(name, version='1.0.0', desc='Package description'):
    return (
        '<?xml version="1.0"?>\n'
        '<package format="2">\n'
        '  <name>%s</name>\n'
        '  <version>%s</version>\n'
        '  <description>%s</description>\n'
        '  <maintainer email="maintainer@example.com">Maintainer</maintainer>\n'
        '  <license>BSD</license>\n'
        '</package>\n'
    ) % (name, version, desc)


def _git_run(cwd, *args, **kwargs):
    env = non_interactive_git_env()
    extra = kwargs.pop('extra_env', None)
    if extra:
        env.update(extra)
    return subprocess.run(
        ['git'] + list(args),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )


def test_temp_git_clone_shallow_blobless_and_hash():
    if not Git.version_gte('2.19.0'):
        return
    tmp = tempfile.mkdtemp(prefix='rosdistro_blobless_')
    try:
        work = os.path.join(tmp, 'work')
        upstream = os.path.join(tmp, 'upstream.git')
        os.makedirs(work)
        _git_run(work, 'init', '.')
        _git_run(work, 'checkout', '-b', 'main')
        _git_run(work, 'config', 'user.email', 'test@example.com')
        _git_run(work, 'config', 'user.name', 'Test')

        with open(os.path.join(work, 'package.xml'), 'w') as f:
            f.write(_make_pkg_xml('my_pkg', '0.1.0'))
        with open(os.path.join(work, 'old.bin'), 'wb') as f:
            f.write(b'x' * 4096)
        _git_run(work, 'add', '.')
        _git_run(work, 'commit', '-m', 'c1')
        c1_sha = _git_run(work, 'rev-parse', 'HEAD').stdout.decode().strip()

        os.remove(os.path.join(work, 'old.bin'))
        with open(os.path.join(work, 'package.xml'), 'w') as f:
            f.write(_make_pkg_xml('my_pkg', '1.0.0'))
        _git_run(work, 'add', '-A')
        _git_run(work, 'commit', '-m', 'c2')
        c2_sha = _git_run(work, 'rev-parse', 'HEAD').stdout.decode().strip()

        _git_run(tmp, 'clone', '--bare', work, upstream)
        _git_run(upstream, '--git-dir=.', 'config', 'uploadpack.allowfilter', 'true')
        url = 'file://' + upstream

        orig_cmd = Git.command
        calls = []

        def spy_cmd(self, *args):
            calls.append(args)
            return orig_cmd(self, *args)

        with patch.object(Git, 'command', spy_cmd):
            repo = SourceRepositorySpecification('my_pkg', {'url': url, 'version': 'main'})
            cache = git_source_manifest_provider(repo)
            assert cache.ref() == c2_sha
            assert '<version>1.0.0</version>' in cache['my_pkg']['package.xml']
            assert any('--filter=blob:none' in c and '--depth' in c for c in calls)

            calls.clear()
            hash_repo = SourceRepositorySpecification('my_pkg', {'url': url, 'version': c1_sha})
            hash_cache = git_source_manifest_provider(hash_repo)
            assert hash_cache.ref() == c1_sha
            assert '<version>0.1.0</version>' in hash_cache['my_pkg']['package.xml']
            assert any('--filter=blob:none' in c and '--no-checkout' in c for c in calls)
    finally:
        rmtree(tmp)


def test_temp_git_clone_fallback_when_blobless_fails():
    tmp = tempfile.mkdtemp(prefix='rosdistro_fallback_')
    try:
        work = os.path.join(tmp, 'work')
        upstream = os.path.join(tmp, 'upstream.git')
        os.makedirs(work)
        _git_run(work, 'init', '.')
        _git_run(work, 'checkout', '-b', 'main')
        _git_run(work, 'config', 'user.email', 'test@example.com')
        _git_run(work, 'config', 'user.name', 'Test')

        with open(os.path.join(work, 'package.xml'), 'w') as f:
            f.write(_make_pkg_xml('my_pkg', '1.0.0'))
        _git_run(work, 'add', '.')
        _git_run(work, 'commit', '-m', 'c1')
        c1_sha = _git_run(work, 'rev-parse', 'HEAD').stdout.decode().strip()
        _git_run(work, 'tag', 'release/rolling/my_pkg/1.0.0-1')
        _git_run(tmp, 'clone', '--bare', work, upstream)
        url = 'file://' + upstream

        orig_cmd = Git.command
        clone_calls = []

        def fail_blobless(self, *args):
            if args and args[0] == 'clone':
                clone_calls.append(args)
                if '--filter=blob:none' in args:
                    # Simulate a partial/failed blobless clone leaving files in the temp dir
                    with open(os.path.join(self.cwd, 'stale_file'), 'w') as f:
                        f.write('stale')
                    return {'cmd': 'git clone', 'cwd': self.cwd, 'output': '', 'error': 'filter unsupported', 'returncode': 128}
            return orig_cmd(self, *args)

        rel_repo = ReleaseRepositorySpecification('my_pkg', {
            'tags': {'release': 'release/rolling/{package}/{version}'},
            'url': url,
            'version': '1.0.0-1',
        })
        with patch.object(Git, 'command', fail_blobless):
            xml = git_manifest_provider('rolling', rel_repo, 'my_pkg')
            assert '<version>1.0.0</version>' in xml
            assert len(clone_calls) == 2
            assert '--filter=blob:none' in clone_calls[0]
            assert '--filter=blob:none' not in clone_calls[1]
            assert '--depth' in clone_calls[1]

            clone_calls.clear()
            with _temp_git_clone(url, c1_sha) as repo_path:
                assert os.path.exists(os.path.join(repo_path, 'package.xml'))
                assert not os.path.exists(os.path.join(repo_path, 'stale_file'))
            assert len(clone_calls) == 2
            assert '--filter=blob:none' in clone_calls[0]
            assert clone_calls[1] == ('clone', url, '.')
    finally:
        rmtree(tmp)
