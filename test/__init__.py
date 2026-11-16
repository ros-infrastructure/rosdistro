import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    'src'))


def path_to_url(path):
    return Path(os.path.abspath(path)).as_uri()
