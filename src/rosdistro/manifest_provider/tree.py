# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Open Source Robotics Foundation, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Open Source Robotics Foundation, Inc. nor
#    the names of its contributors may be used to endorse or promote
#    products derived from this software without specific prior
#    written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import os
import posixpath

IGNORE_MARKERS = frozenset((
    'CATKIN_IGNORE',
    'AMENT_IGNORE',
    'COLCON_IGNORE',
    '.rosindex_ignore',
))

PACKAGE_ASSET_LIST_RESOURCES = ('launch_files', 'msg_files', 'srv_files', 'action_files')

DEFAULT_SOURCE_FILEPATHS = (
    'CHANGELOG.rst',
    'README.md',
    'launch_files',
    'msg_files',
    'srv_files',
    'action_files',
)

ROOT_DOC_CANDIDATES = {
    'contributing': ('CONTRIBUTING.md', 'CONTRIBUTING.rst'),
    'readme': ('README.md', 'README.rst'),
}


def is_launch_file(rel_path):
    """Return True if *rel_path* (relative to a package directory) is a launch file."""
    parts = rel_path.replace('\\', '/').strip('/').split('/')
    if not parts or not parts[-1]:
        return False
    filename = parts[-1].lower()
    if filename in ('package.xml', '__init__.py'):
        return False
    if filename.endswith(('.launch', '.launch.xml', '.launch.py', '.launch.yaml', '.launch.yml')):
        return True
    dir_parts = [p.lower() for p in parts[:-1]]
    if any('launch' in d for d in dir_parts) and filename.endswith(('.xml', '.py', '.yaml', '.yml')):
        return True
    return False


def partition_repo_tree(blob_paths):
    """Scan a list of repository-relative file paths, respecting ignore markers and nested
    package boundaries.

    Returns ``(package_xml_paths, package_assets, root_files)`` where:
    - ``package_xml_paths`` is the list of non-ignored top-level package directories
      (e.g. ``''`` for a root package or ``'my_pkg'`` for ``my_pkg/package.xml``).
    - ``package_assets`` maps each ``package_xml_path`` in ``package_xml_paths`` to
      ``{'launch_files': [...], 'msg_files': [...], 'srv_files': [...], 'action_files': [...]}``
      with sorted package-relative paths.
    - ``root_files`` is the set of top-level file names at the repository root.
    """
    normalized = [p.replace('\\', '/').strip('/') for p in blob_paths if p and p.strip('/')]
    root_files = {p for p in normalized if '/' not in p}

    # Check if repository root itself contains an ignore marker
    if IGNORE_MARKERS.intersection(root_files):
        return [], {}, root_files

    ignored_dirs = set()
    for p in normalized:
        parts = p.split('/')
        if parts[-1] in IGNORE_MARKERS and len(parts) > 1:
            ignored_dirs.add('/'.join(parts[:-1]))

    def _is_ignored(path_or_dir):
        if not ignored_dirs or not path_or_dir:
            return False
        for ig in ignored_dirs:
            if path_or_dir == ig or path_or_dir.startswith(ig + '/'):
                return True
        return False

    all_pkg_dirs_list = []
    all_pkg_dirs_set = set()
    for p in normalized:
        parts = p.split('/')
        if parts[-1] != 'package.xml':
            continue
        parent_dir = '/'.join(parts[:-1])
        if _is_ignored(parent_dir):
            continue
        if any(part.startswith('.') for part in parts[:-1]):
            continue
        if parent_dir not in all_pkg_dirs_set:
            all_pkg_dirs_list.append(parent_dir)
            all_pkg_dirs_set.add(parent_dir)

    # Filter out nested packages inside other packages (matching find_package_paths)
    def _package_xml_in_parent(pkg_dir):
        if pkg_dir == '':
            return True
        parent = pkg_dir
        while True:
            parent = posixpath.dirname(parent)
            if parent in all_pkg_dirs_set:
                return False
            if parent == '':
                return True

    package_xml_paths = [d for d in all_pkg_dirs_list if _package_xml_in_parent(d)]

    # Sort all package dirs (including nested ones) deepest-first so nested package
    # directories claim their own files and prevent them from leaking into parent packages.
    sorted_all_pkg_dirs = sorted(
        all_pkg_dirs_list,
        key=lambda d: (len(d.split('/')) if d else 0, len(d)),
        reverse=True,
    )
    package_assets = {
        pkg_dir: {'launch_files': [], 'msg_files': [], 'srv_files': [], 'action_files': []}
        for pkg_dir in package_xml_paths
    }

    if sorted_all_pkg_dirs:
        for p in normalized:
            if _is_ignored(p):
                continue
            owning_pkg_dir = None
            rel_path = None
            for pkg_dir in sorted_all_pkg_dirs:
                if pkg_dir == '':
                    owning_pkg_dir = ''
                    rel_path = p
                    break
                if p.startswith(pkg_dir + '/'):
                    owning_pkg_dir = pkg_dir
                    rel_path = p[len(pkg_dir) + 1:]
                    break
            if owning_pkg_dir is None or owning_pkg_dir not in package_assets or not rel_path:
                continue
            if any(part.startswith('.') for part in rel_path.split('/')):
                continue
            assets = package_assets[owning_pkg_dir]
            if rel_path.endswith('.msg'):
                assets['msg_files'].append(rel_path)
            elif rel_path.endswith('.srv'):
                assets['srv_files'].append(rel_path)
            elif rel_path.endswith('.action'):
                assets['action_files'].append(rel_path)
            elif is_launch_file(rel_path):
                assets['launch_files'].append(rel_path)

    for assets in package_assets.values():
        assets['launch_files'] = sorted(set(assets['launch_files']))
        assets['msg_files'] = sorted(set(assets['msg_files']))
        assets['srv_files'] = sorted(set(assets['srv_files']))
        assets['action_files'] = sorted(set(assets['action_files']))

    return package_xml_paths, package_assets, root_files


def list_local_repo_files(repo_root):
    """Walk *repo_root* on disk (excluding hidden directories like ``.git``) and return
    a list of repository-relative POSIX file paths.
    """
    rel_files = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        rel_dir = os.path.relpath(root, repo_root)
        if rel_dir == '.':
            rel_dir = ''
        else:
            rel_dir = rel_dir.replace(os.sep, '/')
        for fname in sorted(files):
            rel_files.append(f'{rel_dir}/{fname}' if rel_dir else fname)
        if IGNORE_MARKERS.intersection(files):
            dirs[:] = []
    return rel_files
