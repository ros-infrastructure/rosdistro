#!/usr/bin/env python

import os
import sys

from setuptools import find_packages, setup

kwargs = {
    'name': 'rosdistro',
    # same version as in:
    # - src/rosdistro/__init__.py
    # - stdeb.cfg
    'version': '0.8.1',
    'install_requires': ['PyYAML', 'setuptools'],
    'packages': find_packages('src'),
    'package_dir': {'': 'src'},
    'scripts': [
        # 'scripts/rosdistro',
        'scripts/rosdistro_build_cache',
        'scripts/rosdistro_freeze_source',
        # 'scripts/rosdistro_convert',
        # 'scripts/rosdistro_generate_cache',
        'scripts/rosdistro_migrate_to_rep_141',
        'scripts/rosdistro_migrate_to_rep_143',
        'scripts/rosdistro_reformat'
    ],
    'author': 'Wim Meeussen, Dirk Thomas',
    'author_email': 'wim@hidof.com, dthomas@osrfoundation.org',
    'maintainer': 'Dirk Thomas',
    'maintainer_email': 'dthomas@osrfoundation.org',
    'url': 'http://wiki.ros.org/rosdistro',
    'keywords': ['ROS'],
    'classifiers': [
        'Programming Language :: Python',
        'License :: OSI Approved :: BSD License',
        'License :: OSI Approved :: MIT License'],
    'description': 'A tool to work with rosdistro files',
    'long_description': 'A tool to work with rosdistro files',
    'license': 'BSD, MIT'
}

if sys.version_info[0] == 2 and sys.version_info[1] < 7:
    kwargs['install_requires'].append('argparse')

if 'SKIP_PYTHON_MODULES' in os.environ:
    kwargs['packages'] = []
    kwargs['package_dir'] = {}
elif 'SKIP_PYTHON_SCRIPTS' in os.environ:
    kwargs['name'] += '_modules'
    kwargs['scripts'] = []
else:
    kwargs['install_requires'] += ['catkin_pkg', 'rospkg']

setup(**kwargs)
