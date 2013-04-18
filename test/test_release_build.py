import os
import yaml

from rosdistro import get_index, get_release, get_release_build_files, get_release_builds
from rosdistro.loader import load_url
from rosdistro.release_build_file import ReleaseBuildFile

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'files'))


def test_release_build_file():
    url = 'file://' + FILES_DIR + '/foo/release-build.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    ReleaseBuildFile('foo', data)


def test_get_release_build_files():
    url = 'file://' + FILES_DIR + '/index.yaml'
    i = get_index(url)
    get_release_build_files(i, 'foo')


def test_get_release_builds():
    url = 'file://' + FILES_DIR + '/index.yaml'
    i = get_index(url)
    d = get_release(i, 'foo')
    builds = get_release_builds(i, d)
    assert len(builds) == 1
    build = builds[0]
    assert build.jenkins_sourcedeb_job_timeout == 5
    assert build.jenkins_binarydeb_job_timeout == 42
