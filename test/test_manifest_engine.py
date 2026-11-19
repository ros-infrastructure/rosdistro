import io
from unittest.mock import MagicMock

import pytest

from rosdistro.fetch_reporter import FetchReporter, use_reporter
from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.engine import (
    ManifestProvider,
    ManifestQuery,
    ManifestSession,
    execute_query,
    fetch_release_file,
    fetch_source_cache,
    provider_for,
)
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification


def _pkg_xml(name, version='1.0.0'):
    return (
        '<?xml version="1.0"?>'
        '<package format="2">'
        f'<name>{name}</name>'
        f'<version>{version}</version>'
        f'<description>{name}</description>'
        '<maintainer email="dev@example.com">Dev</maintainer>'
        '<license>BSD</license>'
        '</package>'
    )


class DictSession(ManifestSession):
    """In-memory session backing ``DictProvider`` for unit tests."""

    def __init__(self, provider, query):
        self._provider = provider
        self._query = query
        self.ref = provider.resolved_ref
        self.closed = False

    def list_files(self):
        self._provider.events.append(('list_files', self._query.repo.name, self._query.ref))
        if self._provider.list_error is not None:
            raise self._provider.list_error
        return sorted(self._provider.files.keys())

    def read_file(self, path):
        self._provider.events.append(('read_file', self._query.repo.name, path))
        if path in self._provider.file_errors:
            raise self._provider.file_errors[path]
        return self._provider.files.get(path)

    def fetch_metadata(self, keys):
        self._provider.events.append(('fetch_metadata', self._query.repo.name, keys))
        if keys is None:
            return dict(self._provider.metadata)
        return {k: v for k, v in self._provider.metadata.items() if k in keys}

    def close(self):
        self.closed = True
        self._provider.events.append(('close', self._query.repo.name))


class DictProvider(ManifestProvider):
    """In-memory ManifestProvider test double (reused across engine/batch/unified tests)."""

    name = 'dict'

    def __init__(
        self,
        files=None,
        resolved_ref='a' * 40,
        metadata=None,
        accept_predicate=None,
        permanent_missing=False,
        permanent_error_types=(),
        list_error=None,
        file_errors=None,
    ):
        self.files = dict(files or {})
        self.resolved_ref = resolved_ref
        self.metadata = dict(metadata or {})
        self.accept_predicate = accept_predicate
        self.permanent_missing = permanent_missing
        self.permanent_error_types = tuple(permanent_error_types)
        self.list_error = list_error
        self.file_errors = dict(file_errors or {})
        self.events = []
        self.sessions = []

    def accepts(self, repo):
        if self.accept_predicate is not None and not self.accept_predicate(repo):
            raise SkipManifestProvider('rejected by predicate')

    def open(self, query):
        self.events.append(('open', query.repo.name, query.ref, query.discover))
        session = DictSession(self, query)
        self.sessions.append(session)
        return session

    def is_permanent_error(self, err, repo):
        return isinstance(err, self.permanent_error_types)

    def missing_is_permanent(self, repo):
        return self.permanent_missing


def _rel_repo(repo_type='git', url='https://example.com/repo-release.git', tag='release/rolling/{package}/{version}'):
    return ReleaseRepositorySpecification('my_repo', {
        'type': repo_type,
        'url': url,
        'version': '1.0.0-1',
        'tags': {'release': tag},
    })


def _src_repo(url='https://example.com/repo.git', version='main'):
    return SourceRepositorySpecification('my_repo', {
        'type': 'git',
        'url': url,
        'version': version,
    })


def test_manifest_query_profiles_and_properties():
    git_rel = _rel_repo(repo_type='git')
    q_git_rel = ManifestQuery.for_release(git_rel, 'pkg_a', ('package.xml', 'README.md'))
    assert q_git_rel.ref == 'release/rolling/pkg_a/1.0.0-1'
    assert q_git_rel.ref_is_tag is True
    assert q_git_rel.subpath == ''
    assert q_git_rel.package_name == 'pkg_a'
    assert q_git_rel.discover is False
    assert q_git_rel.needs_tree is False
    assert q_git_rel.required == frozenset(('package.xml',))
    assert q_git_rel.has_metadata_request is False

    tar_rel = _rel_repo(repo_type='tar', url='https://example.com/r.tar.gz', tag='rel-{package}-{version}')
    q_tar_rel = ManifestQuery.for_release(tar_rel, 'pkg_a', ('README.md',))
    assert q_tar_rel.ref is None
    assert q_tar_rel.ref_is_tag is False
    assert q_tar_rel.subpath == 'rel-pkg_a-1.0.0-1'
    assert q_tar_rel.required == frozenset()

    src = _src_repo()
    q_src = ManifestQuery.for_source(src)
    assert q_src.ref == 'main'
    assert q_src.ref_is_tag is False
    assert q_src.discover is True
    assert q_src.needs_tree is True
    assert q_src.required == frozenset(('package.xml',))
    assert q_src.has_metadata_request is True
    assert q_src.wants_metadata('stars') is True


