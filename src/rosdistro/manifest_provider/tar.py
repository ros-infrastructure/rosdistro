# Software License Agreement (BSD License)
#
# Copyright (c) 2020, Canonical Ltd.
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
#  * Neither the name of Canonical Ltd. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
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
import io
import os
import tarfile
import tempfile
import urllib
from urllib.request import urlopen, Request

from catkin_pkg.package import InvalidPackage, parse_package_string
from catkin_pkg.packages import find_package_paths

from rosdistro.common import rmtree
from rosdistro.manifest_provider.cache import sanitize_and_truncate_docs
from rosdistro.source_repository_cache import SourceRepositoryCache
from rosdistro import logger

_TAR_USER = os.getenv('TAR_USER', None)
_TAR_PASSWORD = os.getenv('TAR_PASSWORD', None)

def tar_manifest_provider(_dist_name, repo, pkg_name, filepath='package.xml'):
    assert repo.type == 'tar'

    subdir = repo.get_release_tag(pkg_name)

    request = Request(repo.url)
    if _TAR_USER and _TAR_PASSWORD:
        logger.debug('- using http basic auth from supplied environment variables.')
        credential_pair = '%s:%s' % (_TAR_USER, _TAR_PASSWORD)
        authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
        request.add_header('Authorization', authheader)
    elif _TAR_PASSWORD:
        logger.debug('- using private token auth from supplied environment variables.')
        request.add_header('Private-Token', _TAR_PASSWORD)

    response = urlopen(request)
    with tarfile.open(fileobj=io.BytesIO(response.read())) as tar:
        try:
            extracted = tar.extractfile(subdir + '/' + filepath)
        except KeyError:
            raise RuntimeError('Could not find %s in tarball "%s"' % (subdir + '/' + filepath, repo.url))
        if extracted is None:
            raise RuntimeError('Could not extract %s from tarball "%s"' % (subdir + '/' + filepath, repo.url))
        package_xml = extracted.read()
        return package_xml.decode('utf-8')


def tar_source_manifest_provider(repo, filepaths=['CHANGELOG.rst', 'README.md']):
    assert repo.type == 'tar'
    xmlpath = 'package.xml'

    try:
        request = Request(repo.url)
        if _TAR_USER and _TAR_PASSWORD:
            logger.debug('- using http basic auth from supplied environment variables.')
            credential_pair = '%s:%s' % (_TAR_USER, _TAR_PASSWORD)
            authheader = 'Basic %s' % base64.b64encode(credential_pair.encode()).decode()
            request.add_header('Authorization', authheader)
        elif _TAR_PASSWORD:
            logger.debug('- using private token auth from supplied environment variables.')
            request.add_header('Private-Token', _TAR_PASSWORD)

        response = urlopen(request)
        with tarfile.open(fileobj=io.BytesIO(response.read())) as tar:
            tmpdir = tempfile.mkdtemp()
            try:
                # Extract the package.xmls and requested filepaths
                tar.extractall(path=tmpdir, members=_package_xml_members(tar, filepaths))
                cache = SourceRepositoryCache.from_ref(None)

                for package_path in find_package_paths(tmpdir):
                    if package_path == '.':
                        package_path = ''
                    with open(os.path.join(tmpdir, package_path, xmlpath), 'r') as f:
                        package_xml = f.read()
                    try:
                        name = parse_package_string(package_xml).name
                    except InvalidPackage:
                        raise RuntimeError('Unable to parse package.xml file found in %s' % repo.url)
                    cache.add(name, package_path, package_xml, xmlpath)
                    for filepath in filepaths:
                        if filepath == xmlpath:
                            continue
                        repo_filename = os.path.join(tmpdir, package_path, filepath)
                        if not os.path.isfile(repo_filename):
                            logger.debug(f'- tar load of {filepath} from {repo.url} skipped because it did not exist.')
                            continue
                        with open(repo_filename, 'r') as f:
                            logger.debug('- tar load %s from %s' % (filepath, repo_filename))
                            contents = f.read()
                            contents = sanitize_and_truncate_docs(contents)
                            cache.add(name, package_path, contents, filepath)

                return cache
            finally:
                rmtree(tmpdir)
    except Exception as e:
        raise RuntimeError('Unable to fetch source package.xml files: %s' % e)


def _package_xml_members(tar, filepaths=None):
    target_filenames = {'package.xml'}
    if filepaths:
        target_filenames.update(filepaths)
    for tarinfo in tar:
        if os.path.basename(tarinfo.name) in target_filenames or any(
            tarinfo.name == fp or tarinfo.name.endswith('/' + fp) for fp in target_filenames
        ):
            yield tarinfo
