#!/usr/bin/env python

import yaml
import urllib2
import os
import sys
from rospkg import environment

class RosDistro:
    def __init__(self, name):
        self.distro_file = RosDistroFile(name)
        self.depends_file = RosDependencies(name)


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


    def get_depends_on1(self, items):
        tree = self._build_full_dependency_tree()
        res = {'build': [], 'test': [], 'run': []}
        for key in res:
            for pkg, depends in tree[key].iteritems():
                for p in self._convert_to_pkg_list(items):
                    if p.name in depends and not pkg in res[key]:
                        res[key].append(pkg)
        return res



    def get_depends_on(self, items):
        res = {'build': [], 'test': [], 'run': []}
        for dep_type, dep_list in res.iteritems():
            for p in self._convert_to_pkg_list(items):
                self._get_depends_on_recursive(p.name, dep_type, dep_list)
        return res


    def get_depends1(self, items):
        res = {'build': [], 'test': [], 'run': []}
        for p in self._convert_to_pkg_list(items):
            d = self.depends_file.get_dependencies(p.repository, p.name)
            for t in res:
                for dep in d[t]:
                    res[t].append(dep)
        return res


    def get_depends(self, items):
        res = {'build': [], 'test': [], 'run': []}
        for dep_type, dep_list in res.iteritems():
            for p in self._convert_to_pkg_list(items):
                self._get_depends_recursive(p.name, dep_type, dep_list)
        return res



    def _get_depends_recursive(self, pkg, dep_type, res):
        # get dependencies of pkg
        deps1 = self.get_depends1(pkg)

        # merge and recurse
        for d in deps1[dep_type]:
            if not d in res:
                res.append(d)
                self._get_depends_recursive(d, dep_type, res)



    def _get_depends_on_recursive(self, pkg, dep_type, res):
        # get dependencies_on of pgk
        deps_on = self.get_depends_on1(pkg)

        # merge and recurse
        for d in deps_on[dep_type]:
            if not d in res:
                res.append(d)
                self._get_depends_recursive(d, dep_type, res)




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
        return pkgs



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
    def __init__(self, name):
        # url's
        self.local_url = os.path.join(environment.get_ros_home(), '%s-dependencies.yaml'%name)
        self.server_url = 'https://raw.github.com/ros/rosdistro/master/releases/%s-dependencies.yaml'%name
        self.dependencies = {}

        # initialize with the local cache
        deps = self._read_local_cache()
        for key, value in deps.iteritems():
            self.dependencies[key] = value


    def get_dependencies(self, repo, package):
        # support unreleased stacks
        if not repo.version:
            return {'build': [], 'test': [], 'run': []}

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
    if not os.path.abspath("/usr/lib/pymodules/python2.7") in sys.path:
        sys.path.append("/usr/lib/pymodules/python2.7")
    from catkin_pkg import package as catkin_pkg

    pkg = catkin_pkg.parse_package_string(package_xml)
    depends1 = {'build': [d.name for d in pkg.build_depends],
                'test':  [d.name for d in pkg.test_depends],
                'run':  [d.name for d in pkg.run_depends]}
    return depends1





# tests
def main():
    if len(sys.argv) != 2:
        print "Usage: %s ros_distro"%sys.argv[0]
        return

    # touch everything to create the new cache
    distro  = RosDistro(sys.argv[1])
    distro._build_full_dependency_tree()

    print "Cache written to %s"%(os.path.join(environment.get_ros_home(), '%s-dependencies.yaml'%sys.argv[1]))

if __name__ == "__main__":
    main()
