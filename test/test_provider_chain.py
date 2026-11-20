from functools import partial
from unittest.mock import MagicMock

import pytest

from rosdistro.manifest_provider import PermanentManifestError, SkipManifestProvider
from rosdistro.manifest_provider.cache import CachedManifestProvider, CachedSourceManifestProvider
from rosdistro.manifest_provider.chain import (
    ProviderAttempt,
    REJECT,
    call_release_provider,
    call_source_provider,
    format_attempts,
    run_provider_chain,
)
from rosdistro.release_repository_specification import ReleaseRepositorySpecification
from rosdistro.source_repository_specification import SourceRepositorySpecification


def test_run_provider_chain_skip_fail_success_order():
    calls = []

    def p_skip():
        calls.append('skip')
        raise SkipManifestProvider('wrong host')

    def p_fail():
        calls.append('fail')
        raise RuntimeError('transient')

    def p_ok():
        calls.append('ok')
        return 'payload'

    def p_never():
        calls.append('never')
        return 'unreachable'

    outcome = run_provider_chain(
        [p_skip, p_fail, p_ok, p_never],
        lambda mp: mp(),
        target='pkg',
        dist_name='rolling',
    )
    assert outcome.succeeded is True
    assert outcome.result == 'payload'
    assert outcome.provider_name == 'p_ok'
    assert outcome.permanent is False
    assert calls == ['skip', 'fail', 'ok']
    assert [(a.mp_name, a.skipped) for a in outcome.attempts] == [
        ('p_skip', True),
        ('p_fail', False),
    ]


def test_run_provider_chain_permanent_short_circuit():
    calls = []

    def p_perm():
        calls.append('perm')
        raise PermanentManifestError('404 gone')

    def p_fallback():
        calls.append('fallback')
        return 'should not run'

    outcome = run_provider_chain(
        [p_perm, p_fallback],
        lambda mp: mp(),
        target='pkg',
    )
    assert outcome.succeeded is False
    assert outcome.permanent is True
    assert calls == ['perm']
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].skipped is False


def test_run_provider_chain_accept_reject_and_raise():
    calls = []

    def p_none():
        calls.append('none')
        return None

    def p_bad_xml():
        calls.append('bad_xml')
        return 'not xml'

    def p_good():
        calls.append('good')
        return 'good'

    def accept(raw):
        if raw is None:
            return REJECT
        if raw == 'not xml':
            raise ValueError('bad xml')
        return raw.upper()

    outcome = run_provider_chain(
        [p_none, p_bad_xml, p_good],
        lambda mp: mp(),
        target='pkg',
        accept=accept,
    )
    assert outcome.succeeded is True
    assert outcome.result == 'GOOD'
    assert outcome.provider_name == 'p_good'
    assert calls == ['none', 'bad_xml', 'good']
    # REJECT does not add an error; raised accept error does
    assert [(a.mp_name, str(a.error), a.skipped) for a in outcome.attempts] == [
        ('p_bad_xml', 'bad xml', False),
    ]


def test_format_attempts_prefers_non_skipped():
    attempts = (
        ProviderAttempt('gitlab_manifest_provider', SkipManifestProvider('not gitlab'), True),
        ProviderAttempt('github_manifest_provider', RuntimeError('500 error'), False),
        ProviderAttempt('git_manifest_provider', RuntimeError('clone failed'), False),
    )
    multi = format_attempts(attempts, multiline=True)
    assert 'gitlab_manifest_provider' not in multi
    assert '  - github_manifest_provider(): 500 error' in multi
    assert '  - git_manifest_provider(): clone failed' in multi

    inline = format_attempts(attempts, multiline=False)
    assert inline == 'github_manifest_provider(): 500 error; git_manifest_provider(): clone failed'

    only_skipped = (
        ProviderAttempt('gitlab_manifest_provider', SkipManifestProvider('not gitlab'), True),
    )
    assert format_attempts(only_skipped, multiline=False) == 'gitlab_manifest_provider(): not gitlab'
    assert format_attempts((), multiline=True) == ''