def test_engine_tree_query_discovery_assets_metadata_and_not_in_tree():
    files = {
        'README.md': '# Root README\n',
        'pkg_a/package.xml': _pkg_xml('pkg_a', '1.0.0'),
        'pkg_a/README.md': '# pkg_a README\n',
        'pkg_a/msg/Alpha.msg': 'int32 a\n',
        'pkg_a/srv/Query.srv': '---\nbool ok\n',
        'pkg_a/action/Go.action': '---\n---\n',
        'pkg_a/launch/start.launch.py': '# launch\n',
        'pkg_a/test/nested/package.xml': _pkg_xml('nested'),
        'ignored/CATKIN_IGNORE': '',
        'ignored/package.xml': _pkg_xml('ignored'),
    }
    provider = DictProvider(
        files=files,
        resolved_ref='b' * 40,
        metadata={'stars': 42, 'description': 'Desc', 'tags': ['ros2']},
    )
    stream = io.StringIO()
    with use_reporter(FetchReporter(debug=True, console_stream=stream)):
        cache = fetch_source_cache(provider, _src_repo())

    assert cache.ref() == 'b' * 40
    assert set(cache.keys()) == {'pkg_a'}
    assert cache.stars() == 42
    assert cache.description() == 'Desc'
    assert cache.tags() == ['ros2']
    assert cache.readme() == '# Root README'
    assert cache.contributing() is None
    assert cache['pkg_a']['README.md'] == '# pkg_a README\n'
    assert 'CHANGELOG.rst' not in cache['pkg_a']
    assert cache.msg_files('pkg_a') == ['msg/Alpha.msg']
    assert cache.srv_files('pkg_a') == ['srv/Query.srv']
    assert cache.action_files('pkg_a') == ['action/Go.action']
    assert cache.launch_files('pkg_a') == ['launch/start.launch.py']
    # Session closed and absent files were never passed to read_file()
    assert provider.sessions[0].closed is True
    read_paths = [e[2] for e in provider.events if e[0] == 'read_file']
    assert 'pkg_a/CHANGELOG.rst' not in read_paths
    assert 'CONTRIBUTING.md' not in read_paths
    assert 'source:repo_tree' in stream.getvalue()
    assert 'source:partition_tree' in stream.getvalue()


def test_engine_direct_query_required_optional_and_error_classification():
    files = {
        'package.xml': _pkg_xml('pkg_a'),
        'README.md': '# Release README\n',
    }
    provider = DictProvider(
        files=files,
        permanent_missing=True,
        permanent_error_types=(PermissionError,),
    )
    repo = _rel_repo()

    # Single query fetches all files in one session and records None for missing optional file
    query = ManifestQuery.for_release(repo, 'pkg_a', ('README.md', 'package.xml', 'CHANGELOG.rst'))
    result = execute_query(provider, query)
    assert result.file('package.xml', 'pkg_a') == files['package.xml']
    assert result.file('README.md', 'pkg_a') == files['README.md']
    assert result.file('CHANGELOG.rst', 'pkg_a') is None
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    # Required package.xml was read BEFORE optional files
    read_paths = [e[2] for e in provider.events if e[0] == 'read_file']
    assert read_paths[0] == 'package.xml'

    # Missing required package.xml raises PermanentManifestError when missing_is_permanent=True
    missing_perm = DictProvider(files={}, permanent_missing=True)
    with pytest.raises(PermanentManifestError):
        fetch_release_file(missing_perm, repo, 'pkg_a', 'package.xml')

    # Missing required package.xml raises transient RuntimeError when missing_is_permanent=False
    missing_transient = DictProvider(files={}, permanent_missing=False)
    with pytest.raises(RuntimeError) as exc_info:
        fetch_release_file(missing_transient, repo, 'pkg_a', 'package.xml')
    assert not isinstance(exc_info.value, PermanentManifestError)

    # Permanent error on required file -> PermanentManifestError; optional error recorded in result.errors
    err_provider = DictProvider(
        files={'package.xml': _pkg_xml('pkg_a')},
        file_errors={'README.md': RuntimeError('500')},
    )
    res = execute_query(err_provider, ManifestQuery.for_release(repo, 'pkg_a', ('package.xml', 'README.md')))
    assert res.file('package.xml', 'pkg_a') == _pkg_xml('pkg_a')
    assert 'README.md' in res.errors
    with pytest.raises(RuntimeError, match='500'):
        res.file('README.md', 'pkg_a')

    # Permanent error on required file stops before reading optional files
    perm_req = DictProvider(
        files={'README.md': '# unused'},
        permanent_error_types=(PermissionError,),
        file_errors={'package.xml': PermissionError('403 blocked')},
    )
    with pytest.raises(PermanentManifestError):
        execute_query(perm_req, ManifestQuery.for_release(repo, 'pkg_a', ('README.md', 'package.xml')))
    assert [e[2] for e in perm_req.events if e[0] == 'read_file'] == ['package.xml']


def test_engine_skips_before_opening_session_and_provider_for_ignores_magicmock():
    provider = DictProvider(accept_predicate=lambda _repo: False)
    with pytest.raises(SkipManifestProvider):
        fetch_source_cache(provider, _src_repo())
    assert provider.events == []
    assert provider.sessions == []

    # provider_for() uses isinstance only, never fooled by MagicMock dynamic attributes
    assert provider_for(provider) is provider
    assert provider_for(MagicMock()) is None

    def legacy_fn(repo):
        return None

    assert provider_for(legacy_fn) is None
    legacy_fn._unified_provider = provider
    assert provider_for(legacy_fn) is provider
    legacy_fn._unified_provider = MagicMock()
    assert provider_for(legacy_fn) is None
