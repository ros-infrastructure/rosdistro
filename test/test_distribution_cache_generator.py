import pytest
from unittest.mock import Mock, patch
from rosdistro.distribution_cache_generator import generate_distribution_cache

@patch('rosdistro.distribution_cache_generator._get_cached_distribution')
def test_generate_distribution_cache_version_invalidation(mock_get_cached_distribution):
    mock_index = Mock()
    mock_dist = Mock()
    mock_cache = Mock()
    
    mock_pkg_name = "test_pkg"
    mock_dist.release_packages = {mock_pkg_name: Mock(repository_name="test_repo")}
    
    mock_repo = Mock()
    mock_repo.release_repository.version = "1.2.3-1"
    mock_dist.repositories = {"test_repo": mock_repo}
    
    mock_dist.get_release_package_xml.return_value = '<package format="2"><name>test_pkg</name><version>1.2.3</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
    mock_dist.get_release_resource.return_value = "dummy"
    
    mock_cache.release_resources = {
        mock_pkg_name: {
            'version': '1.0.0-1',
            'package.xml': '<package format="2"><name>test_pkg</name><version>1.0.0</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
        }
    }
    
    mock_get_cached_distribution.return_value = (mock_dist, mock_cache)
    
    generate_distribution_cache(mock_index, "distro_name")
    
    # The cache should have been invalidated and the new version added
    assert mock_cache.release_resources[mock_pkg_name] == {'version': '1.2.3-1'}

@patch('rosdistro.distribution_cache_generator._get_cached_distribution')
def test_generate_distribution_cache_version_match(mock_get_cached_distribution):
    mock_index = Mock()
    mock_dist = Mock()
    mock_cache = Mock()
    
    mock_pkg_name = "test_pkg"
    mock_dist.release_packages = {mock_pkg_name: Mock(repository_name="test_repo")}
    
    mock_repo = Mock()
    mock_repo.release_repository.version = "1.2.3-1"
    mock_dist.repositories = {"test_repo": mock_repo}
    
    mock_dist.get_release_package_xml.return_value = '<package format="2"><name>test_pkg</name><version>1.2.3</version><description>d</description><maintainer email="m@m.com">m</maintainer><license>l</license></package>'
    mock_dist.get_release_resource.return_value = "dummy"
    
    mock_cache.release_resources = {
        mock_pkg_name: {
            'version': '1.2.3-1',
            'package.xml': 'old_xml'
        }
    }
    
    mock_get_cached_distribution.return_value = (mock_dist, mock_cache)
    
    generate_distribution_cache(mock_index, "distro_name")
    
    # The cache should NOT have been cleared because versions match
    assert mock_cache.release_resources[mock_pkg_name]['version'] == '1.2.3-1'
    assert mock_cache.release_resources[mock_pkg_name]['package.xml'] == 'old_xml'
