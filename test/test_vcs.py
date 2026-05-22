from rosdistro.vcs import _version_gte

import pytest

@pytest.mark.parametrize("version, required_version, expected", [
    ("2.50.0", "2.51.0", False),
    ("2.51.0", "2.51.0", True),
    ("2.52.0", "2.51.0", True),
    ("2.51.0-rc1", "2.51.0", False),
    ("2.51.0-rc1", "2.51.0-rc1", True),
    ("2.51.0-rc1", "2.51.0-rc2", False),
    ("2.51.0-rc1", "2.51.0-rc0", True),
    ("2.51.0-rc1", "2.51.0-rc0+post1", True),
    ("2.51.0-rc1", "2.51.0-rc1+post1", False),
    ("2.51.0.windows.1", "2.50.0", True),
    ("2.51.0.windows.1", "2.51.0", True),
    ("2.51.0.windows.1", "2.52", False),
])
def test_version_gte(version: str, required_version: str, expected: bool):
    assert _version_gte(version, required_version) == expected
