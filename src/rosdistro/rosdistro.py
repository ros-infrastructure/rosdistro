#!/usr/bin/env python

import yaml
import urllib2
import os
import sys
from catkin_pkg import package as catkin_pkg


class RosDistro:
    def __init__(self, name):
        self.distro_file = RosDistroFile(name)
        self.depends_file = RosDependencies(name)


    def get_repositories(self):
        return self.distro_file.repositories

    def get_packages(self):
        return self.distro_file.packages


    def get_rosinstall(self, name, version=None):
        if self.distro_file.repositories.has_key(name):
            return self.distro_file.repositories[name].get_rosinstall(version)
        elif self.distro_file.packages.has_key(name):
            return self.distro_file.packages[name].get_rosinstall(version)


    def get_depends_one(self, name):
        pkgs = []
        if self.distro_file.repositories.has_key(name):
            pkgs = self.distro_file.repositories[name].packages
        elif self.distro_file.packages.has_key(name):
            pkgs = [self.distro_file.packages[name]]

        deps = {'build': [], 'test': [], 'run': []}
        for p in pkgs:
            d = self.depends_file.get_dependencies(p.repository, p.name)
            for t in deps.keys():
                for dep in d[t]:
                    deps[t].append(dep)
        return deps


class RosDistroFile:
    def __init__(self, name):
        self.packages = {}
        self.repositories = {}

        # parse ros distro file
        distro_url = urllib2.urlopen('https://raw.github.com/ros/rosdistro/master/releases/%s.yaml'%name)
        distro = yaml.load(distro_url.read())['repositories']

        # loop over all repo's
        for repo_name, data in distro.iteritems():
            if data['version']:
                repo = RosRepository(repo_name, data['version'], data['url'])
                self.repositories[repo_name] = repo
                if not data.has_key('packages'):   # support unary disto's
                    data['packages'] = {repo_name: ''}

                # loop over all packages
                for pkg_name in data['packages'].keys():
                    pkg = RosPackage(pkg_name, repo)
                    repo.packages.append(pkg)
                    self.packages[pkg_name] = pkg



class RosRepository:
    def __init__(self, name, version, url):
        self.name = name
        self.version = version
        self.url = url
        self.packages = []

    def get_rosinstall(self, version=None):
        return "\n".join([p.get_rosinstall(version) for p in self.packages])



class RosPackage:
    def __init__(self, name, repository):
        self.name = name
        self.repository = repository

    def get_rosinstall(self, version=None):
        if not version:
            version = self.repository.version

        if version == 'master':
            return yaml.dump([{'git': {'local-name': self.name,
                                       'uri': self.repository.url,
                                       'version': '/'.join(['release', self.name])}}],
                             default_style=False)

        else:
            return yaml.safe_dump([{'git': {'local-name': self.name,
                                            'uri': self.repository.url,
                                            'version': '/'.join(['release', self.name, self.repository.version])}}],
                                  default_style=False)



class RosDependencies:
    def __init__(self, name):
        # url's
        self.local_url = '/home/wim/.ros/%s-dependencies.yaml'%name
        self.server_url = 'https://raw.github.com/ros/rosdistro/master/releases/%s-dependencies.yaml'%name
        self.dependencies = {}

        # initialize with the local cache
        deps = self._read_local_cache()
        for key, value in deps.iteritems():
            self.dependencies[key] = value


    def get_dependencies(self, repo, package):
        key = '%s?%s?%s'%(repo.name, repo.version, package)

        # check in memory first
        if self.dependencies.has_key(key):
            return self.dependencies[key]

        # read server cache if needed
        if self.cache != 'server':
            deps = self._read_server_cache()
            for key, value in deps.iteritems():
                self.dependencies[key] = value
            self._write_local_cache()
            if self.dependencies.has_key(key):
                return self.dependencies[key]


        # retrieve dependencies
        deps = retrieve_dependencies(repo)
        self.dependencies[key] = deps
        self._write_local_cache()
        return deps



    def _read_server_cache(self):
        try:
            print "Reading dependency cache on server..."
            self.cache = 'server'
            return yaml.load(urllib2.urlopen(self.server_url).read())
        except:
            print "   No dependency cache found on server."
            return {}



    def _read_local_cache(self):
        try:
            print "Reading local dependency cache..."
            self.cache = 'local'
            with open(self.local_url)  as f:
                deps = yaml.safe_load(f.read())
                if not deps:
                    raise
                return deps
        except:
            print "   No local dependency cache found."
            return {}


    def _write_local_cache(self):
        try:
            print "Writing local dependency cache..."
            with open(self.local_url, 'w')  as f:
                yaml.dump(self.dependencies, f)
                print "   Wrote local dependency cache"
        except:
            print "   Failed to write local dependency cache"







def retrieve_dependencies(repo):
    print "Retrieve dependencies of package %s"%repo.name
    if 'github' in repo.url:
        for p in repo.packages:
            url = repo.url
            url = url.replace('.git', '/release/%s/%s/package.xml'%(p.name, repo.version.split('-')[0]))
            url = url.replace('git://', 'https://')
            url = url.replace('https://', 'https://raw.')
            print url
            package_xml = urllib2.urlopen(url).read()
            return get_package_dependencies(package_xml)



def get_package_dependencies(package_xml):
    print "Parse package.xml"


    pkg = catkin_pkg.parse_package_string(package_xml)
    depends1 = {'build': [d.name for d in pkg.build_depends],
                'test':  [d.name for d in pkg.test_depends],
                'run':  [d.name for d in pkg.run_depends]}
    return depends1





# command line tools
def main():
    distro  = RosDistro('groovy')

    for p in distro.get_packages():
        try:
            distro.get_depends_one(p)
        except:
            print "Fail on %s"%p






if __name__ == "__main__":
    main()