def test_call_release_provider_adapters():
    def mp3(dist_name, repo, pkg_name):
        return (3, dist_name, repo, pkg_name)

    def mp4(dist_name, repo, pkg_name, filepath='package.xml'):
        return (4, dist_name, repo, pkg_name, filepath)

    def mp_extra(prefix, dist_name, repo, pkg_name, filepath='package.xml'):
        return (prefix, dist_name, repo, pkg_name, filepath)

    mock_mp = MagicMock(return_value='mocked')

    assert call_release_provider(mp3, 'rolling', 'R', 'pkg') == (3, 'rolling', 'R', 'pkg')
    assert call_release_provider(mp3, 'rolling', 'R', 'pkg', 'README.md') is None
    assert call_release_provider(mp4, 'rolling', 'R', 'pkg', 'README.md') == (
        4, 'rolling', 'R', 'pkg', 'README.md'
    )
    assert call_release_provider(partial(mp_extra, 'P'), 'rolling', 'R', 'pkg', 'README.md') == (
        'P', 'rolling', 'R', 'pkg', 'README.md'
    )
    assert call_release_provider(mock_mp, 'rolling', 'R', 'pkg', 'README.md') == 'mocked'
    mock_mp.assert_called_once_with('rolling', 'R', 'pkg', 'README.md')


def test_call_source_provider_adapters_and_no_double_call_on_internal_typeerror():
    def mp1(repo):
        return (1, repo)

    def mp2(repo, filepaths=None):
        return (2, repo, filepaths)

    def mp3(repo, filepaths=None, metadata=None):
        return (3, repo, filepaths, metadata)

    def mp_extra(prefix, repo, filepaths=None, metadata=None):
        return (prefix, repo, filepaths, metadata)

    mock_mp = MagicMock(return_value='mocked')

    assert call_source_provider(mp1, 'R', filepaths=['README.md'], metadata=['stars']) == (1, 'R')
    assert call_source_provider(mp2, 'R', filepaths=['README.md'], metadata=['stars']) == (
        2, 'R', ['README.md']
    )
    assert call_source_provider(mp3, 'R', filepaths=['README.md'], metadata=['stars']) == (
        3, 'R', ['README.md'], ['stars']
    )
    assert call_source_provider(
        partial(mp_extra, 'P'), 'R', filepaths=['README.md'], metadata=['stars']
    ) == ('P', 'R', ['README.md'], ['stars'])
    assert call_source_provider(mock_mp, 'R', filepaths=['README.md'], metadata=['stars']) == 'mocked'
    mock_mp.assert_called_once_with('R', filepaths=['README.md'], metadata=['stars'])

    # Internal TypeError inside a 3-arg provider must NOT re-invoke the provider
    invocations = []

    def mp3_buggy(repo, filepaths=None, metadata=None):
        invocations.append((repo, filepaths, metadata))
        raise TypeError('internal bug')

    with pytest.raises(TypeError, match='internal bug'):
        call_source_provider(mp3_buggy, 'R', filepaths=['README.md'], metadata=['stars'])
    assert len(invocations) == 1


def test_cached_source_provider_does_not_reinvoke_on_internal_typeerror():
    class FakeCache:
        def __init__(self):
            self.source_repo_resources = {}
            self.release_resources = {}

    invocations = []

    def mp3_buggy(repo, filepaths=None, metadata=None):
        invocations.append(repo.name)
        raise TypeError('internal bug')

    repo = SourceRepositorySpecification('r', {'url': 'https://example.com/r.git', 'version': 'main'})
    cached = CachedSourceManifestProvider(FakeCache(), [mp3_buggy])
    assert cached(repo) is None
    assert invocations == ['r']


def test_legacy_release_cache_handles_provider_returning_none():
    class LegacyReleaseCache:
        def __init__(self):
            self.release_package_xmls = {}

    repo = ReleaseRepositorySpecification('pkg', {
        'url': 'https://example.com/pkg-release.git',
        'version': '1.0.0-1',
        'tags': {'release': 'release/rolling/{package}/{version}'},
    })
    calls = []

    def mp_none(dist_name, repo, pkg_name):
        calls.append('none')
        return None

    def mp_ok(dist_name, repo, pkg_name):
        calls.append('ok')
        return '<package><name>pkg</name></package>'

    cache = LegacyReleaseCache()
    cached = CachedManifestProvider(cache, [mp_none, mp_ok])
    xml = cached('rolling', repo, 'pkg')
    assert '<name>pkg</name>' in xml
    assert calls == ['none', 'ok']

    # All providers returning None returns None cleanly instead of crashing sanitize_xml(None)
    cache2 = LegacyReleaseCache()
    cached2 = CachedManifestProvider(cache2, [mp_none])
    assert cached2('rolling', repo, 'pkg') is None
