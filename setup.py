#!/usr/bin/env python

from setuptools import setup, find_packages

setup(
    name='rosdistro',
    version='0.1.17',
    packages=[
        'rosdistro',
        'rosdistro.manifest_provider'
    ],
    package_dir = {
        'rosdistro': 'src/rosdistro',
        'rosdistro.manifest_provider': 'src/rosdistro/manifest_provider'
    },
    scripts = [
        'scripts/rosdistro',
        'scripts/rosdistro_build_cache',
        'scripts/rosdistro_convert',
        'scripts/rosdistro_generate_cache'
    ],
    install_requires=['empy', 'PyYAML', 'argparse', 'rospkg', 'distribute'],
    package_data = {'rosdistro': ['resources/templates/*']},
    author='Wim Meeussen',
    author_email='wim@hidof.com',
    maintainer='Dirk Thomas',
    maintainer_email='dthomas@osrfoundation.org',
    url='http://www.ros.org/wiki/rosdistro',
    download_url='http://pr.willowgarage.com/downloads/rosdistro/',
    keywords=['ROS'],
    classifiers=['Programming Language :: Python',
                 'License :: OSI Approved :: BSD License'],
    description="A tool to work with rosdistro files",
    long_description="""A tool to work with rosdistro files""",
    license='BSD'
)
