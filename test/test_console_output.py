import datetime
from unittest.mock import patch

import yaml

from rosdistro import (
    CacheConfig,
    DistributionCache,
    FetchReporter,
    format_fetch_event,
    get_provider_name,
    use_reporter,
)
from rosdistro.cli.rosdistro_build_cache import main as build_cache_main
from rosdistro.distribution_cache_generator import generate_distribution_caches
from rosdistro.fetch_reporter import format_duration
from rosdistro.manifest_provider import SkipManifestProvider
from rosdistro.manifest_provider.bitbucket import bitbucket_manifest_provider
from rosdistro.manifest_provider.git import (
    git_manifest_provider,
    git_source_manifest_provider,
)
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
from rosdistro.source_repository_cache import SourceRepositoryCache


def _pkg_xml(name, version='1.0.0'):
    return (
        f'<?xml version="1.0"?>'
        f'<package format="2">'
        f'<name>{name}</name>'
        f'<version>{version}</version>'
        f'<description>{name} package</description>'
        f'<maintainer email="dev@example.com">Dev</maintainer>'
        f'<license>BSD</license>'
        f'</package>'
    )


def _write_index_and_distros(tmp_path, distros_dict):
    index_data = {
        'type': 'index',
        'version': 4,
        'distributions': {},
    }
    for dist_name, repos in distros_dict.items():
        dist_dir = tmp_path / dist_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / 'distribution.yaml').write_text(yaml.safe_dump({
            'type': 'distribution',
            'version': 2,
            'repositories': repos,
        }))
        index_data['distributions'][dist_name] = {
            'distribution': [f'{dist_name}/distribution.yaml'],
            'distribution_status': 'active',
            'distribution_type': 'ros2',
            'python_version': 3,
        }
    index_path = tmp_path / 'index-v4.yaml'
    index_path.write_text(yaml.safe_dump(index_data))
    return str(index_path)


def test_fetch_reporter_formatting_helpers():
    assert get_provider_name(github_manifest_provider) == 'github'
    assert get_provider_name(github_source_manifest_provider) == 'github'
    assert get_provider_name(gitlab_manifest_provider) == 'gitlab'
    assert get_provider_name(gitlab_source_manifest_provider) == 'gitlab'
    assert get_provider_name(bitbucket_manifest_provider) == 'bitbucket'
    assert get_provider_name(git_manifest_provider) == 'git'
    assert get_provider_name(git_source_manifest_provider) == 'git'
    assert get_provider_name(tar_manifest_provider) == 'tar'
    assert get_provider_name(tar_source_manifest_provider) == 'tar'
    assert get_provider_name('custom') == 'custom'
    assert get_provider_name(None) == 'unknown'

    def my_custom_manifest_provider():
        pass

    assert get_provider_name(my_custom_manifest_provider) == 'my_custom'

    assert format_duration(0.1234) == '0.123s'
    assert format_duration(75.5) == '1m 15.50s'
    assert format_duration(3725.2) == '1h 02m 05.2s'

    msg = format_fetch_event(
        'release:package.xml', 'my_pkg', 'github',
        status='ok', duration_s=0.125, dist_name='rolling', detail='version=1.0.0-1',
    )
    assert msg == '  [rolling] [release:package.xml] my_pkg via=github (ok, 0.125s, version=1.0.0-1)'


