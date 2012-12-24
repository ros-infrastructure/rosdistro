#!/usr/bin/env python

import yaml
import urllib2
import os
import sys
from rospkg import environment
import copy
import threading

RES_DICT = {'build': [], 'buildtool': [], 'test': [], 'run': []}
RES_TREE = {'build': {}, 'buildtool': {}, 'test': {}, 'run': {}}
CACHE_VERSION = 1

class RosDistro:
    def __init__(self, name, cache_location=None):
        self.depends_on1_cache = copy.deepcopy(RES_TREE)
        t1 = threading.Thread(target=self._construct_rosdistro_file, args=(name,))
        t2 = threading.Thread(target=self._construct_rosdistro_dependencies, args=(name, cache_location,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    
    def _construct_rosdistro_file(self, name):
        self.distro_file = RosDistroFile(name)

    def _construct_rosdistro_dependencies(self, name, cache_location):
        self.depends_file = RosDependencies(name, cache_location)

    def get_repositories(self):
        return self.distro_file.repositories

    def get_repository(self, repo):
        return self.get_repositories()[repo]

    def get_packages(self):
        return self.distro_file.packages

    def get_package(self, pkg):
        return self.get_packages()[pkg]

    def get_rosinstall(self, items, version='last_release', source='vcs'):
        rosinstall = ""
        for p in self._convert_to_pkg_list(items):
            rosinstall += p.get_rosinstall(version, source)
        return rosinstall


    def _get_depends_on1(self, package_name):
        if self.depends_on1_cache.has_key(package_name):
            return self.depends_on1_cache[package_name]
        res = copy.deepcopy(RES_DICT)
        for pkg in self.get_packages():
            for key, depends in self._get_depends1(pkg).iteritems():
                if package_name in depends:
                    res[key].append(pkg)
        self.depends_on1_cache[package_name] = res
        return res


    def get_depends_on1(self, items):
        return self.get_depends_on(items, 1)


    def get_depends_on(self, items, depth=0):
        res = copy.deepcopy(RES_DICT)
        for p in self._convert_to_pkg_list(items):
            for dep_type, dep_list in res.iteritems():
                self._get_depends_on_recursive(p.name, dep_type, dep_list, depth, 1)
        return res


    def _get_depends_on_recursive(self, package_name, dep_type, res, depth, curr_depth):
        deps_on = self._get_depends_on1(package_name)

        # merge and recurse
        for d in deps_on[dep_type]:
            if not d in res:
                res.append(d)
                if depth == 0 or curr_depth < depth:
                    self._get_depends_on_recursive(d, dep_type, res, depth, curr_depth+1)




    def _get_depends1(self, package_name):
        p = self.distro_file.packages[package_name]
        return self.depends_file.get_dependencies(p.repository, p.name)


    def get_depends1(self, items):
        return self.get_depends(items, 1)

    def get_depends(self, items, depth=0):
        res = copy.deepcopy(RES_DICT)
        for p in self._convert_to_pkg_list(items):
            for dep_type, dep_list in res.iteritems():
                self._get_depends_recursive(p.name, dep_type, dep_list, depth, 1)
        return res


    def _get_depends_recursive(self, package_name, dep_type, res, depth, curr_depth):
        deps1 = self._get_depends1(package_name)

        # merge and recurse
        for d in deps1[dep_type]:
            if not d in res:
                res.append(d)
                if depth == 0 or curr_depth < depth:
                    if d in self.get_packages():  # recurse on packages only
                        self._get_depends_recursive(d, dep_type, res, depth, curr_depth+1)





    def _convert_to_pkg_list(self, items):
        if type(items) != list:
            items = [items]
        pkgs = []
        for i in items:
            if self.distro_file.repositories.has_key(i):
                for p in self.distro_file.repositories[i].packages:
                    if not p in pkgs:
                        pkgs.append(p)
            elif self.distro_file.packages.has_key(i):
                if not self.distro_file.packages[i] in pkgs:
                    pkgs.append(self.distro_file.packages[i])
            else:
                print "!!! %s is not a package name nor a repository name"%i
                raise Exception()
        return pkgs






class RosDistroFile:
    def __init__(self, name):
        self.packages = {}
        self.repositories = {}

        # parse ros distro file
        distro_url = urllib2.urlopen('https://raw.github.com/ros/rosdistro/master/releases/%s.yaml'%name)
        distro = yaml.load(distro_url.read())['repositories']

        # loop over all repo's
        for repo_name, data in distro.iteritems():
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

    def get_rosinstall(self, version, source):
        return "\n".join([p.get_rosinstall(version, source) for p in self.packages])



class RosPackage:
    def __init__(self, name, repository):
        self.name = name
        self.repository = repository

    def get_rosinstall(self, version, source):
        # can't get last release of unreleased repository
        if version == 'last_release' and not self.repository.version:
            print "Can't get the last release of unreleased repository %s"%self.repository.name
            raise

        # set specific version of last release of needed
        if version == 'last_release':
            version = self.repository.version.split('-')[0]

        # generate the rosinstall file
        if version == 'master':
            return yaml.dump([{'git': {'local-name': self.name,
                                       'uri': self.repository.url,
                                       'version': '/'.join(['release', self.name])}}],
                             default_style=False)

        else:
            if source == 'vcs':
                return yaml.safe_dump([{'git': {'local-name': self.name,
                                                'uri': self.repository.url,
                                                'version': '/'.join(['release', self.name, version])}}],
                                      default_style=False)
            elif source == 'tar':
                return yaml.safe_dump([{'tar': {'local-name': self.name,
                                                'uri': self.repository.url.replace('git://', 'https://').replace('.git', '/archive/release/%s/%s.tar.gz'%(self.name, version)),
                                                'version': '%s-release-release-%s-%s'%(self.repository.name, self.name, version)}}],
                                      default_style=False)
            else:
                print "Invalid source type %s"%source
                raise



class RosDependencies:
    def __init__(self, name, cache_location):
        # url's
        if cache_location:
            self.local_url = os.path.join(cache_location, '%s-dependencies.yaml'%name)
        else:
            self.local_url = os.path.join(environment.get_ros_home(), '%s-dependencies.yaml'%name)
        self.server_url = 'http://www.ros.org/rosdistro/%s-dependencies.yaml'%name
        self.dependencies = {}

        # initialize with the local or server cache
        deps = self._read_local_cache()
        if deps == {}:
            deps = self._read_server_cache()            
        for key, value in deps.iteritems():
            self.dependencies[key] = value
        if self.cache == 'server':
            self._write_local_cache()
            

    def get_dependencies(self, repo, package):
        # support unreleased stacks
        if not repo.version:
            return copy.deepcopy(RES_DICT)

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
        try:
            self.cache = 'server'
            deps = yaml.load(urllib2.urlopen(self.server_url).read())
            if not deps or not 'cache_version' in deps or deps['cache_version'] != CACHE_VERSION or not 'repositories' in deps:
                raise
            return deps['repositories']
        except:
            return {}



    def _read_local_cache(self):
        try:
            self.cache = 'local'
            with open(self.local_url)  as f:
                deps = yaml.safe_load(f.read())
                if not deps or not 'cache_version' in deps or deps['cache_version'] != CACHE_VERSION or not 'repositories' in deps:
                    raise
                return deps['repositories']
        except:
            return {}


    def _write_local_cache(self):
        try:
            with open(self.local_url, 'w')  as f:
                yaml.dump({'cache_version': CACHE_VERSION,
                           'repositories': self.dependencies},
                          f)
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
    else:
        print "Non-github repositories are net yet supported by the rosdistro tool"
        raise Exception()



def get_package_dependencies(package_xml):
    if not os.path.abspath("/usr/lib/pymodules/python2.7") in sys.path:
        sys.path.append("/usr/lib/pymodules/python2.7")
    from catkin_pkg import package as catkin_pkg

    pkg = catkin_pkg.parse_package_string(package_xml)
    depends1 = {'build': [d.name for d in pkg.build_depends],
                'buildtool':  [d.name for d in pkg.buildtool_depends],
                'test':  [d.name for d in pkg.test_depends],
                'run':  [d.name for d in pkg.run_depends]}
    return depends1



