# Copyright (c) 2013-2018 NASK. All rights reserved.

import glob
import os
import os.path as osp
import sys

from setuptools import setup, find_packages


setup_dir = osp.dirname(osp.abspath(__file__))
venv_dir = os.environ.get('VIRTUAL_ENV')

with open(osp.join(setup_dir, '.n6-version')) as f:
    n6_version = f.read().strip()

with open(osp.join(setup_dir, '.n6sdk-version')) as f:
    n6sdk_version = f.read().strip()


def setup_data_line_generator(filename_base):
    path_base = osp.join(setup_dir, filename_base)
    path_glob = path_base + '*'
    for path in glob.glob(path_glob):
        with open(path) as f:
            for raw_line in f:
                yield raw_line.strip()


pip_install = False
setup_install = False
requirements = ['n6sdk==' + n6sdk_version]
requirements_pip = []
dep_links = []
for line in setup_data_line_generator('requirements'):
    if line == '# pip install section':
        pip_install = True
        setup_install = False
    elif line == '# setuptools install section':
        setup_install = True
        pip_install = False
    if not line or line.startswith('#'):
        continue
    if setup_install:
        req = line.split('\t')
        requirements.append(req[0])
        try:
            dep_links.append(req[1])
        except IndexError:
            pass
    elif pip_install:
        requirements_pip.append(line)


setup(
    name="n6lib",
    version=n6_version,

    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
    tests_require=["mock==1.0.1", "unittest_expander"],
    test_suite="n6lib.tests",
    dependency_links=dep_links,
    install_requires=requirements,
    entry_points={
      'console_scripts': [
        'n6create_and_initialize_auth_db = n6lib.auth_db.scripts:create_and_initialize_auth_db',
        'n6drop_auth_db = n6lib.auth_db.scripts:drop_auth_db',
        'n6populate_auth_db = n6lib.auth_db.scripts:populate_auth_db',
      ],
    },

    description='The library of common *n6* modules',
    url='https://github.com/CERT-Polska/n6',
    maintainer='CERT Polska',
    maintainer_email='n6@cert.pl',
    classifiers=[
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Topic :: Security',
    ],
    keywords='n6 network incident exchange',
)

for pkgname in requirements_pip:
    if venv_dir:
        command = '{}/bin/pip install {}'.format(venv_dir, pkgname)
    else:
        command = '/usr/bin/pip install {}'.format(pkgname)
    if os.system(command):
        sys.exit('exiting after error when executing {}'.format(command))
