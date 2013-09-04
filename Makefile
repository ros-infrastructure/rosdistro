.PHONY: all setup clean_dist distro clean install upload push

NAME=rosdistro
VERSION=`./setup.py --version`

CHANGENAME=rosdistro

OUTPUT_DIR=deb_dist

USERNAME ?= $(shell whoami)

UNAME := $(shell uname)

.PHONY: doc
doc:
	python setup.py build_sphinx
ifeq ($(UNAME),Darwin)
	@open doc/build/html/index.html
else
	@echo "Not opening index.html on $(UNAME)"
endif

all:
	echo "noop for debbuild"

setup:
	echo "building version ${VERSION}"

clean_dist:
	-rm -f MANIFEST
	-rm -rf dist
	-rm -rf deb_dist

distro: setup clean_dist
	python setup.py sdist

push: distro
	python setup.py sdist register upload
	scp dist/${NAME}-${VERSION}.tar.gz ros@ftp-osl.osuosl.org:/home/ros/data/download.ros.org/downloads/${NAME}

clean: clean_dist
	echo "clean"

install: distro
	sudo checkinstall python setup.py install

deb_dist:
	python setup.py --command-packages=stdeb.command sdist_dsc --workaround-548392=False bdist_deb

upload-packages: deb_dist
	dput -u -c dput.cf all-shadow-fixed ${OUTPUT_DIR}/${CHANGENAME}_${VERSION}-1_amd64.changes
	dput -u -c dput.cf all-ros ${OUTPUT_DIR}/${CHANGENAME}_${VERSION}-1_amd64.changes

upload-building: deb_dist
	dput -u -c dput.cf all-building ${OUTPUT_DIR}/${CHANGENAME}_${VERSION}-1_amd64.changes

upload: upload-building upload-packages

testsetup:
	echo "running rosdistro! tests"

test: testsetup
	python setup.py nosetests

test--pdb-failures: testsetup
	python setup.py nosetests --pdb-failures
