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

from xml.dom import minidom


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
