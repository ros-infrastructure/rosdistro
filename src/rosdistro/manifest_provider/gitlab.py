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

import json
import os
import re
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlencode

from catkin_pkg.package import parse_package_string

from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro import logger

GITLAB_PRIVATE_TOKEN = os.getenv('GITLAB_PRIVATE_TOKEN', None)
ROSDISTRO_GITLAB_SERVER = os.getenv('ROSDISTRO_GITLAB_SERVER', None)

def _gitlab_urlopen(url):
    req = Request(url)
    if GITLAB_PRIVATE_TOKEN:
        req.add_header('Private-Token', GITLAB_PRIVATE_TOKEN)
    logger.warn('Performing GitLab API query "%s"' % (url,))
    return urlopen(req)


def _gitlab_api_query(server, path, resource, attrs):
    url = 'https://%s/api/v4/projects/%s/%s' % (server, urlquote(path, safe=''), resource)
    if attrs:
        url += '?' + urlencode(attrs)
    return _gitlab_urlopen(url)


def _gitlab_paged_api_query(server, path, resource, attrs):
    _attrs = {
        'per_page': 50,
        **attrs,
        'pagination': 'keyset',
        'page': '1',
    }

    url = 'https://%s/api/v4/projects/%s/%s' % (server, urlquote(path, safe=''), resource)
    if _attrs:
        url += '?' + urlencode(_attrs)

    while True:
        with _gitlab_urlopen(url) as res:
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
    if not server.endswith('gitlab.com') and server != ROSDISTRO_GITLAB_SERVER:
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise RuntimeError('can not handle non gitlab urls')

    resource = 'repository/files/package.xml/raw'
    attrs = {
        'ref': repo.get_release_tag(pkg_name),
    }
    try:
        with _gitlab_api_query(server, path, resource, attrs) as res:
            return res.read().decode('utf-8')
    except URLError as e:
        logger.debug('- failed (%s), trying "%s"' % (e, e.filename))
        raise


def gitlab_source_manifest_provider(repo):
    assert repo.version
    server, path = repo.get_url_parts()
    if not server.endswith('gitlab.com') and server != ROSDISTRO_GITLAB_SERVER:
        logger.debug('Skip non-gitlab url "%s"' % repo.url)
        raise RuntimeError('can not handle non gitlab urls')

    # Resolve the version ref to a sha since we need to make multiple queries
    attrs = {
        'per_page': 1,
        'ref_name': repo.version,
    }
    sha = next(_gitlab_paged_api_query(server, path, 'repository/commits', attrs))['id']

    # Look for package.xml files in the tree
    attrs = {
        'recursive': 'true',
        'ref': sha,
    }
    package_xml_paths = set()
    for obj in _gitlab_paged_api_query(server, path, 'repository/tree', attrs):
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
        resource_path = urlquote(
            package_xml_path + '/package.xml' if package_xml_path else 'package.xml', safe='')
        resource = 'repository/files/' + resource_path + '/raw'
        with _gitlab_api_query(server, path, resource, {'ref': sha}) as res:
            package_xml = res.read().decode('utf-8')
        name = parse_package_string(package_xml).name
        cache.add(name, package_xml_path, package_xml)

    return cache
