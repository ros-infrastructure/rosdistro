import urllib2


class AptDistro:
    def __init__(self, ubuntudistro, arch, shadow=True):
        if shadow:
            url = 'http://packages.ros.org/ros-shadow-fixed/ubuntu/dists/{0}/main/binary-{1}/Packages'
            url = urllib2.urlopen(url.format(ubuntudistro, arch))
        else:
            url = 'http://packages.ros.org/ros/ubuntu/dists/{0}/main/binary-{1}/Packages'
            url = urllib2.urlopen(url.format(ubuntudistro, arch))
        self.dep = {}
        package = None
        for l in url.read().split('\n'):
            if 'Package: ' in l:
                package = l.split('Package: ')[1]
            if 'Depends: ' in l:
                if not package:
                    raise RuntimeError("Found 'depends' but not 'package' while parsing the apt repository index file")
                self.dep[package] = [d.split(' ')[0] for d in (l.split('Depends: ')[1].split(', '))]
                package = None

    def has_package(self, package):
        return package in self.dep

    def depends1(self, package):
        return self.depends(package, one=True)

    def depends(self, package, res=[], one=False):
        if package in self.dep:
            for d in self.dep[package]:
                if not d in res:
                    res.append(d)
                if not one:
                    self.depends(d, res, one)
        return res

    def depends_on1(self, package):
        return self.depends_on(package, one=True)

    def depends_on(self, package, res=[], one=False):
        for p, dep in self.dep.iteritems():
            if package in dep:
                if not p in res:
                    res.append(p)
                if not one:
                    self.depends_on(p, res, one)
        return res
