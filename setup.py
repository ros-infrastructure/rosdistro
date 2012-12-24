#!/usr/bin/env python

from setuptools import setup, find_packages

setup(
    name='rosdistro',
    version='0.0.17',
    packages=['rosdistro'],
    package_dir = {'rosdistro':'src/rosdistro'},
    scripts = ['scripts/rosdistro_generate_cache', 'scripts/rosdistro'],
    install_requires=['empy', 'PyYAML', 'argparse', 'rospkg', 'distribute'],
    package_data = {'rosdistro': ['resources/templates/*']},
    author='Wim Meeussen',
    author_email='wim@hidof.com',
    maintainer='Wim Meeussen',
    maintainer_email='wim@hidof.com',
    url='http://www.ros.org/wiki/rosdistro',
    download_url='http://pr.willowgarage.com/downloads/rosdistro/',
    keywords=['ROS'],
    classifiers=['Programming Language :: Python',
                 'License :: OSI Approved :: BSD License'],
    description="A tool to work with rosdistro files",
    long_description="""A tool to work with rosdistro files""",
    license='BSD'
)
