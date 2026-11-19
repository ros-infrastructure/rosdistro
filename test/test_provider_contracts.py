"""Offline contract tests pinning the observable behaviour of all manifest providers.

Every test in this file runs offline against module patch points so later
refactoring commits can verify exact compatibility. Known quirks that later
commits intentionally fix are annotated with ``# QUIRK(commit N)``.
"""

from contextlib import contextmanager
import io
import json
import socket
import tarfile
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from rosdistro.fetch_reporter import FetchReporter, use_reporter
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.git import (
    git_manifest_provider,
    git_source_manifest_provider,
)
import rosdistro.manifest_provider.github as github_mp
from rosdistro.manifest_provider.github import (
    github_manifest_provider,
    github_source_manifest_provider,
)
from rosdistro.manifest_provider.gitlab import (
    gitlab_manifest_provider,
    gitlab_source_manifest_provider,
)
from rosdistro.manifest_provider.tar import (
    tar_manifest_provider,
    tar_source_manifest_provider,
)
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification


def _pkg_xml(name, version='1.0.0'):
    return (
        '<?xml version="1.0"?>'
        '<package format="2">'
        f'<name>{name}</name>'
        f'<version>{version}</version>'
        f'<description>{name} package</description>'
        '<maintainer email="dev@example.com">Dev</maintainer>'
        '<license>BSD</license>'
        '</package>'
    )


def _mock_http_response(body, headers=None):
    headers = headers or {}
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
    resp.getheader.side_effect = lambda k, default=None: headers.get(k, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _build_tar_bytes(files_dict):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, content in files_dict.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.getvalue()


def _rel_repo(url, repo_type='git', tag_tmpl='release/rolling/{package}/{version}', version='1.0.0-1'):
    return ReleaseRepositorySpecification('my_repo', {
        'type': repo_type,
        'url': url,
        'version': version,
        'tags': {'release': tag_tmpl},
    })


def _src_repo(url, repo_type='git', version='main'):
    data = {'type': repo_type, 'url': url}
    if version is not None:
        data['version'] = version
    return SourceRepositorySpecification('my_repo', data)


# ---------------------------------------------------------------------------
# Release provider contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('provider,repo', [
    (github_manifest_provider, _rel_repo('https://gitlab.com/org/repo.git')),
    (github_manifest_provider, _rel_repo('https://github.com/org/repo.tar.gz', repo_type='tar')),
    (gitlab_manifest_provider, _rel_repo('https://github.com/org/repo.git')),
    (gitlab_manifest_provider, _rel_repo('https://gitlab.com/org/repo.tar.gz', repo_type='tar')),
    (bitbucket_manifest_provider, _rel_repo('https://github.com/org/repo.git')),
    (bitbucket_manifest_provider, _rel_repo('https://bitbucket.org/org/repo.tar.gz', repo_type='tar')),
    (git_manifest_provider, _rel_repo('https://example.com/repo.tar.gz', repo_type='tar')),
    (tar_manifest_provider, _rel_repo('https://github.com/org/repo.git', repo_type='git')),
])
def test_release_providers_skip_mismatched_host_or_type(provider, repo):
    with pytest.raises(SkipManifestProvider):
        provider('rolling', repo, 'pkg_a')


