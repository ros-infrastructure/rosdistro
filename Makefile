.PHONY: all setup clean_dist distro clean install

NAME=rosdistro
VERSION=`./setup.py --version`

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

clean: clean_dist
	echo "clean"

install: distro
	sudo checkinstall python setup.py install

testsetup:
	echo "running rosdistro! tests"

test: testsetup
	python setup.py nosetests

test--pdb-failures: testsetup
	python setup.py nosetests --pdb-failures
