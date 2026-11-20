from contextlib import contextmanager
import io
import tarfile
from unittest.mock import MagicMock, patch

import yaml

from rosdistro import CacheConfig, DistributionCache, get_index
from rosdistro.distribution_cache_generator import (
    generate_distribution_cache,
    generate_distribution_caches,
)
from rosdistro.manifest_provider.cache import CachedManifestProvider
from rosdistro.manifest_provider.engine import fetch_release_file
from rosdistro.manifest_provider.git import git_manifest_provider
from rosdistro.manifest_provider.tar import tar_manifest_provider
from rosdistro.release_repository_specification import ReleaseRepositorySpecification

from . import path_to_url
from .test_manifest_engine import DictProvider


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


def _mock_http_response(body):
    resp = MagicMock()
    resp.read.return_value = body.encode('utf-8') if isinstance(body, str) else body
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


def _write_index(tmp_path, distros):
    index_data = {'type': 'index', 'version': 4, 'distributions': {}}
    for dist_name, repos in distros.items():
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
    index_path = tmp_path / 'index.yaml'
    index_path.write_text(yaml.safe_dump(index_data))
    return str(index_path)


def test_git_and_tar_release_extended_build_clone_and_download_once_per_package(tmp_path, capsys):
    git_dir = tmp_path / 'git_release_tag'
    git_dir.mkdir()
    (git_dir / 'package.xml').write_text(_pkg_xml('git_pkg', '1.0.0'))
    (git_dir / 'README.md').write_text('# git_pkg README\n')
    # CHANGELOG.rst intentionally missing -> confirmed None

    tar_bytes = _build_tar_bytes({
        'rel-tar_pkg-2.0.0-1/package.xml': _pkg_xml('tar_pkg', '2.0.0'),
        'rel-tar_pkg-2.0.0-1/README.md': '# tar_pkg README\n',
        'rel-tar_pkg-2.0.0-1/CHANGELOG.rst': '2.0.0\n* init\n',
    })

    index_path = _write_index(tmp_path, {
        'rolling': {
            'git_repo': {
                'release': {
                    'packages': ['git_pkg'],
                    'tags': {'release': 'release/rolling/{package}/{version}'},
                    'url': 'https://example.com/git_pkg-release.git',
                    'version': '1.0.0-1',
                },
            },
            'tar_repo': {
                'release': {
                    'type': 'tar',
                    'packages': ['tar_pkg'],
                    'tags': {'release': 'rel-{package}-{version}'},
                    'url': 'https://example.com/tar_pkg-release.tar.gz',
                    'version': '2.0.0-1',
                },
            },
        },
    })

    clone_calls = []
    tar_downloads = []

    @contextmanager
    def fake_clone(url, ref):
        clone_calls.append((url, ref))
        yield str(git_dir)

    def fake_tar_urlopen(req, timeout=None):
        tar_downloads.append(req.get_full_url())
        return _mock_http_response(tar_bytes)

    cfg = CacheConfig({
        'release_package_resources': {
            'package.xml': True,
            'README.md': True,
            'CHANGELOG.rst': True,
        },
    })
    with patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone), \
            patch('rosdistro.manifest_provider.tar.urlopen', side_effect=fake_tar_urlopen):
        caches = generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, ignore_local=True, config=cfg
        )

    assert len(clone_calls) == 1
    assert len(tar_downloads) == 1

    cache = caches['rolling']
    assert '<name>git_pkg</name>' in cache.release_resources['git_pkg']['package.xml']
    assert cache.release_resources['git_pkg']['README.md'] == '# git_pkg README'
    assert cache.release_resources['git_pkg']['CHANGELOG.rst'] is None
    assert '<name>tar_pkg</name>' in cache.release_resources['tar_pkg']['package.xml']
    assert cache.release_resources['tar_pkg']['README.md'] == '# tar_pkg README'
    assert cache.release_resources['tar_pkg']['CHANGELOG.rst'] == '2.0.0\n* init'

    out = capsys.readouterr().out
    assert '[rolling] [release:package.xml] git_pkg via=git (ok,' in out
    assert '[rolling] [release:README.md] git_pkg via=git (ok, 0.000s, batched)' in out
    assert '[rolling] [release:CHANGELOG.rst] git_pkg via=git (not_found, 0.000s, batched)' in out
    assert '[rolling] [release:package.xml] tar_pkg via=tar (ok,' in out
    assert '[rolling] [release:README.md] tar_pkg via=tar (ok, 0.000s, batched)' in out
    assert '[rolling] [release:CHANGELOG.rst] tar_pkg via=tar (ok, 0.000s, batched)' in out


