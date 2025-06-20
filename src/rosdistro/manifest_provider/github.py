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

import base64
import json
import os
import time

from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.error import URLError

from catkin_pkg.package import parse_package_string

from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro import logger

GITHUB_USER = os.getenv('GITHUB_USER', None)
GITHUB_PASSWORD = os.getenv('GITHUB_PASSWORD', None)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', None)

def _get_url_contents(url):
    backoff = 1
    while backoff < 120:
        try:
            return urlopen(url).read().decode('utf-8')
        except HTTPError as e:
            if e.code != 403:
                raise e
            logger.debug(f'Fetch of {url} failed with 403, assuming rate limit, retrying after period {backoff} seconds.')
        time.sleep(backoff)
        backoff *= 1.5
    return urlopen(url).read().decode('utf-8')

def github_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.version
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        logger.debug('Skip non-github url "%s"' % repo.url)
        raise RuntimeError('can not handle non github urls')

    release_tag = repo.get_release_tag(pkg_name)

    if not repo.has_remote_tag(release_tag):
        raise RuntimeError('specified tag "%s" is not a git tag' % release_tag)

    url = 'https://raw.githubusercontent.com/%s/%s/%s' % (path, release_tag, filepath)
    try:
        logger.debug('Load %s file from url "%s"' % (filepath, url))
        # TODO(tfoote) magic number for testing
        return '\n'.join( _get_url_contents(url).splitlines()[:100])
    except HTTPError as e:
        if e.code == 404:
            logger.debug('- File not found (%s), trying "%s"' % (e, url))
            return 'Missing'
        logger.debug('- HTTP ERROR (%s), trying "%s"' % (e, url))
        raise e
    except URLError as e:
        logger.debug('- failed (%s), trying "%s"' % (e, url))
        raise RuntimeError()


def github_source_manifest_provider(repo, filepaths=['CHANGELOG.rst', 'README.md']):
    server, path = repo.get_url_parts()
    if not server.endswith('github.com'):
        logger.debug('Skip non-github url "%s"' % repo.url)
        raise RuntimeError('can not handle non github urls')

    tree_url = 'https://api.github.com/repos/%s/git/trees/%s?recursive=1' % (path, repo.version)
    req = Request(tree_url)
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    if GITHUB_USER and GITHUB_PASSWORD:
        logger.debug('- using http basic auth from supplied environment variables.')
        credential_pair = '%s:%s' % (GITHUB_USER, GITHUB_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        req.add_header('Authorization', authheader)
    try:
        tree_json = json.loads(_get_url_contents(req))
        logger.debug('- load repo tree from %s' % tree_url)
    except URLError as e:
        raise RuntimeError('Unable to fetch JSON tree from %s: %s' % (tree_url, e))

    if tree_json['truncated']:
        raise RuntimeError('JSON tree is truncated, must perform full clone.')

    package_xml_paths = set()
    for obj in tree_json['tree']:
        if obj['path'].split('/')[-1] == 'package.xml': # Actually package.xml to find packages instead of filepath
            package_xml_paths.add(os.path.dirname(obj['path']))

    # TODO(tfoote) This is not correct for non-package.xml
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

    cache = SourceRepositoryCache.from_ref(tree_json['sha'])
    for package_xml_path in package_xml_paths:
        package_xml_filename = 'package.xml'
        url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
            (path, cache.ref(), package_xml_path + '/' + package_xml_filename if package_xml_path else package_xml_filename)
        logger.debug('- load %s from %s' % (package_xml_filename, url))
        package_xml = _get_url_contents(url)
        name = parse_package_string(package_xml).name
        logger.debug(f'==== Package xml added for {name}')
        cache.add(name, package_xml_path, package_xml, package_xml_filename)
        for filepath in filepaths:
            url = 'https://raw.githubusercontent.com/%s/%s/%s' % \
                (path, cache.ref(), package_xml_path + '/' + filepath if package_xml_path else filepath)
            logger.debug('- load %s from %s' % (filepath, url))
            try:
                contents = _get_url_contents(url)
            except HTTPError as e:
                if e.code == 404:
                    logger.debug('- Recording Missing (%s), hit error "%s"' % (url, e))
                    contents = 'Missing'
                else:
                    logger.debug('- HTTP ERROR (%s), trying "%s"' % (e, url))
                    raise e
            cache.add(name, package_xml_path, contents, filepath)

    return cache
