from rosdistro.repository_specification import RepositorySpecification


def test_repository_specification():
    data = {'type': 'git', 'url': 'https://github.com/ros/catkin.git'}
    r = RepositorySpecification("test", data)
    assert r.get_data() == data
    assert r.version is None
    assert r.get_url_parts() == ('github.com', 'ros/catkin')

    r.url = 'http://github.com/ros/catkin'
    assert r.get_url_parts() == ('github.com', 'ros/catkin')

    r.url = 'ssh://example.com/a/b/c.git'
    assert r.get_url_parts() == ('example.com', 'a/b/c')

    r.url = 'git://example.com/a/b/c/d.git'
    assert r.get_url_parts() == ('example.com', 'a/b/c/d')

    r.url = 'git@example.com:a/b/c/d/e.git'
    assert r.get_url_parts() == ('example.com', 'a/b/c/d/e')

    r.url = 'https://1234567890abcdeABCDE1234567890abcdeABCDE@example.com:a/b/c/d/e.git'
    parts = r.get_url_parts()
    assert(len(parts) == 2)
    assert(parts[0].endswith('example.com'))
    assert(parts[1] == 'a/b/c/d/e')
