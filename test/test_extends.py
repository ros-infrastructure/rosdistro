import os
import pytest
from rosdistro import get_distribution_file, get_index, CircularInheritanceError
from . import path_to_url

FILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files'))


def test_extends_parsing():
    url = path_to_url(os.path.join(FILES_DIR, 'index_extends.yaml'))
    index = get_index(url)

    # Test base distro loading
    base_dist = get_distribution_file(index, 'base')
    assert base_dist.version == 3
    assert 'bar_repo' in base_dist.repositories
    assert base_dist.repositories['bar_repo'].origin_distro == 'base'

    # Test derived distro loading and inheritance merging
    derived_dist = get_distribution_file(index, 'derived')
    assert derived_dist.version == 3

    # Check extends attributes are parsed correctly
    assert len(derived_dist.extends) == 1
    assert derived_dist.extends[0]['distro_name'] == 'base'
    assert derived_dist.extends[0]['extension_method'] == 'binary_import'

    # Check dependencies are parsed correctly
    assert len(derived_dist.dependencies) == 1
    assert derived_dist.dependencies[0]['rosdep_sources_list_urls'] == [
        'https://raw.githubusercontent.com/ros/rosdistro/master/rosdep/base.yaml'
    ]
    assert derived_dist.dependencies[0]['rosdep_minimum_target_platforms'] == [
        'ubuntu:noble'
    ]

    # Check repositories merged
    assert 'derived_repo' in derived_dist.repositories
    assert 'bar_repo' in derived_dist.repositories

    # Check metadata annotation
    assert derived_dist.repositories['derived_repo'].origin_distro == 'derived'
    assert derived_dist.repositories['derived_repo'].extension_method is None
    assert derived_dist.repositories['bar_repo'].origin_distro == 'base'
    assert derived_dist.repositories['bar_repo'].extension_method == 'binary_import'


def test_circular_extends():
    url = path_to_url(os.path.join(FILES_DIR, 'index_extends.yaml'))
    index = get_index(url)

    with pytest.raises(CircularInheritanceError) as excinfo:
        get_distribution_file(index, 'cyclic_a')
    assert "Circular inheritance detected" in str(excinfo.value)


def test_platform_compatibility_warning(capsys):
    url = path_to_url(os.path.join(FILES_DIR, 'index_extends.yaml'))
    index = get_index(url)

    get_distribution_file(index, 'derived_invalid_platform')
    captured = capsys.readouterr()
    assert "WARNING: Target platform 'ubuntu:quantal' specified in derived distribution is not supported by base distribution." in captured.out


def test_multi_parent_precedence_and_collisions(capsys):
    url = path_to_url(os.path.join(FILES_DIR, 'index_extends.yaml'))
    index = get_index(url)

    child_dist = get_distribution_file(index, 'multi_parent_child')
    captured = capsys.readouterr()

    # Verify precedence: collision_repo release URL and version should match parent_a (0.1.0) because parent_a was declared first
    assert 'collision_repo' in child_dist.repositories
    assert child_dist.repositories['collision_repo'].release_repository.version == '0.1.0'

    # Verify collision warnings are logged for both repo and package
    assert "WARNING: Collision detected. Repository 'collision_repo' is defined in multiple parents ('parent_a' and 'parent_b'). Using definition from 'parent_a'." in captured.out
    assert "WARNING: Collision detected. Package 'collision_pkg' is defined in multiple parents ('parent_a' and 'parent_b'). Using definition from 'parent_a'." in captured.out

    # Verify metadata annotation: parent_a is binary_import, parent_b is source_rebuild
    # repo_a: imported from parent_a (binary_import) -> origin_distro remains parent_a
    assert child_dist.repositories['repo_a'].origin_distro == 'parent_a'
    assert child_dist.repositories['repo_a'].extension_method == 'binary_import'

    # repo_b: rebuilt from parent_b (source_rebuild) -> origin_distro becomes child (multi_parent_child)
    assert child_dist.repositories['repo_b'].origin_distro == 'multi_parent_child'
    assert child_dist.repositories['repo_b'].extension_method == 'source_rebuild'
