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
from xml.dom import minidom

from rosdistro import logger

IGNORE_MARKERS = frozenset((
    'CATKIN_IGNORE',
    'AMENT_IGNORE',
    'COLCON_IGNORE',
    '.rosindex_ignore',
))

PACKAGE_ASSET_LIST_RESOURCES = ('launch_files', 'msg_files', 'srv_files')

DEFAULT_SOURCE_FILEPATHS = (
    'CHANGELOG.rst',
    'README.md',
    'launch_files',
    'msg_files',
    'srv_files',
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
      ``{'launch_files': [...], 'msg_files': [...], 'srv_files': [...]}`` with sorted
      package-relative paths.
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
            parent = os.path.dirname(parent)
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
        pkg_dir: {'launch_files': [], 'msg_files': [], 'srv_files': []}
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
            elif is_launch_file(rel_path):
                assets['launch_files'].append(rel_path)

    for assets in package_assets.values():
        assets['launch_files'] = sorted(set(assets['launch_files']))
        assets['msg_files'] = sorted(set(assets['msg_files']))
        assets['srv_files'] = sorted(set(assets['srv_files']))

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


def sanitize_and_truncate_docs(doc_string, max_length=100):
    if doc_string is None:
        return None
    # Remove trailing whitespace then truncate
    lines = doc_string.rstrip().splitlines()
    if max_length is not None and len(lines) > max_length:
        msg = f'Truncated content at {max_length} of {len(lines)} lines'
        if max_length <= 1:
            return msg
        return '\n'.join(lines[:max_length - 1]) + '\n' + msg
    return '\n'.join(lines)


def sanitize_xml(xml_string):
    """ Returns a version of the supplied XML string with comments and all whitespace stripped,
    including runs of spaces internal to text nodes. The returned value will be a str.
    """
    def _squash(node):
        # remove comment nodes
        for x in list(node.childNodes):
            if x.nodeType is minidom.Node.COMMENT_NODE:
                node.removeChild(x)
        # minimize whitespaces, remove empty text nodes
        for x in list(node.childNodes):
            if x.nodeType == minidom.Node.TEXT_NODE:
                if x.nodeValue:
                    x.nodeValue = ' '.join(x.nodeValue.strip().split())
                if not x.nodeValue:
                    node.removeChild(x)
        # process all tags recursively
        for x in node.childNodes:
            if x.nodeType == minidom.Node.ELEMENT_NODE:
                _squash(x)
        return node

    xml_node = _squash(minidom.parseString(xml_string))
    return xml_node.toxml()


class CachedManifestProvider(object):

    def __init__(self, distribution_cache, manifest_providers=None):
        self._distribution_cache = distribution_cache
        self._manifest_providers = manifest_providers

    def _get_max_lines(self, filepath):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_release_resource_max_lines'):
            return config.get_release_resource_max_lines(filepath)
        return 100

    def _sanitize_resource(self, filepath, content):
        if content is None:
            return None
        if filepath == 'package.xml':
            return sanitize_xml(content)
        return sanitize_and_truncate_docs(content, max_length=self._get_max_lines(filepath))

    def __call__(self, dist_name, repo, pkg_name, filepath='package.xml'):
        assert repo.version

        # Backwards compatibility for legacy ReleaseCache without release_resources
        if not hasattr(self._distribution_cache, 'release_resources'):
            if filepath != 'package.xml':
                return None
            package_xml = self._distribution_cache.release_package_xmls.get(pkg_name, None)
            if package_xml:
                package_xml = sanitize_xml(package_xml)
                self._distribution_cache.release_package_xmls[pkg_name] = package_xml
                logger.debug('Loading package.xml for package "%s" from cache' % pkg_name)
                return package_xml
            for mp in self._manifest_providers or []:
                try:
                    package_xml = sanitize_xml(mp(dist_name, repo, pkg_name))
                    break
                except Exception as e:
                    logger.debug('Skipped "%s()": %s' % (mp.__name__, e))
            if package_xml is None:
                return None
            self._distribution_cache.release_package_xmls[pkg_name] = package_xml
            return package_xml

        pkg_resources = self._distribution_cache.release_resources.get(pkg_name, {})
        if filepath in pkg_resources:
            manifest_content = self._sanitize_resource(filepath, pkg_resources[filepath])
            if manifest_content is not None:
                self._distribution_cache.release_resources[pkg_name][filepath] = manifest_content
            logger.debug('Loading %s for package "%s" from cache' % (filepath, pkg_name))
            return manifest_content

        if not self._manifest_providers:
            return None

        # Use manifest providers to lazy load
        fetched = False
        manifest_content = None
        for mp in self._manifest_providers:
            try:
                manifest_content = self._sanitize_resource(
                    filepath, mp(dist_name, repo, pkg_name, filepath)
                )
                fetched = True
                break
            except Exception as e:
                # pass and try next manifest provider
                logger.debug('Skipped "%s()": %s' % (getattr(mp, '__name__', mp), e))

        if not fetched or (filepath == 'package.xml' and manifest_content is None):
            return None

        # Populate the cache (including None for optional resources confirmed missing)
        if pkg_name not in self._distribution_cache.release_resources:
            self._distribution_cache.release_resources[pkg_name] = {}
        self._distribution_cache.release_resources[pkg_name][filepath] = manifest_content
        return manifest_content


class CachedSourceManifestProvider(object):

    def __init__(self, distribution_cache, source_manifest_providers=None):
        self._distribution_cache = distribution_cache
        self._source_manifest_providers = source_manifest_providers

    def _get_source_query_params(self):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and config.has_any_source_enabled():
            filepaths = [fp for fp in config.enabled_source_resources() if fp != 'package.xml']
            metadata = config.enabled_source_metadata()
            return filepaths, metadata
        return list(DEFAULT_SOURCE_FILEPATHS), None

    def _get_max_lines(self, filepath):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_source_resource_max_lines'):
            return config.get_source_resource_max_lines(filepath)
        return 100

    def _get_metadata_max_lines(self, key):
        config = getattr(self._distribution_cache, 'config', None)
        if config is not None and hasattr(config, 'get_source_metadata_max_lines'):
            return config.get_source_metadata_max_lines(key)
        return 100

    def _sanitize_resource(self, filepath, content):
        if content is None:
            return None
        if isinstance(content, list):
            return list(content)
        if filepath == 'package.xml':
            return sanitize_xml(content)
        return sanitize_and_truncate_docs(content, max_length=self._get_max_lines(filepath))

    def __call__(self, repo):
        assert repo.url
        repo_cache = self._distribution_cache.source_repo_resources.get(repo.name, None)
        if not repo_cache:
            logger.debug(f"Internal Cache Miss for {repo.name} Loading from Source Manifest Providers")
            filepaths, metadata = self._get_source_query_params()
            # Use manifest providers to lazy load
            for mp in self._source_manifest_providers or []:
                try:
                    try:
                        repo_cache = mp(repo, filepaths=filepaths, metadata=metadata)
                    except TypeError:
                        try:
                            repo_cache = mp(repo, filepaths=filepaths)
                        except TypeError:
                            repo_cache = mp(repo)
                except Exception as e:
                    # pass and try next manifest provider
                    logger.debug('Skipped "%s()": %s' % (getattr(mp, '__name__', mp), e))
                    continue

                if repo_cache is not None:
                    self._distribution_cache.source_repo_resources[repo.name] = repo_cache
                    break
        else:
            logger.debug('Load package XMLs for repo "%s" from cache' % repo.name)

        # Sanitize/truncate and de-duplicate with the release resources so the YAML writer uses aliases
        if repo_cache:
            for meta_doc in ('contributing', 'readme'):
                meta_val = repo_cache.get_metadata(meta_doc)
                if isinstance(meta_val, str):
                    repo_cache.set_metadata(
                        meta_doc,
                        sanitize_and_truncate_docs(meta_val, max_length=self._get_metadata_max_lines(meta_doc)),
                    )
            for package_name, pkg_entries in repo_cache._data.items():
                if package_name.startswith('_') or not isinstance(pkg_entries, dict):
                    continue
                for resource_type in list(pkg_entries.keys()):
                    if resource_type == 'package_path' or pkg_entries[resource_type] is None:
                        continue
                    content = self._sanitize_resource(resource_type, pkg_entries[resource_type])
                    if isinstance(content, str):
                        release_content = self._distribution_cache.release_resources.get(
                            package_name, {}
                        ).get(resource_type, None)
                        if content == release_content and release_content is not None:
                            logger.debug(
                                f'{package_name} Linking {resource_type} of source cache entry for compaction. '
                                f'Lines saved: {len(content.splitlines())}'
                            )
                            content = release_content
                    repo_cache.add(
                        package_name, pkg_entries['package_path'],
                        content, resource_type, increment_update_time=False
                    )
        return repo_cache