def test_release_provider_urls_quoting_and_optional_files(tmp_path):
    pkg_xml = _pkg_xml('pkg_a', '1.0.0')
    readme = '# pkg_a README\n'

    # GitHub: quotes special chars in tag, returns None for optional 404
    gh_repo = _rel_repo(
        'https://github.com/org/repo-release.git',
        tag_tmpl='release/rolling/{package}/v{version}+build#1',
    )
    gh_urls = []

    def gh_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        gh_urls.append(url)
        if url.endswith('/package.xml'):
            return _mock_http_response(pkg_xml)
        if url.endswith('/README.md'):
            return _mock_http_response(readme)
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    with patch.object(gh_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=gh_urlopen):
        assert github_manifest_provider('rolling', gh_repo, 'pkg_a') == pkg_xml
        assert github_manifest_provider('rolling', gh_repo, 'pkg_a', 'README.md') == readme
        assert github_manifest_provider('rolling', gh_repo, 'pkg_a', 'CHANGELOG.rst') is None
    assert gh_urls[0] == (
        'https://raw.githubusercontent.com/org/repo-release/'
        'release/rolling/pkg_a/v1.0.0-1%2Bbuild%231/package.xml'
    )

    # GitLab: URL-encodes project path and filepath, returns None for optional 404
    gl_repo = _rel_repo('https://gitlab.com/org/sub/repo-release.git')
    gl_urls = []

    def gl_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        gl_urls.append(url)
        if 'package.xml/raw' in url:
            return _mock_http_response(pkg_xml)
        if 'docs%2FREADME.md/raw' in url:
            return _mock_http_response(readme)
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=gl_urlopen):
        assert gitlab_manifest_provider('rolling', gl_repo, 'pkg_a') == pkg_xml
        assert gitlab_manifest_provider('rolling', gl_repo, 'pkg_a', 'docs/README.md') == readme
        assert gitlab_manifest_provider('rolling', gl_repo, 'pkg_a', 'CHANGELOG.rst') is None
    assert 'projects/org%2Fsub%2Frepo-release/repository/files/package.xml/raw' in gl_urls[0]
    assert 'ref=release%2Frolling%2Fpkg_a%2F1.0.0-1' in gl_urls[0]

    # Bitbucket: raw URL, returns None for optional 404
    bb_repo = _rel_repo('https://bitbucket.org/org/repo-release.git')
    bb_urls = []

    def bb_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        bb_urls.append(url)
        if url.endswith('/package.xml'):
            return _mock_http_response(pkg_xml)
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    with patch.object(bb_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.bitbucket.urlopen', side_effect=bb_urlopen):
        assert bitbucket_manifest_provider('rolling', bb_repo, 'pkg_a') == pkg_xml
        assert bitbucket_manifest_provider('rolling', bb_repo, 'pkg_a', 'README.md') is None
    assert bb_urls[0] == 'https://bitbucket.org/org/repo-release/raw/release/rolling/pkg_a/1.0.0-1/package.xml'

    # Git: clones once per release call, returns None for missing optional file
    clone_dir = tmp_path / 'git_rel'
    clone_dir.mkdir()
    (clone_dir / 'package.xml').write_text(pkg_xml)
    (clone_dir / 'README.md').write_text(readme)
    clone_calls = []

    @contextmanager
    def fake_clone(url, ref):
        clone_calls.append((url, ref))
        yield str(clone_dir)

    git_repo = _rel_repo('https://example.com/repo-release.git')
    with patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone):
        assert git_manifest_provider('rolling', git_repo, 'pkg_a') == pkg_xml
        assert git_manifest_provider('rolling', git_repo, 'pkg_a', 'README.md') == readme
        assert git_manifest_provider('rolling', git_repo, 'pkg_a', 'CHANGELOG.rst') is None
    assert len(clone_calls) == 3
    assert clone_calls[0] == ('https://example.com/repo-release.git', 'release/rolling/pkg_a/1.0.0-1')

    # Tar: downloads once per release call, returns None for missing optional file
    tar_bytes = _build_tar_bytes({
        'rel-pkg_a-1.0.0-1/package.xml': pkg_xml,
        'rel-pkg_a-1.0.0-1/README.md': readme,
    })
    tar_downloads = []

    def tar_urlopen(req, timeout=None):
        tar_downloads.append(req.get_full_url())
        return _mock_http_response(tar_bytes)

    tar_repo = _rel_repo(
        'https://example.com/repo.tar.gz',
        repo_type='tar',
        tag_tmpl='rel-{package}-{version}',
    )
    with patch('rosdistro.manifest_provider.tar.urlopen', side_effect=tar_urlopen):
        assert tar_manifest_provider('rolling', tar_repo, 'pkg_a') == pkg_xml
        assert tar_manifest_provider('rolling', tar_repo, 'pkg_a', 'README.md') == readme
        assert tar_manifest_provider('rolling', tar_repo, 'pkg_a', 'CHANGELOG.rst') is None
    assert len(tar_downloads) == 3


