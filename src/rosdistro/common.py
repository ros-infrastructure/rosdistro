from errno import EACCES, EPERM
import os
from pathlib import Path
import shutil
import stat
import sys
from urllib.parse import urlparse
from urllib.request import url2pathname

_quiet = False


def quiet(state=True):
    global _quiet
    _quiet = state


def _print_func(msg, end, file):
    global _quiet
    if not _quiet:
        print(msg, end=end, file=file)


def override_print(print_func=_print_func):
    global _print
    _print = print_func


_print = _print_func


def info(msg, end=None, file=None):
    global _print
    _print(msg, end=end, file=file)


def warning(msg, end=None, file=None):
    global _print
    _print(msg, end=end, file=file)


def error(msg, end=None, file=None):
    global _print
    file = sys.stderr if file is None else file
    _print(msg, end=end, file=file)


def rmtree(path):
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['onerror'] = _onerror_windows
    return shutil.rmtree(path, **kwargs)


def _onerror_windows(function, path, excinfo):
    if isinstance(excinfo[1], OSError) and excinfo[1].errno in (EACCES, EPERM):
        os.chmod(path, stat.S_IWRITE)
        function(path)


def path_to_url(path):
    return Path(os.path.abspath(path)).as_uri()


def url_to_path(url):
    parsed = urlparse(url)
    if parsed.scheme.lower() != 'file':
        raise ValueError('Not a file:// URL: %s' % url)
    if parsed.netloc and parsed.netloc.lower() != 'localhost':
        raw_path = '//' + parsed.netloc + parsed.path
    else:
        raw_path = parsed.path
    return url2pathname(raw_path)