def test_batched_fetch_mixed_cache_neighbor_and_pending(tmp_path):
    repos = {
        'shared_repo': {
            'release': {
                'packages': ['shared_pkg'],
                'tags': {'release': 'release/shared/{package}/{version}'},
                'url': 'https://example.com/shared-release.git',
                'version': '1.0.0-1',
            },
        },
    }
    index_path = _write_index(tmp_path, {'distro_a': repos, 'distro_b': repos})

    # Neighbor distro_a has package.xml cached, but NOT README.md or CHANGELOG.rst
    dist_a_data = yaml.safe_load((tmp_path / 'distro_a' / 'distribution.yaml').read_text())
    neighbor_cache = DistributionCache(
        'distro_a',
        distribution_file_data=dist_a_data,
        config=CacheConfig({'release_package_resources': {'package.xml': True}}),
    )
    neighbor_cache.release_resources['shared_pkg'] = {
        'version': '1.0.0-1',
        'package.xml': _pkg_xml('shared_pkg', '1.0.0'),
    }

    git_dir = tmp_path / 'shared_clone'
    git_dir.mkdir()
    (git_dir / 'package.xml').write_text(_pkg_xml('shared_pkg', '1.0.0'))
    (git_dir / 'README.md').write_text('# Shared README\n')
    (git_dir / 'CHANGELOG.rst').write_text('1.0.0\n')
    clone_calls = []

    @contextmanager
    def fake_clone(url, ref):
        clone_calls.append((url, ref))
        yield str(git_dir)

    cfg = CacheConfig({
        'release_package_resources': {
            'package.xml': True,
            'README.md': True,
            'CHANGELOG.rst': True,
        },
    })
    with patch('rosdistro.Distribution.default_manifest_providers', [git_manifest_provider]), \
            patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone):
        cache_b = generate_distribution_cache(
            get_index(path_to_url(index_path)),
            'distro_b',
            preclean=False,
            ignore_local=True,
            config=cfg,
            neighbor_caches={'distro_a': neighbor_cache},
        )

    # package.xml came from neighbor distro_a; README.md + CHANGELOG.rst came from ONE git clone
    assert len(clone_calls) == 1
    assert '<name>shared_pkg</name>' in cache_b.release_resources['shared_pkg']['package.xml']
    assert cache_b.release_resources['shared_pkg']['README.md'] == '# Shared README'
    assert cache_b.release_resources['shared_pkg']['CHANGELOG.rst'] == '1.0.0'


def test_batched_fetch_optional_file_error_falls_back_only_for_failed_file():
    primary = DictProvider(
        files={'package.xml': _pkg_xml('pkg_a')},
        file_errors={'README.md': RuntimeError('500 transient')},
    )
    primary.name = 'primary'
    fallback = DictProvider(
        files={'package.xml': _pkg_xml('pkg_a'), 'README.md': '# Fallback README\n'},
    )
    fallback.name = 'fallback'

    dist_data = {'type': 'distribution', 'version': 2, 'repositories': {}}
    cache = DistributionCache('rolling', distribution_file_data=dist_data)
    repo = ReleaseRepositorySpecification('pkg_a', {
        'url': 'https://example.com/pkg_a-release.git',
        'version': '1.0.0-1',
        'tags': {'release': 'release/rolling/{package}/{version}'},
    })

    def primary_manifest_provider(_dist, r, pkg, fp='package.xml'):
        return fetch_release_file(primary, r, pkg, fp)

    primary_manifest_provider._unified_provider = primary

    def fallback_manifest_provider(_dist, r, pkg, fp='package.xml'):
        return fetch_release_file(fallback, r, pkg, fp)

    fallback_manifest_provider._unified_provider = fallback

    cached = CachedManifestProvider(cache, [primary_manifest_provider, fallback_manifest_provider])
    res = cached.fetch_many('rolling', repo, 'pkg_a', ('package.xml', 'README.md', 'CHANGELOG.rst'))

    assert '<name>pkg_a</name>' in res['package.xml']
    assert res['README.md'] == '# Fallback README'
    assert res['CHANGELOG.rst'] is None
    assert cached.last_fetch_infos['package.xml']['fetch_type'] == 'primary'
    assert cached.last_fetch_infos['CHANGELOG.rst']['fetch_type'] == 'primary'
    assert cached.last_fetch_infos['README.md']['fetch_type'] == 'fallback'
    # Fallback was ONLY asked for README.md, not package.xml or CHANGELOG.rst
    fallback_reads = [e[2] for e in fallback.events if e[0] == 'read_file']
    assert fallback_reads == ['README.md']


def test_batched_fetch_invalid_package_xml_leaves_no_stub_in_non_strict_mode(tmp_path):
    git_dir = tmp_path / 'bad_pkg_clone'
    git_dir.mkdir()
    # Valid XML syntax (passes sanitize_xml) but invalid catkin package (missing <version>)
    (git_dir / 'package.xml').write_text('<package format="2"><name>bad_pkg</name></package>')
    (git_dir / 'README.md').write_text('# Should not leave stub\n')

    index_path = _write_index(tmp_path, {
        'rolling': {
            'bad_repo': {
                'release': {
                    'packages': ['bad_pkg'],
                    'tags': {'release': 'release/rolling/{package}/{version}'},
                    'url': 'https://example.com/bad-release.git',
                    'version': '1.0.0-1',
                },
            },
        },
    })

    @contextmanager
    def fake_clone(_url, _ref):
        yield str(git_dir)

    cfg = CacheConfig({
        'release_package_resources': {
            'package.xml': True,
            'README.md': True,
            'CHANGELOG.rst': True,
        },
    })
    with patch('rosdistro.Distribution.default_manifest_providers', [git_manifest_provider]), \
            patch('rosdistro.manifest_provider.git._temp_git_clone', fake_clone):
        caches = generate_distribution_caches(
            index_path, dist_names=['rolling'], preclean=True, ignore_local=True, config=cfg
        )

    assert 'bad_pkg' not in caches['rolling'].release_resources