@pytest.mark.parametrize('provider,patch_target,https_url,ssh_url,needs_tag', [
    (
        github_manifest_provider,
        'rosdistro.manifest_provider.github.urlopen',
        'https://github.com/org/repo-release.git',
        'git@github.com:org/repo-release.git',
        True,
    ),
    (
        gitlab_manifest_provider,
        'rosdistro.manifest_provider.gitlab.urlopen',
        'https://gitlab.com/org/repo-release.git',
        'git@gitlab.com:org/repo-release.git',
        False,
    ),
    (
        bitbucket_manifest_provider,
        'rosdistro.manifest_provider.bitbucket.urlopen',
        'https://bitbucket.org/org/repo-release.git',
        'git@bitbucket.org:org/repo-release.git',
        True,
    ),
])
def test_release_http_error_classification(provider, patch_target, https_url, ssh_url, needs_tag):
    https_repo = _rel_repo(https_url)
    ssh_repo = _rel_repo(ssh_url)

    def _run(repo, err_factory):
        def fake_open(req, timeout=None):
            url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
            raise err_factory(url)

        with patch(patch_target, side_effect=fake_open), \
                patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 0):
            if needs_tag:
                with patch.object(repo, 'has_remote_tag', return_value=True):
                    return provider('rolling', repo, 'pkg_a')
            return provider('rolling', repo, 'pkg_a')

    # Definitive 404/410/451 on unauthenticated https -> PermanentManifestError
    for code in (404, 410, 451):
        with pytest.raises(PermanentManifestError):
            _run(https_repo, lambda u, c=code: HTTPError(u, c, 'Err', {}, io.BytesIO(b'')))

    # 404 on SSH URL must stay transient (not PermanentManifestError) so git fallback runs
    with pytest.raises(Exception) as exc_info:
        _run(ssh_repo, lambda u: HTTPError(u, 404, 'Not Found', {}, io.BytesIO(b'')))
    assert not isinstance(exc_info.value, PermanentManifestError)

    # 429, 500, and socket timeout must stay transient (not PermanentManifestError)
    for err_factory in (
        lambda u: HTTPError(u, 429, 'Too Many Requests', {'Retry-After': '60'}, io.BytesIO(b'')),
        lambda u: HTTPError(u, 500, 'Server Error', {}, io.BytesIO(b'')),
        lambda u: socket.timeout('timed out'),
    ):
        with pytest.raises(Exception) as exc_info:
            _run(https_repo, err_factory)
        assert not isinstance(exc_info.value, PermanentManifestError)

    # Missing remote tag on GitHub/Bitbucket -> PermanentManifestError
    if needs_tag:
        with patch.object(https_repo, 'has_remote_tag', return_value=False):
            with pytest.raises(PermanentManifestError):
                provider('rolling', https_repo, 'pkg_a')


def test_release_http_permanent_403_classification():
    gh_repo = _rel_repo('https://github.com/org/repo-release.git')
    gl_repo = _rel_repo('https://gitlab.com/org/repo-release.git')

    # GitHub non-rate-limit 403 -> PermanentManifestError
    def gh_perm_403(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        raise HTTPError(
            url, 403, 'Forbidden',
            {'X-RateLimit-Remaining': '4999'},
            io.BytesIO(b'{"message": "Repository access blocked"}'),
        )

    with patch.object(gh_repo, 'has_remote_tag', return_value=True), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=gh_perm_403), \
            patch.object(github_mp, 'ROSDISTRO_RETRY_MAX', 0):
        with pytest.raises(PermanentManifestError):
            github_manifest_provider('rolling', gh_repo, 'pkg_a')

    # GitLab non-rate-limit 403 -> PermanentManifestError
    def gl_perm_403(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        raise HTTPError(
            url, 403, 'Forbidden',
            {'RateLimit-Remaining': '99'},
            io.BytesIO(b'{"message": "403 Forbidden"}'),
        )

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=gl_perm_403):
        with pytest.raises(PermanentManifestError):
            gitlab_manifest_provider('rolling', gl_repo, 'pkg_a')


# ---------------------------------------------------------------------------
# Source provider contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('provider,repo', [
    (github_source_manifest_provider, _src_repo('https://gitlab.com/org/repo.git')),
    (github_source_manifest_provider, _src_repo('https://github.com/org/repo.tar.gz', repo_type='tar', version=None)),
    (gitlab_source_manifest_provider, _src_repo('https://github.com/org/repo.git')),
    (git_source_manifest_provider, _src_repo('https://example.com/repo.tar.gz', repo_type='tar', version=None)),
    (tar_source_manifest_provider, _src_repo('https://github.com/org/repo.git')),
])
def test_source_providers_skip_mismatched_host_or_type(provider, repo):
    with pytest.raises(SkipManifestProvider):
        provider(repo)


