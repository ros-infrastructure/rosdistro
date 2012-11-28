#!/usr/bin/env python

import yaml
import urllib2

class DevelDistro:
    def __init__(self, name):
        url = urllib2.urlopen('https://raw.github.com/ros/rosdistro/master/releases/%s-devel.yaml'%name)
        distro = yaml.load(url.read())['repositories']
        self.repositories = {}
        for name, data in distro.iteritems():
            repo = DevelDistroRepo(name, data)
            self.repositories[name] = repo



class DevelDistroRepo:
    def __init__(self, name, data):
        self.name = name
        self.type = data['type']
        self.url = data['url']
        self.version = None
        if data.has_key('version'):
            self.version = data['version']

    def get_rosinstall(self):
        if self.version:
            return yaml.dump([{self.type: {'local-name': self.name, 'uri': '%s'%self.url, 'version': '%s'%self.version}}], default_style=False)
        else:
            return yaml.dump([{self.type: {'local-name': self.name, 'uri': '%s'%self.url}}], default_style=False)



