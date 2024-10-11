#!/usr/bin/env python3

import os

from setuptools import find_packages, setup

kwargs = {
    'name': 'rosdistro',
    # same version as in:
    # - src/rosdistro/__init__.py
    # - stdeb.cfg
    'version': '1.0.0',
    'install_requires': ['PyYAML', 'setuptools'],
    'python_requires': '>=3.6',
    'packages': find_packages('src'),
    'package_dir': {'': 'src'},
    'entry_points': {
        'console_scripts': [
            # 'rosdistro = rosdistro.cli.rosdistro:main',
            'rosdistro_build_cache = rosdistro.cli.rosdistro_build_cache:main',
            'rosdistro_freeze_source = rosdistro.cli.rosdistro_freeze_source:main',
            # 'rosdistro_convert = rosdistro.cli.rosdistro_convert:main',
            # 'rosdistro_generate_cache = rosdistro.cli.rosdistro_generate_cache:main',
            'rosdistro_migrate_to_rep_141 = rosdistro.cli.rosdistro_migrate_to_rep_141:main',
            'rosdistro_migrate_to_rep_143 = rosdistro.cli.rosdistro_migrate_to_rep_143:main',
            'rosdistro_reformat = rosdistro.cli.rosdistro_reformat:main'
        ]},
    'extras_require': {
        'test': [
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
    kwargs['entry_points'] = {}
else:
    kwargs['install_requires'] += ['catkin_pkg', 'rospkg']

setup(**kwargs)
