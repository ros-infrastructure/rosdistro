import os
import yaml

from rosdistro import get_index, get_distribution_file, get_distribution_files
from rosdistro.distribution_file import DistributionFile
from rosdistro.loader import load_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_distribution_file():
    url = 'file://' + FILES_DIR + '/foo/distribution.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    dist_file = DistributionFile('foo', data)
    _validate_dist_file(dist_file)


def test_get_distribution_file():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    dist_file = get_distribution_file(i, 'foo')
    _validate_dist_file(dist_file)

    dist_files = get_distribution_files(i, 'foo')
    assert len(dist_files) == 1


def _validate_dist_file(dist_file):
    assert('bar_repo' in dist_file.repositories)
    repo = dist_file.repositories['bar_repo']
    assert repo.release_repository.package_names == ['bar_repo']
    assert 'bar_repo' in dist_file.release_packages

    assert'baz-repo' in dist_file.repositories
    repo = dist_file.repositories['baz-repo']
    assert set(repo.release_repository.package_names) == set(['baz_pkg1', 'baz_pkg2'])
    assert 'baz_pkg1' in dist_file.release_packages
    assert 'baz_pkg2' in dist_file.release_packages