def test_gitlab_source_tar_repo_without_version_quirk():
    # QUIRK(commit 7): gitlab_source_manifest_provider currently runs `assert repo.version`
    # before checking repo.type != 'git', raising AssertionError instead of SkipManifestProvider.
    tar_repo = _src_repo('https://gitlab.com/org/repo.tar.gz', repo_type='tar', version=None)
    with pytest.raises(AssertionError):
        gitlab_source_manifest_provider(tar_repo)


def _make_sample_tree_files():
    long_doc = '\n'.join(f'Line {i}' for i in range(1, 121))
    return {
        'README.md': long_doc,
        'CONTRIBUTING.rst': long_doc,
        'pkg_a/package.xml': _pkg_xml('pkg_a', '1.2.3'),
        'pkg_a/README.md': '# pkg_a README\n',
        'pkg_a/msg/Alpha.msg': 'int32 a\n',
        'pkg_a/srv/Query.srv': '---\nbool ok\n',
        'pkg_a/action/Go.action': '---\n---\n',
        'pkg_a/launch/run.launch.py': '# launch\n',
        # Nested package inside pkg_a must be excluded
        'pkg_a/test/nested_pkg/package.xml': _pkg_xml('nested_pkg', '0.0.1'),
        'pkg_a/test/nested_pkg/msg/Nested.msg': 'int32 n\n',
        # Ignored packages
        'ign_catkin/CATKIN_IGNORE': '',
        'ign_catkin/package.xml': _pkg_xml('ign_catkin'),
        'ign_colcon/COLCON_IGNORE': '',
        'ign_colcon/package.xml': _pkg_xml('ign_colcon'),
        'ign_ament/AMENT_IGNORE': '',
        'ign_ament/package.xml': _pkg_xml('ign_ament'),
        'ign_rosindex/.rosindex_ignore': '',
        'ign_rosindex/package.xml': _pkg_xml('ign_rosindex'),
    }


def _extract_sub_fetch_pairs(console_output):
    """Parse ``[resource_type] ... via=fetch_type`` pairs emitted by FetchReporter."""
    pairs = set()
    for line in console_output.splitlines():
        line = line.strip()
        if not line.startswith('['):
            continue
        close_idx = line.find(']')
        if close_idx == -1:
            continue
        resource_type = line[1:close_idx]
        via_marker = ' via='
        via_idx = line.find(via_marker, close_idx)
        if via_idx == -1:
            continue
        rest = line[via_idx + len(via_marker):]
        fetch_type = rest.split(' ', 1)[0]
        pairs.add((resource_type, fetch_type))
    return pairs


