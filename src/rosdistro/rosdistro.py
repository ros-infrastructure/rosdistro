#!/usr/bin/env python

import yaml
import urllib2
import urllib
import os
import sys
from rospkg import environment
import copy
import threading
import tarfile

RES_DICT = {'build': [], 'buildtool': [], 'test': [], 'run': []}
RES_TREE = {'build': {}, 'buildtool': {}, 'test': {}, 'run': {}}
CACHE_VERSION = 1
MAIN_ROSDIST = 'http://raw.github.com/ros/rosdistro/master'

walks = {
    'FULL_WALK': {'build': ['build', 'run', 'buildtool', 'test'],
                  'run': ['build', 'run', 'buildtool', 'test'],
                  'buildtool': ['build', 'run', 'buildtool', 'test'],
                  'test': ['build', 'run', 'buildtool', 'test']},
    'SPIRAL_OF_DOOM': {'build': ['run'],
                       'run': ['buildtool'],
                       'buildtool': ['test'],
                       'test': ['build']}

}

def invert_dict(d):
    inverted = {}
    for key,value in d.iteritems():
        for v in value:
            v_keys = inverted.setdefault(v, [])
            if key not in v_keys:
                v_keys.append(key)
    return inverted

class RosDistro:
    def __init__(self, name, cache_location=None, rosdist_rep=MAIN_ROSDIST, dont_load_deps=False):
        self.depends_on1_cache = copy.deepcopy(RES_TREE)
        self.name = name
        mf = MasterFile(rosdist_rep=rosdist_rep)
        if dont_load_deps:
            self._construct_rosdistro_file(name, mf)
        else:
            t1 = threading.Thread(target=self._construct_rosdistro_file, args=(name,mf,))
            t2 = threading.Thread(target=self._construct_rosdistro_dependencies, args=(name, cache_location, mf,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

    def _construct_rosdistro_file(self, name, master_file):
        self.distro_file = RosDistroFile(name, master_file=master_file)

    def _construct_rosdistro_dependencies(self, name, cache_location, master_file):
        self.depends_file = RosDependencies(name, cache_location, master_file=master_file)

    def get_repositories(self):
        return self.distro_file.repositories

    def get_repository(self, repo):
        return self.get_repositories()[repo]

    def get_packages(self):
        return self.distro_file.packages

    def get_package(self, pkg):
        return self.get_packages()[pkg]

    def get_version(self, pkg):
        return self.get_package(pkg).repository.version

    def get_targets(self):
        return [t for t in self.distro_file.targets]

    def get_arches(self, target=None):
        if target is None:
            return self.distro_file.targets
        else:
            return self.distro_file.targets[target]

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


    def get_depends_on(self, items, depth=0, dep_dict=walks['FULL_WALK']):
        res = copy.deepcopy(RES_DICT)
        for p in self._convert_to_pkg_list(items):
            for dep_type, dep_list in res.iteritems():
                self._get_depends_on_recursive(p.name, dep_type, invert_dict(dep_dict), dep_list, depth, 1)
        return res


    def _get_depends_on_recursive(self, package_name, dep_type, dep_dict, res, depth, curr_depth):
        deps_on = self._get_depends_on1(package_name)

        # merge and recurse
        for d in deps_on[dep_type]:
            if not d in res:
                res.append(d)
                if depth == 0 or curr_depth < depth:
                    for next_dep_type in dep_dict[dep_type]:
                        self._get_depends_on_recursive(d, next_dep_type, dep_dict, res, depth, curr_depth+1)


    def get_maintainers(self, package_name):
        return self._get_depends1(package_name)['maintainers']

    def _get_depends1(self, package_name):
        p = self.distro_file.packages[package_name]
        return self.depends_file.get_dependencies(p.repository, p.name)


    def get_depends1(self, items):
        return self.get_depends(items, 1)

    def get_depends(self, items, depth=0, dep_dict=walks['FULL_WALK']):
        res = copy.deepcopy(RES_DICT)
        for p in self._convert_to_pkg_list(items):
            for dep_type, dep_list in res.iteritems():
                self._get_depends_recursive(p.name, dep_type, dep_dict, dep_list, depth, 1)
        return res

    def _get_depends_recursive(self, package_name, dep_type, dep_dict, res, depth, curr_depth):
        deps1 = self._get_depends1(package_name)

        # merge and recurse
        for d in deps1[dep_type]:
            if not d in res:
                res.append(d)
                if depth == 0 or curr_depth < depth:
                    if d in self.get_packages():  # recurse on packages only
                        for next_dep_type in dep_dict[dep_type]:
                            #print "Recursing on %s for type %s"%(d, dep_type)
                            self._get_depends_recursive(d, next_dep_type, dep_dict, res, depth, curr_depth+1)

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




class MasterFile:
    def __init__(self, rosdist_rep=MAIN_ROSDIST):
        self._rosdist_rep = rosdist_rep
        try:
            mf_cts = yaml.load(urllib2.urlopen("%s/rosdistros.yaml"%rosdist_rep))
        except urllib2.URLError as e:
            print("Could not load rosdistros file: %s"%str(e))
            raise
        self._version = mf_cts['version']
        self._distros = mf_cts['distros']

    def get_distro_url(self, dist, kind='release'):
        return "%s/%s" % (self._rosdist_rep, self._distros[dist][kind])

    def get_distro(self, dist, kind='release'):
        url = self.get_distro_url(dist, kind)
        try:
            dist_file = yaml.load(urllib2.urlopen(url))
        except urllib2.URLError as e:
            print("Could not load distro file: %s"%str(e))
            raise
        return dist_file

    def get_deps_server_cache_url(self, dist):
        if 'deps_cache' not in self._distros[dist]:
            return None
        return self._distros[dist]['deps_cache']

    def get_distros(self):
        return self._distros.keys()

    def get_targets(self):
        tgts = {}
        for d in self.get_distros():
            tgts[d] = self.get_distro(d)['targets'].keys()
        return tgts




class RosDistroFile:
    def __init__(self, name, master_file=None):
        self.packages = {}
        self.repositories = {}

        if master_file is None:
            mf = MasterFile()
        else:
            mf = master_file
        dist = mf.get_distro(name)
        distro = dist['repositories']
        self.targets = dist['targets']

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
    def __init__(self, name, cache_location, master_file=None):
        # url's
        if master_file is None:
            mf = MasterFile()
        else:
            mf = master_file
        self.file_name = '%s-dependencies.yaml'%name
        if cache_location:
            self.local_url = os.path.join(cache_location, self.file_name)
        else:
            self.local_url = os.path.join(environment.get_ros_home(), self.file_name)
        self.server_url = mf.get_deps_server_cache_url(name)
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
            tar_file = urllib.urlretrieve(self.server_url)
        except Exception, e:
            print "Failed to read server cache"
            return {}
        tar = tarfile.open(tar_file[0], 'r')
        data = tar.extractfile(self.file_name)
        deps = yaml.load(data.read())
        if not deps or not 'cache_version' in deps or deps['cache_version'] != CACHE_VERSION or not 'repositories' in deps:
            raise
        return deps['repositories']



    def _read_local_cache(self):
        try:
            self.cache = 'local'
            with open(self.local_url)  as f:
                deps = yaml.safe_load(f.read())
                if not deps or not 'cache_version' in deps or deps['cache_version'] != CACHE_VERSION or not 'repositories' in deps:
                    raise
                return deps['repositories']
        except Exception, e:
            return {}


    def _write_local_cache(self):
        try:
            with open(self.local_url, 'w')  as f:
                yaml.dump({'cache_version': CACHE_VERSION,
                           'repositories': self.dependencies},
                          f)
        except Exception, e:
            print "Failed to write local dependency cache"





def retrieve_dependencies(repo, package):
    if 'github' in repo.url:
        url = repo.url
        url = url.replace('.git', '/release/%s/%s/package.xml'%(package, repo.version.split('-')[0]))
        url = url.replace('git://', 'https://')
        url = url.replace('https://', 'https://raw.')
        try:
            package_xml = urllib2.urlopen(url).read()
        except Exception, e:
            print "Failed to read package.xml file from url %s"%url
            import time
            print "Trying again in a split second..."
            time.sleep(.5)
            try:
                package_xml = urllib2.urlopen(url).read()
            except Exception, e:
                print("Nope. Github y u no let me fetch? %s" % str(e))
                raise Exception()
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
                'run':  [d.name for d in pkg.run_depends],
                'maintainers': [{'name': m.name, 'email': m.email} for m in pkg.maintainers]}
    return depends1



