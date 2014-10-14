import os
import yaml

from rosdistro import get_index, get_release, get_release_build_files, get_release_builds
from rosdistro.loader import load_url
from rosdistro.release_build_file import ReleaseBuildFile

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_release_build_file():
    url = 'file://' + FILES_DIR + '/foo/release-build.yaml'
    yaml_str = load_url(url)
    data = yaml.load(yaml_str)
    ReleaseBuildFile('foo', data)


def test_get_release_build_files():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    get_release_build_files(i, 'foo')


def test_get_release_builds():
    url = 'file://' + FILES_DIR + '/index_v2.yaml'
    i = get_index(url)
    d = get_release(i, 'foo')
    builds = get_release_builds(i, d)
    assert len(builds) == 1
    build = builds[0]
    assert build.jenkins_sourcedeb_job_timeout == 5
    assert build.jenkins_binarydeb_job_timeout == 42

    os_names = build.get_target_os_names()
    assert set(os_names) == set(['ubuntu'])
    os_code_names = build.get_target_os_code_names('ubuntu')
    assert set(os_code_names) == set(['precise', 'quantal', 'raring'])
    arches = build.get_target_arches('ubuntu', 'precise')
    assert set(arches) == set(['amd64', 'i386'])

    c = build.get_target_configuration()
    assert len(c.keys()) == 2
    assert set(c.keys()) == set(['apt_target_repository', 'foo'])
    assert c['apt_target_repository'] == 'http://repo.example.com/'
    assert c['foo'] == 'bar'

    c = build.get_target_configuration('ubuntu', 'precise')
    assert 'foo' in c.keys()
    assert c['foo'] == 'bar'
    assert 'ping' in c.keys()
    assert c['ping'] == 'pong'

    c = build.get_target_configuration('ubuntu', 'precise', 'amd64')
    assert 'foo' in c.keys()
    assert c['foo'] == 'baz'