def test_source_provider_contracts_discovery_metadata_assets_and_labels(tmp_path):
    files = _make_sample_tree_files()
    tree_sha = '1' * 40
    commit_sha = '2' * 40

    # 1. GitHub source
    gh_requested = []

    def gh_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        gh_requested.append(url)
        if '/git/trees/' in url:
            return _mock_http_response(json.dumps({
                'sha': tree_sha,
                'truncated': False,
                'tree': [{'path': p, 'type': 'blob'} for p in sorted(files)],
            }))
        if url.endswith('/repos/org/my_repo'):
            return _mock_http_response(json.dumps({
                'stargazers_count': 11,
                'description': 'GH desc',
                'topics': ['ros2'],
                'pushed_at': '2026-03-01T00:00:00Z',
            }))
        raw_prefix = f'https://raw.githubusercontent.com/org/my_repo/{tree_sha}/'
        if url.startswith(raw_prefix):
            rel = url[len(raw_prefix):]
            if rel in files:
                return _mock_http_response(files[rel])
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    gh_stream = io.StringIO()
    with use_reporter(FetchReporter(debug=True, console_stream=gh_stream)), \
            patch('rosdistro.manifest_provider.github.urlopen', side_effect=gh_urlopen):
        gh_cache = github_source_manifest_provider(_src_repo('https://github.com/org/my_repo.git'))

    # QUIRK(commit 13): github_source_manifest_provider currently stores the tree SHA as _ref
    assert gh_cache.ref() == tree_sha
    assert set(gh_cache.keys()) == {'pkg_a'}
    assert gh_cache['pkg_a']['package_path'] == 'pkg_a'
    assert gh_cache.msg_files('pkg_a') == ['msg/Alpha.msg']
    assert gh_cache.srv_files('pkg_a') == ['srv/Query.srv']
    assert gh_cache.action_files('pkg_a') == ['action/Go.action']
    assert gh_cache.launch_files('pkg_a') == ['launch/run.launch.py']
    assert gh_cache['pkg_a']['README.md'] == '# pkg_a README\n'
    assert 'CHANGELOG.rst' not in gh_cache['pkg_a']
    assert not any(u.endswith('/pkg_a/CHANGELOG.rst') for u in gh_requested)
    assert gh_cache.stars() == 11
    assert gh_cache.description() == 'GH desc'
    assert gh_cache.tags() == ['ros2']
    assert gh_cache.last_commit_time() == '2026-03-01T00:00:00Z'
    # QUIRK(commit 12): raw providers currently truncate root docs at 100 lines
    assert 'Truncated content at 100 of 120 lines' in gh_cache.readme()
    assert 'Truncated content at 100 of 120 lines' in gh_cache.contributing()

    gh_pairs = _extract_sub_fetch_pairs(gh_stream.getvalue())
    for expected in (
        'source:repo_tree', 'source:partition_tree', 'source:package.xml',
        'source:launch_files', 'source:msg_files', 'source:srv_files', 'source:action_files',
        'source:README.md', 'source:CHANGELOG.rst',
        'source:metadata:repo_info', 'source:metadata:readme', 'source:metadata:contributing',
    ):
        assert (expected, 'github') in gh_pairs

    # 2. GitLab source
    gl_requested = []

    def gl_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        gl_requested.append(url)
        if '/repository/commits?' in url:
            return _mock_http_response(json.dumps([{'id': commit_sha, 'committed_date': '2026-03-02T00:00:00Z'}]))
        if '/repository/tree?' in url:
            return _mock_http_response(json.dumps([{'path': p, 'type': 'blob'} for p in sorted(files)]))
        if url.endswith('/projects/org%2Fmy_repo/'):
            return _mock_http_response(json.dumps({
                'star_count': 7,
                'description': 'GL desc',
                'tag_list': ['ros2'],
                'last_activity_at': '2026-03-03T00:00:00Z',
            }))
        for path, content in files.items():
            encoded = path.replace('/', '%2F')
            if f'/repository/files/{encoded}/raw?' in url:
                return _mock_http_response(content)
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    gl_stream = io.StringIO()
    with use_reporter(FetchReporter(debug=True, console_stream=gl_stream)), \
            patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=gl_urlopen):
        gl_cache = gitlab_source_manifest_provider(_src_repo('https://gitlab.com/org/my_repo.git'))

    assert gl_cache.ref() == commit_sha
    assert set(gl_cache.keys()) == {'pkg_a'}
    assert gl_cache.msg_files('pkg_a') == ['msg/Alpha.msg']
    assert gl_cache.srv_files('pkg_a') == ['srv/Query.srv']
    assert gl_cache.action_files('pkg_a') == ['action/Go.action']
    assert gl_cache.launch_files('pkg_a') == ['launch/run.launch.py']
    assert 'CHANGELOG.rst' not in gl_cache['pkg_a']
    # QUIRK(commit 7): GitLab source currently requests optional files absent from the tree
    assert any('pkg_a%2FCHANGELOG.rst/raw' in u for u in gl_requested)
    assert gl_cache.stars() == 7
    assert gl_cache.description() == 'GL desc'
    assert gl_cache.tags() == ['ros2']
    assert gl_cache.last_commit_time() == '2026-03-02T00:00:00Z'
    # QUIRK(commit 12): raw providers currently truncate root docs at 100 lines
    assert 'Truncated content at 100 of 120 lines' in gl_cache.readme()
    assert 'Truncated content at 100 of 120 lines' in gl_cache.contributing()

    gl_pairs = _extract_sub_fetch_pairs(gl_stream.getvalue())
    for expected in (
        'source:repo_tree', 'source:partition_tree', 'source:package.xml',
        'source:launch_files', 'source:msg_files', 'source:srv_files', 'source:action_files',
        'source:README.md', 'source:CHANGELOG.rst',
        'source:metadata:repo_info', 'source:metadata:readme', 'source:metadata:contributing',
    ):
        assert (expected, 'gitlab') in gl_pairs

    # 3. Git source
    git_dir = tmp_path / 'git_src'
    for rel_path, content in files.items():
        full = git_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    @contextmanager
    def fake_clone(_url, _ref):
        yield str(git_dir)

    class FakeGit:
        def __init__(self, cwd=None):
            self.cwd = cwd

        def command(self, *args):
            if args == ('rev-parse', 'HEAD'):
                return {'returncode': 0, 'output': commit_sha}
            if args == ('log', '-1', '--format=%cI', 'HEAD'):
                return {'returncode': 0, 'output': '2026-03-04T00:00:00+00:00\n'}
            raise AssertionError(f'Unexpected git command: {args!r}')

    git_stream = io.StringIO()
    with use_reporter(FetchReporter(debug=True, console_stream=git_stream)), \
            patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone), \
            patch('rosdistro.manifest_provider.git.Git', FakeGit):
        git_cache = git_source_manifest_provider(_src_repo('https://example.com/my_repo.git'))

    assert git_cache.ref() == commit_sha
    assert set(git_cache.keys()) == {'pkg_a'}
    assert git_cache.msg_files('pkg_a') == ['msg/Alpha.msg']
    assert git_cache.srv_files('pkg_a') == ['srv/Query.srv']
    assert git_cache.action_files('pkg_a') == ['action/Go.action']
    assert git_cache.launch_files('pkg_a') == ['launch/run.launch.py']
    assert git_cache.last_commit_time() == '2026-03-04T00:00:00+00:00'
    # QUIRK(commit 12): raw providers currently truncate root docs at 100 lines
    assert 'Truncated content at 100 of 120 lines' in git_cache.readme()
    assert 'Truncated content at 100 of 120 lines' in git_cache.contributing()

    git_pairs = _extract_sub_fetch_pairs(git_stream.getvalue())
    for expected in (
        'source:repo_tree', 'source:partition_tree', 'source:package.xml',
        'source:launch_files', 'source:msg_files', 'source:srv_files', 'source:action_files',
        'source:README.md', 'source:CHANGELOG.rst',
        'source:metadata:last_commit_time', 'source:metadata:readme', 'source:metadata:contributing',
    ):
        assert (expected, 'git') in git_pairs

    # 4. Tar source
    tar_bytes = _build_tar_bytes(files)
    tar_stream = io.StringIO()
    with use_reporter(FetchReporter(debug=True, console_stream=tar_stream)), \
            patch('rosdistro.manifest_provider.tar.urlopen', return_value=_mock_http_response(tar_bytes)):
        tar_cache = tar_source_manifest_provider(
            _src_repo('https://example.com/my_repo.tar.gz', repo_type='tar', version=None)
        )

    assert tar_cache.ref() is None
    assert set(tar_cache.keys()) == {'pkg_a'}
    assert tar_cache.msg_files('pkg_a') == ['msg/Alpha.msg']
    assert tar_cache.srv_files('pkg_a') == ['srv/Query.srv']
    assert tar_cache.action_files('pkg_a') == ['action/Go.action']
    assert tar_cache.launch_files('pkg_a') == ['launch/run.launch.py']
    # QUIRK(commit 12): raw providers currently truncate root docs at 100 lines
    assert 'Truncated content at 100 of 120 lines' in tar_cache.readme()
    assert 'Truncated content at 100 of 120 lines' in tar_cache.contributing()

    tar_pairs = _extract_sub_fetch_pairs(tar_stream.getvalue())
    for expected in (
        'source:repo_tree', 'source:partition_tree', 'source:package.xml',
        'source:launch_files', 'source:msg_files', 'source:srv_files', 'source:action_files',
        'source:README.md', 'source:CHANGELOG.rst',
        'source:metadata:readme', 'source:metadata:contributing',
    ):
        assert (expected, 'tar') in tar_pairs


