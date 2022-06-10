#!/usr/bin/env python

import os

from setuptools import find_packages, setup

kwargs = {
    'name': 'rosdistro',
    # same version as in:
    # - src/rosdistro/__init__.py
    # - stdeb.cfg
    'version': '0.9.0',
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
    'extras_require': {
        'test': [
            "mock; python_version < '3.3'",
            'pytest',
        ]},
    'author': 'Wim Meeussen, Dirk Thomas',
    'author_email': 'wim@hidof.com, dthomas@osrfoundation.org',
    'maintainer': 'ROS Infrastructure Team',
    'url': 'http://wiki.ros.org/rosdistro',
    'project_urls': {
        'Source code':
        'https://github.com/ros-infrastructure/rosdistro',
        'Issue tracker':
        'https://github.com/ros-infrastructure/rosdistro/issues',
    },
    'keywords': ['ROS'],
    'classifiers': [
        'Programming Language :: Python',
        'License :: OSI Approved :: BSD License',
        'License :: OSI Approved :: MIT License'],
    'description': 'A tool to work with rosdistro files',
    'long_description': 'A tool to work with rosdistro files',
    'license': 'BSD, MIT'
}

if 'SKIP_PYTHON_MODULES' in os.environ:
    kwargs['packages'] = []
    kwargs['package_dir'] = {}
elif 'SKIP_PYTHON_SCRIPTS' in os.environ:
    kwargs['name'] += '_modules'
    kwargs['scripts'] = []
else:
    kwargs['install_requires'] += ['catkin_pkg', 'rospkg']

setup(**kwargs)
