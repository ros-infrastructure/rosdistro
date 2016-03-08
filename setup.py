#!/usr/bin/env python

import os
from setuptools import setup, find_packages
import sys

exec(open(os.path.join(os.path.dirname(__file__), 'src', 'rosdistro', '_version.py')).read())

install_requires = ['catkin_pkg', 'rospkg', 'PyYAML', 'setuptools']
if sys.version_info[0] == 2 and sys.version_info[1] < 7:
    install_requires.append('argparse')

setup(
    name='rosdistro',
    version=__version__,
    install_requires=install_requires,
    packages=find_packages('src'),
    package_dir={'': 'src'},
    scripts=[
#        'scripts/rosdistro',
        'scripts/rosdistro_build_cache',
        'scripts/rosdistro_freeze_source',
#        'scripts/rosdistro_convert',
#        'scripts/rosdistro_generate_cache',
        'scripts/rosdistro_migrate_to_rep_141',
        'scripts/rosdistro_migrate_to_rep_143',
        'scripts/rosdistro_reformat'
    ],
    author='Wim Meeussen, Dirk Thomas',
    author_email='wim@hidof.com, dthomas@osrfoundation.org',
    maintainer='Dirk Thomas',
    maintainer_email='dthomas@osrfoundation.org',
    url='http://wiki.ros.org/rosdistro',
    download_url='http://download.ros.org/downloads/rosdistro/',
    keywords=['ROS'],
    classifiers=['Programming Language :: Python',
                 'License :: OSI Approved :: BSD License',
                 'License :: OSI Approved :: MIT License'],
    description="A tool to work with rosdistro files",
    long_description="""A tool to work with rosdistro files""",
    license='BSD, MIT'
)