def test_homogenized_console_output_across_datatypes_and_modes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    sha = 'a' * 40

    repos_a = {
        'shared_repo': {
            'release': {
                'packages': ['shared_pkg'],
                'tags': {'release': 'release/shared/{package}/{version}'},
                'url': 'https://github.com/org/shared-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/shared.git',
                'version': 'main',
            },
        },
        'release_only_repo': {
            'release': {
                'packages': ['rel_only_pkg'],
                'tags': {'release': 'release/rel_only/{package}/{version}'},
                'url': 'https://gitlab.com/org/rel-only-release.git',
                'version': '2.0.0-1',
            },
        },
        'failing_source_repo': {
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/broken.git',
                'version': 'main',
            },
        },
    }
    repos_b = {'shared_repo': repos_a['shared_repo']}
    index_path = _write_index_and_distros(tmp_path, {'distro_a': repos_a, 'distro_b': repos_b})

    def gh_rel(_dist_name, repo, pkg_name, filepath='package.xml'):
        if 'github.com' not in repo.url:
            raise SkipManifestProvider('not github')
        if filepath == 'package.xml':
            return _pkg_xml(pkg_name, '1.0.0')
        if filepath == 'README.md':
            return '# Shared README\n'
        return None

    gh_rel.__name__ = 'github_manifest_provider'

    def gl_rel(_dist_name, repo, pkg_name, filepath='package.xml'):
        if 'gitlab.com' not in repo.url:
            raise SkipManifestProvider('not gitlab')
        if filepath == 'package.xml':
            return _pkg_xml(pkg_name, '2.0.0')
        return '# GitLab doc\n'

    gl_rel.__name__ = 'gitlab_manifest_provider'

    def gh_src(repo, filepaths=None, metadata=None):
        if 'broken' in repo.url:
            raise RuntimeError('404 Not Found')
        rc = SourceRepositoryCache.from_ref(sha)
        rc.set_stars(42)
        rc.set_description('Shared repo')
        rc.set_tags(['ros2'])
        rc.set_last_commit_time('2026-04-01T12:00:00Z')
        rc.set_contributing('# Contributing')
        rc.set_readme('# Root README')
        rc.add('shared_pkg', 'shared_pkg', _pkg_xml('shared_pkg', '1.0.0'), 'package.xml')
        rc.add('shared_pkg', 'shared_pkg', '# Shared README', 'README.md')
        rc.add('shared_pkg', 'shared_pkg', '1.0.0', 'CHANGELOG.rst')
        rc.add('shared_pkg', 'shared_pkg', ['launch/run.launch.py'], 'launch_files')
        rc.add('shared_pkg', 'shared_pkg', ['msg/A.msg'], 'msg_files')
        rc.add('shared_pkg', 'shared_pkg', [], 'srv_files')
        rc.add('shared_pkg', 'shared_pkg', [], 'action_files')
        return rc

    gh_src.__name__ = 'github_source_manifest_provider'

    with patch('rosdistro.Distribution.default_manifest_providers', [gh_rel, gl_rel]), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [gh_src]):
        caches = generate_distribution_caches(
            index_path, dist_names=['distro_a', 'distro_b'],
            preclean=False, config='extended', debug=False,
        )

    out = capsys.readouterr().out
    assert '[distro_a] [release:package.xml] shared_pkg via=github (ok,' in out
    assert '[distro_a] [release:README.md] shared_pkg via=github (ok,' in out
    assert '[distro_a] [release:CHANGELOG.rst] shared_pkg via=github (not_found,' in out
    assert '[distro_a] [release:package.xml] rel_only_pkg via=gitlab (ok,' in out
    assert '[distro_a] [source:repo] shared_repo via=github (ok,' in out
    assert '[distro_a] [source:repo] failing_source_repo via=failed (failed,' in out
    assert '[distro_b] [release:package.xml] shared_pkg via=local_neighbor (ok,' in out
    assert 'neighbor=distro_a' in out
    assert '[distro_b] [release:README.md] shared_pkg via=local_neighbor (ok,' in out
    assert '[distro_b] [release:CHANGELOG.rst] shared_pkg via=local_neighbor (not_found,' in out
    assert '[distro_b] [source:repo] shared_repo via=local_neighbor (ok,' in out
    assert 'via=skipped' not in out
    assert 'updated manifest of package' not in out
    assert 'dist cache fetch' not in out
    assert 'dist cache source fetch' not in out
    assert 'release fetch summary [distro_a]' in out
    assert 'source fetch summary [distro_b]' in out

    # Persist distro_a cache to disk and rebuild with debug=True -> cache hits & skipped entries reported
    (tmp_path / 'distro_a-extended-cache.yaml').write_text(
        yaml.safe_dump(caches['distro_a'].get_data(version=3))
    )
    with patch('rosdistro.Distribution.default_manifest_providers', [gh_rel, gl_rel]), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [gh_src]):
        generate_distribution_caches(
            index_path, dist_names=['distro_a'],
            preclean=False, config='extended', debug=True,
        )

    warm_out = capsys.readouterr().out
    assert '[distro_a] [cache:load] distro_a via=local_file (ok,' in warm_out
    assert '[distro_a] [cache:validate_source_ref] shared_repo via=ttl_skip (kept,' in warm_out
    assert '[distro_a] [release:package.xml] shared_pkg via=cache (ok,' in warm_out
    assert '[distro_a] [release:README.md] shared_pkg via=cache (ok,' in warm_out
    assert '[distro_a] [release:CHANGELOG.rst] shared_pkg via=cache (not_found,' in warm_out
    assert '[distro_a] [source:repo] shared_repo via=cache (ok,' in warm_out
    assert '[distro_a] [source:repo] failing_source_repo via=cached_failure (skipped,' in warm_out
    assert '[distro_a] [source:repo] release_only_repo via=skipped (skipped,' in warm_out


