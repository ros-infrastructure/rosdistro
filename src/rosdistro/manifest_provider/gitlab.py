# Software License Agreement (BSD License)
#
# Copyright (c) 2021, Open Source Robotics Foundation, Inc.
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

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    from urllib.parse import quote as urlquote
except ImportError:
    from urllib2 import urlopen, Request
    from urllib2 import URLError
    from urllib2.parse import quote as urlquote

import json
import os
import re

from catkin_pkg.package import parse_package_string

from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro import logger


def _gitlab_paged_api_query(path, resource, attrs):
    _attrs = {'per_page': 50}
    _attrs.update(attrs)
    _attrs.pop('pagination', None)
    _attrs.pop('page', None)

    url = 'https://gitlab.com/api/v4/projects/%s/%s?pagination=keyset' % (urlquote(path, safe=''), resource)
    for k, v in _attrs.items():
        url += '&%s=%s' % (k, urlquote(str(v), safe=''))

    while True:
        with urlopen(url) as res:
            for result in json.loads(res.read().decode('utf-8')):
                yield result

            # Get the URL to the next page
            links = res.getheader('Link')
            if not links:
                break
            match = re.match(r'.*<([^>]*)>; rel="next"', links)
            if not match:
                break
            url = match.group(1)


def gitlab_manifest_provider(_dist_name, repo, pkg_name):
    assert repo.version
    server, path = repo.get_url_parts()
    if not server.endswith('gitlab.com'):
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise RuntimeError('can not handle non gitlab urls')

    release_tag = repo.get_release_tag(pkg_name)

    if not repo.has_remote_tag(release_tag):
        raise RuntimeError('specified tag "%s" is not a git tag' % release_tag)

    url = 'https://gitlab.com/%s/-/raw/%s/package.xml' % (path, release_tag)
    try:
        logger.debug('Load package.xml file from url "%s"' % url)
        return urlopen(url).read().decode('utf-8')
    except URLError as e:
        logger.debug('- failed (%s), trying "%s"' % (e, url))
        raise RuntimeError()


def gitlab_source_manifest_provider(repo):
    assert repo.version
    server, path = repo.get_url_parts()
    if not server.endswith('gitlab.com'):
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise RuntimeError('can not handle non gitlab urls')

    # Resolve the version ref to a sha
    sha = next(_gitlab_paged_api_query(path, 'repository/commits', {'per_page': 1, 'ref_name': repo.version}))['id']

    # Look for package.xml files in the tree
    package_xml_paths = set()
    for obj in _gitlab_paged_api_query(path, 'repository/tree', {'recursive': 'true', 'ref': sha}):
        if obj['path'].split('/')[-1] == 'package.xml':
            package_xml_paths.add(os.path.dirname(obj['path']))

    # Filter out ones that are inside other packages (eg, part of tests)
    def package_xml_in_parent(path):
        if path == '':
            return True
        parent = path
        while True:
            parent = os.path.dirname(parent)
            if parent in package_xml_paths:
                return False
            if parent == '':
                return True
    package_xml_paths = list(filter(package_xml_in_parent, package_xml_paths))

    cache = SourceRepositoryCache.from_ref(sha)
    for package_xml_path in package_xml_paths:
        url = 'https://gitlab.com/%s/-/raw/%s/%s' % \
            (path, sha, package_xml_path + '/package.xml' if package_xml_path else 'package.xml')
        logger.debug('- load package.xml from %s' % url)
        package_xml = urlopen(url).read().decode('utf-8')
        name = parse_package_string(package_xml).name
        cache.add(name, package_xml_path, package_xml)

    return cache
