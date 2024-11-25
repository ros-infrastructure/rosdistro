import os
import sys
try:
    from urllib.parse import urljoin
    from urllib.request import pathname2url
except ImportError:
    from urlparse import urljoin
    from urllib import pathname2url

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    'src'))

def path_to_url(path):
    return urljoin('file:', pathname2url(path))