def test_custom_resource_uses_generic_reporting_without_custom_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repos = {
        'custom_repo': {
            'release': {
                'packages': ['custom_pkg'],
                'tags': {'release': 'release/custom/{package}/{version}'},
                'url': 'https://github.com/org/custom-release.git',
                'version': '1.0.0-1',
            },
            'source': {
                'type': 'git',
                'url': 'https://github.com/org/custom.git',
                'version': 'main',
            },
        },
    }
    index_path = _write_index_and_distros(tmp_path, {'rolling': repos})
    cfg_path = tmp_path / 'custom_cfg.yaml'
    cfg_path.write_text(yaml.safe_dump({
        'release_package_resources': {'package.xml': True, 'LICENSE': True},
        'source_package_resources': {'package.xml': True, 'LICENSE': True},
    }))

    def fake_rel(_dist_name, _repo, pkg_name, filepath='package.xml'):
        return _pkg_xml(pkg_name, '1.0.0') if filepath == 'package.xml' else 'BSD-3-Clause'

    fake_rel.__name__ = 'github_manifest_provider'

    def fake_src(_repo, filepaths=None, metadata=None):
        rc = SourceRepositoryCache.from_ref('2' * 40)
        rc.add('custom_pkg', '', _pkg_xml('custom_pkg', '1.0.0'), 'package.xml')
        if filepaths and 'LICENSE' in filepaths:
            rc.add('custom_pkg', '', 'BSD-3-Clause', 'LICENSE')
        return rc

    fake_src.__name__ = 'github_source_manifest_provider'

    with patch('rosdistro.Distribution.default_manifest_providers', [fake_rel]), \
            patch('rosdistro.Distribution.default_source_manifest_providers', [fake_src]):
        rc = build_cache_main([index_path, 'rolling', '--config', str(cfg_path)])

    assert not rc
    out = capsys.readouterr().out
    assert '[rolling] [release:package.xml] custom_pkg via=github (ok,' in out
    assert '[rolling] [release:LICENSE] custom_pkg via=github (ok,' in out
    assert '[rolling] [source:repo] custom_repo via=github (ok,' in out
    assert '[rolling] [cache:write] rolling-cache.yaml via=legacy_yaml (ok,' in out
    assert '[rolling] [cache:write] rolling-extended-cache.yaml via=extended_yaml (ok,' in out


def test_update_distribution_validation_console_output(capsys):
    dist_data = {
        'type': 'distribution',
        'version': 2,
        'repositories': {
            'ttl_repo': {'source': {'type': 'git', 'url': 'https://example.com/fresh.git', 'version': 'main'}},
            'ls_remote_repo': {'source': {'type': 'git', 'url': 'https://example.com/stale.git', 'version': 'main'}},
        },
    }
    cache = DistributionCache('rolling', distribution_file_data=dist_data, config=CacheConfig.extended())
    now = datetime.datetime.now()
    old = now - datetime.timedelta(hours=3)
    for name, ref, ts in (('ttl_repo', 'a' * 40, now), ('ls_remote_repo', 'b' * 40, old), ('obsolete_repo', 'c' * 40, old)):
        rc = SourceRepositoryCache.from_ref(ref)
        rc.add('pkg', '', _pkg_xml('pkg'), 'package.xml')
        rc.set_last_update_time(ts)
        cache.source_repo_resources[name] = rc

    with use_reporter(FetchReporter(debug=False)), patch('rosdistro.distribution_cache.Git') as mock_git:
        mock_git.return_value.command.return_value = {'returncode': 0, 'output': '%s\trefs/heads/main' % ('b' * 40)}
        cache.update_distribution([dist_data])

    out = capsys.readouterr().out
    assert '- validated 0 release package cache entries' in out
    assert '[rolling] [cache:validate_source_ref] ls_remote_repo via=git_ls_remote (kept,' in out
    assert '[rolling] [cache:validate_source_ref] obsolete_repo via=obsolete (dropped,' in out
    assert '- validated 3 source repository cache entries (kept=2, dropped=1' in out