def test_source_http_error_classification():
    gh_https = _src_repo('https://github.com/org/my_repo.git')
    gh_ssh = _src_repo('git@github.com:org/my_repo.git')
    gl_https = _src_repo('https://gitlab.com/org/my_repo.git')
    gl_ssh = _src_repo('git@gitlab.com:org/my_repo.git')

    # GitHub: 404/410/451 on unauthenticated https -> PermanentManifestError; SSH 404 & truncated -> transient
    for code in (404, 410, 451):
        with patch('rosdistro.manifest_provider.github.urlopen',
                   side_effect=lambda r, timeout=None, c=code: (_ for _ in ()).throw(
                       HTTPError(r.get_full_url(), c, 'Err', {}, io.BytesIO(b''))
                   )):
            with pytest.raises(PermanentManifestError):
                github_source_manifest_provider(gh_https)

    with patch('rosdistro.manifest_provider.github.urlopen',
               side_effect=lambda r, timeout=None: (_ for _ in ()).throw(
                   HTTPError(r.get_full_url(), 404, 'Not Found', {}, io.BytesIO(b''))
               )):
        with pytest.raises(RuntimeError) as exc_info:
            github_source_manifest_provider(gh_ssh)
        assert not isinstance(exc_info.value, PermanentManifestError)

    with patch('rosdistro.manifest_provider.github.urlopen',
               return_value=_mock_http_response(json.dumps({'sha': 'a' * 40, 'truncated': True, 'tree': []}))):
        with pytest.raises(RuntimeError) as exc_info:
            github_source_manifest_provider(gh_https)
        assert not isinstance(exc_info.value, PermanentManifestError)

    # GitLab: 404/410/451 and empty commit list on unauthenticated https -> PermanentManifestError
    for code in (404, 410, 451):
        with patch('rosdistro.manifest_provider.gitlab.urlopen',
                   side_effect=lambda r, timeout=None, c=code: (_ for _ in ()).throw(
                       HTTPError(r.get_full_url(), c, 'Err', {}, io.BytesIO(b''))
                   )):
            with pytest.raises(PermanentManifestError):
                gitlab_source_manifest_provider(gl_https)

    with patch('rosdistro.manifest_provider.gitlab.urlopen',
               return_value=_mock_http_response('[]')):
        with pytest.raises(PermanentManifestError):
            gitlab_source_manifest_provider(gl_https)

    with patch('rosdistro.manifest_provider.gitlab.urlopen',
               side_effect=lambda r, timeout=None: (_ for _ in ()).throw(
                   HTTPError(r.get_full_url(), 404, 'Not Found', {}, io.BytesIO(b''))
               )):
        with pytest.raises(HTTPError) as exc_info:
            gitlab_source_manifest_provider(gl_ssh)
        assert not isinstance(exc_info.value, PermanentManifestError)


