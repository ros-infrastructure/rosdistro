#!/usr/bin/env python

import os
from setuptools import setup, find_packages

exec(open(os.path.join(os.path.dirname(__file__), 'src', 'rosdistro', '_version.py')).read())

setup(
    name='rosdistro',
    version=__version__,
    install_requires=['argparse', 'catkin_pkg', 'distribute', 'rospkg', 'PyYAML'],
    packages=find_packages('src'),
    package_dir={'': 'src'},
    scripts=[
#        'scripts/rosdistro',
        'scripts/rosdistro_build_cache',
#        'scripts/rosdistro_convert',
#        'scripts/rosdistro_generate_cache',
        'scripts/rosdistro_migrate_to_rep_141',
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
