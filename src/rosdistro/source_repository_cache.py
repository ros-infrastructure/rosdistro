# Software License Agreement (BSD License)
#
# Copyright (c) 2018, Open Source Robotics Foundation, Inc.
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

from datetime import datetime

import yaml


class _PackageResourceDict(dict):
    """Dictionary of package resources that also supports legacy v2 tuple indexing:
    pkg_data[0] -> package_path, pkg_data[1] -> package.xml.
    """

    def __getitem__(self, key):
        if key == 0:
            return self.get('package_path', '')
        if key == 1:
            return self.get('package.xml')
        return super(_PackageResourceDict, self).__getitem__(key)


yaml.SafeDumper.add_representer(_PackageResourceDict, yaml.SafeDumper.represent_dict)


class SourceRepositoryCache(object):
    """
    This class represents a cache of the resource strings for all packages in a single
    repo at a particular moment in time. A dictionary of many of these (one for each repo)
    keyed to the repo name represents the totality of the source package resources cache.
    """

    def __init__(self, data):
        assert data
        self._ref = data['_ref']
        self._data = data
        self._last_update_time = data.get('_last_update_time')

        self._package_names = set([name for name in data.keys() if not name.startswith('_')])
        for pkg_name in self._package_names:
            if isinstance(self._data[pkg_name], (list, tuple)) and len(self._data[pkg_name]) == 2:
                package_path, package_xml = self._data[pkg_name]
                self._data[pkg_name] = _PackageResourceDict({
                    'package_path': package_path,
                    'package.xml': package_xml,
                })
            elif isinstance(self._data[pkg_name], dict):
                if '_last_update_time' in self._data[pkg_name]:
                    pkg_update_time = self._data[pkg_name].pop('_last_update_time')
                    if self._last_update_time is None and pkg_update_time is not None:
                        self._last_update_time = pkg_update_time
                if not isinstance(self._data[pkg_name], _PackageResourceDict):
                    self._data[pkg_name] = _PackageResourceDict(self._data[pkg_name])

    @property
    def _last_update_time(self):
        val = self._data.get('_last_update_time')
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(val)

    @_last_update_time.setter
    def _last_update_time(self, value):
        if value is None:
            self._data.pop('_last_update_time', None)
        elif isinstance(value, datetime):
            self._data['_last_update_time'] = value.isoformat()
        elif isinstance(value, str):
            datetime.fromisoformat(value)
            self._data['_last_update_time'] = value
        else:
            raise TypeError("Expected datetime, ISO-8601 str, or None for _last_update_time")

    def get_data(self, version=3):
        """
        Return the bare data dict, suitable for serializing into yaml.
        """
        if version == 2:
            data = {'_ref': self._ref}
            for pkg_name in sorted(self._package_names):
                pkg_data = self._data[pkg_name]
                data[pkg_name] = [pkg_data.get('package_path', ''), pkg_data.get('package.xml')]
            return data
        if isinstance(self._data.get('_last_update_time'), datetime):
            self._data['_last_update_time'] = self._data['_last_update_time'].isoformat()
        return self._data

    @classmethod
    def from_ref(cls, ref):
        """
        Create a new empty cache instance from just the version control reference hash.
        """
        return cls({'_ref': ref})

    def get_metadata(self, key, default=None):
        """
        Return the value of a repository metadata field (e.g., 'stars', 'description', 'tags').
        """
        meta_key = key if key.startswith('_') else '_' + key
        return self._data.get(meta_key, default)

    def set_metadata(self, key, value):
        """
        Set the value of a repository metadata field (e.g., 'stars', 'description', 'tags').
        """
        meta_key = key if key.startswith('_') else '_' + key
        self._data[meta_key] = value

    def get_all_metadata(self):
        """
        Return a dictionary of all repository metadata fields (excluding internal '_ref' and '_last_update_time').
        """
        return {
            k[1:]: v for k, v in self._data.items()
            if k.startswith('_') and k not in ('_ref', '_last_update_time')
        }

    def stars(self):
        """
        Return the repository star count, or None if not available.
        """
        return self.get_metadata('stars')

    def set_stars(self, stars):
        """
        Set the repository stars.
        """
        self.set_metadata('stars', stars)

    def description(self):
        """
        Return the repository description, or None if not available.
        """
        return self.get_metadata('description')

    def set_description(self, description):
        """
        Set the repository description.
        """
        self.set_metadata('description', description)

    def tags(self):
        """
        Return the repository tags/topics list, or None if not available.
        """
        return self.get_metadata('tags')

    def set_tags(self, tags):
        """
        Set the repository tags.
        """
        self.set_metadata('tags', tags)

    def last_commit_time(self):
        """
        Return the repository last commit timestamp string, or None if not available.
        """
        return self.get_metadata('last_commit_time')

    def set_last_commit_time(self, last_commit_time):
        """
        Set the repository last commit timestamp string.
        """
        self.set_metadata('last_commit_time', last_commit_time)

    def contributing(self):
        """
        Return the repository root CONTRIBUTING contents, or None if not available.
        """
        return self.get_metadata('contributing')

    def set_contributing(self, contributing):
        """
        Set the repository root CONTRIBUTING contents.
        """
        self.set_metadata('contributing', contributing)

    def readme(self):
        """
        Return the repository root README contents, or None if not available.
        """
        return self.get_metadata('readme')

    def set_readme(self, readme):
        """
        Set the repository root README contents.
        """
        self.set_metadata('readme', readme)

    def launch_files(self, package_name):
        """
        Return the list of relative launch file paths for package_name, or None.
        """
        return self[package_name].get('launch_files')

    def msg_files(self, package_name):
        """
        Return the list of relative .msg file paths for package_name, or None.
        """
        return self[package_name].get('msg_files')

    def srv_files(self, package_name):
        """
        Return the list of relative .srv file paths for package_name, or None.
        """
        return self[package_name].get('srv_files')

    def action_files(self, package_name):
        """
        Return the list of relative .action file paths for package_name, or None.
        """
        return self[package_name].get('action_files')

    def set_last_update_time(self, update_time):
        """
        Set the repository last update time.
        """
        self._last_update_time = update_time

    def last_update_time(self):
        """
        Return the last update time as a datetime object, or None.
        """
        return self._last_update_time

    def add(self, package_name, package_path, payload_string, payload_type='package.xml', increment_update_time=True):
        """
        Add a package resource to the cache.
        """
        if package_name not in self._data or not isinstance(self._data[package_name], _PackageResourceDict):
            existing = self._data.get(package_name)
            self._data[package_name] = _PackageResourceDict(existing if isinstance(existing, dict) else {})

        self._data[package_name]['package_path'] = package_path
        self._data[package_name][payload_type] = payload_string
        self._package_names.add(package_name)
        if increment_update_time:
            self._last_update_time = datetime.now()

    def __iter__(self):
        """
        Iterate the list of package names in this cached repo. Returns an iterator of str.
        """
        return iter(self._package_names)

    def __getitem__(self, package_name):
        """
        Access the cached information about a specific package. Returns a dict of
        path to package relative paths to repo root, and string of the file contents (potentially truncated).
        """
        if package_name not in self._package_names:
            raise KeyError("Package '%s' not present in SourceRepositoryCache." % package_name)
        return self._data[package_name]

    def items(self):
        """
        Generator of (str, dict) containing the package name, and a dict of
        paths to file contents (potentially truncated).
        """
        for package_name in self._package_names:
            yield package_name, self._data[package_name]

    def __len__(self):
        """
        Returns the number of packages in this repo.
        """
        return len(self._package_names)

    def keys(self):
        """
        Return the list of packages in the repo cache.
        """
        return self._package_names

    def ref(self):
        """
        Return the version control hash.
        """
        return self._ref