def test_gitlab_source_optional_file_transient_error_quirk():
    # QUIRK(commit 7): GitLab source currently swallows transient errors (e.g. 500/URLError)
    # on optional per-package files and omits the file instead of aborting so git fallback runs.
    commit_sha = '3' * 40

    def gl_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if '/repository/commits?' in url:
            return _mock_http_response(json.dumps([{'id': commit_sha}]))
        if '/repository/tree?' in url:
            return _mock_http_response(json.dumps([
                {'path': 'pkg_a/package.xml', 'type': 'blob'},
                {'path': 'pkg_a/README.md', 'type': 'blob'},
            ]))
        if 'pkg_a%2Fpackage.xml/raw' in url:
            return _mock_http_response(_pkg_xml('pkg_a'))
        if 'pkg_a%2FREADME.md/raw' in url:
            raise HTTPError(url, 500, 'Server Error', {}, io.BytesIO(b''))
        raise HTTPError(url, 404, 'Not Found', {}, io.BytesIO(b''))

    with patch('rosdistro.manifest_provider.gitlab.urlopen', side_effect=gl_urlopen):
        cache = gitlab_source_manifest_provider(
            _src_repo('https://gitlab.com/org/my_repo.git'),
            filepaths=('README.md',),
            metadata=(),
        )
    assert 'pkg_a' in cache
    assert 'README.md' not in cache['pkg_a']


def test_tar_source_dot_prefix_normalization_quirk():
    # QUIRK(commit 5): tar_source_manifest_provider currently uses `m.name.lstrip('./')`,
    # which strips the leading dot from `./.rosindex_ignore`, turning it into `rosindex_ignore`
    # so the root ignore marker fails to take effect.
    tar_bytes = _build_tar_bytes({
        './.rosindex_ignore': '',
        './pkg_a/package.xml': _pkg_xml('pkg_a'),
    })
    with patch('rosdistro.manifest_provider.tar.urlopen', return_value=_mock_http_response(tar_bytes)):
        cache = tar_source_manifest_provider(
            _src_repo('https://example.com/my_repo.tar.gz', repo_type='tar', version=None),
            filepaths=(),
            metadata=(),
        )
    assert set(cache.keys()) == {'pkg_a'}
