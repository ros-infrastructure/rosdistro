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


    def get_depends_on1(self, name):
        tree = self._build_full_dependency_tree()
        res = {'build': [], 'test': [], 'run': []}
        for key in res:
            for pkg, depends in tree[key].iteritems():
                if name in depends and not pkg in res[key]:
                    res[key].append(pkg)
        return res



    def get_depends_on(self, name):
        res = {'build': [], 'test': [], 'run': []}
        for dep_type, dep_list in res.iteritems():
            self._get_depends_on_recursive(name, dep_type, dep_list)
        return res


    def _get_depends_on_recursive(self, name, dep_type, res):
        # get dependencies_on of name
        deps_on = self.get_depends_on1(name)

        # merge and recurse
        for d in deps_on[dep_type]:
            if not d in res:
                res.append(d)
                self._get_depends_recursive(d, dep_type, res)


    def _build_full_dependency_tree(self):
        tree = {'build': {}, 'test': {}, 'run': {}}
        for p in self.get_packages():
            try:
                deps1 = self.get_depends1(p)
                for key, deps_list in deps1.iteritems():
                    tree[key][p] = deps_list
            except:
                print "Could not find dependencies of package %s"%p
        return tree



    def get_depends1(self, name):
        pkgs = []
        if self.distro_file.repositories.has_key(name):
            pkgs = self.distro_file.repositories[name].packages
        elif self.distro_file.packages.has_key(name):
            pkgs = [self.distro_file.packages[name]]

        res = {'build': [], 'test': [], 'run': []}
        for p in pkgs:
            d = self.depends_file.get_dependencies(p.repository, p.name)
            for t in res:
                for dep in d[t]:
                    res[t].append(dep)
        return res


    def get_depends(self, name):
        res = {'build': [], 'test': [], 'run': []}
        for dep_type, dep_list in res.iteritems():
            self._get_depends_recursive(name, dep_type, dep_list)
        return res



    def _get_depends_recursive(self, name, dep_type, res):
        # get dependencies of name
        deps1 = self.get_depends1(name)

        # merge and recurse
        for d in deps1[dep_type]:
            if not d in res:
                res.append(d)
                self._get_depends_recursive(d, dep_type, res)



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
        deps = retrieve_dependencies(repo, package)
        self.dependencies[key] = deps
        self._write_local_cache()
        return deps



    def _read_server_cache(self):
        return {}
        try:
            self.cache = 'server'
            return yaml.load(urllib2.urlopen(self.server_url).read())
        except:
            return {}



    def _read_local_cache(self):
        try:
            self.cache = 'local'
            with open(self.local_url)  as f:
                deps = yaml.safe_load(f.read())
                if not deps:
                    raise
                return deps
        except:
            return {}


    def _write_local_cache(self):
        try:
            with open(self.local_url, 'w')  as f:
                yaml.dump(self.dependencies, f)
        except:
            print "Failed to write local dependency cache"







def retrieve_dependencies(repo, package):
    if 'github' in repo.url:
        url = repo.url
        url = url.replace('.git', '/release/%s/%s/package.xml'%(package, repo.version.split('-')[0]))
        url = url.replace('git://', 'https://')
        url = url.replace('https://', 'https://raw.')
        package_xml = urllib2.urlopen(url).read()
        return get_package_dependencies(package_xml)



def get_package_dependencies(package_xml):
    pkg = catkin_pkg.parse_package_string(package_xml)
    depends1 = {'build': [d.name for d in pkg.build_depends],
                'test':  [d.name for d in pkg.test_depends],
                'run':  [d.name for d in pkg.run_depends]}
    return depends1





# command line tools
def main():
    distro  = RosDistro('groovy')
    print "Depends1 tf"
    print distro.get_depends1('tf')
    print "Depends tf"
    print distro.get_depends('tf')
    print "Depends on 1 tf"
    print distro.get_depends_on1('tf')
    print "Depends on tf"
    print distro.get_depends_on('tf')

    for p in distro.get_packages():
        print "Dependencies of %s:"%p
        print distro.get_depends_on(p)






if __name__ == "__main__":
    main()
