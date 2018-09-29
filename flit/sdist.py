from collections import defaultdict
from copy import copy
from gzip import GzipFile
import io
import logging
import os
from pathlib import Path
from posixpath import join as pjoin
from pprint import pformat
import tarfile

from flit import common, inifile
from flit.common import VCSError
from flit.vcs import identify_vcs

log = logging.getLogger(__name__)

SETUP = """\
#!/usr/bin/env python
# setup.py generated by flit for tools that don't yet use PEP 517

from distutils.core import setup

{before}
setup(name={name!r},
      version={version!r},
      description={description!r},
      author={author!r},
      author_email={author_email!r},
      url={url!r},
      {extra}
     )
"""

PKG_INFO = """\
Metadata-Version: 1.1
Name: {name}
Version: {version}
Summary: {summary}
Home-page: {home_page}
Author: {author}
Author-email: {author_email}
"""


def auto_packages(pkgdir: str):
    """Discover subpackages and package_data"""
    pkgdir = os.path.normpath(pkgdir)
    pkg_name = os.path.basename(pkgdir)
    pkg_data = defaultdict(list)
    # Undocumented distutils feature: the empty string matches all package names
    pkg_data[''].append('*')
    packages = [pkg_name]
    subpkg_paths = set()

    def find_nearest_pkg(rel_path):
        parts = rel_path.split(os.sep)
        for i in reversed(range(1, len(parts))):
            ancestor = '/'.join(parts[:i])
            if ancestor in subpkg_paths:
                pkg = '.'.join([pkg_name] + parts[:i])
                return pkg, '/'.join(parts[i:])

        # Relative to the top-level package
        return pkg_name, rel_path

    for path, dirnames, filenames in os.walk(pkgdir, topdown=True):
        if os.path.basename(path) == '__pycache__':
            continue

        from_top_level = os.path.relpath(path, pkgdir)
        if from_top_level == '.':
            continue

        is_subpkg = '__init__.py' in filenames
        if is_subpkg:
            subpkg_paths.add(from_top_level)
            parts = from_top_level.split(os.sep)
            packages.append('.'.join([pkg_name] + parts))
        else:
            pkg, from_nearest_pkg = find_nearest_pkg(from_top_level)
            pkg_data[pkg].append(pjoin(from_nearest_pkg, '*'))

    # Sort values in pkg_data
    pkg_data = {k: sorted(v) for (k, v) in pkg_data.items()}

    return sorted(packages), pkg_data

def _parse_req(requires_dist):
    """Parse "Foo (v); python_version == '2.x'" from Requires-Dist

    Returns pip-style appropriate for requirements.txt.
    """
    if ';' in requires_dist:
        name_version, env_mark = requires_dist.split(';', 1)
        env_mark = env_mark.strip()
    else:
        name_version, env_mark = requires_dist, None

    if '(' in name_version:
        # turn 'name (X)' and 'name (<X.Y)'
        # into 'name == X' and 'name < X.Y'
        name, version = name_version.split('(', 1)
        name = name.strip()
        version = version.replace(')', '').strip()
        if not any(c in version for c in '=<>'):
            version = '==' + version
        name_version = name + version

    return name_version, env_mark

def convert_requires(reqs_by_extra):
    """Regroup requirements by (extra, env_mark)"""
    grouping = defaultdict(list)
    for extra, reqs in reqs_by_extra.items():
        for req in reqs:
            name_version, env_mark = _parse_req(req)
            grouping[(extra, env_mark)].append(name_version)

    install_reqs = grouping.pop(('.none',  None), [])
    extra_reqs = {}
    for (extra, env_mark), reqs in grouping.items():
        if extra == '.none':
            extra = ''
        if env_mark is None:
            extra_reqs[extra] = reqs
        else:
            extra_reqs[extra + ':' + env_mark] = reqs

    return install_reqs, extra_reqs

def include_path(p):
    return not (p.startswith('dist' + os.sep)
                or (os.sep+'__pycache__' in p)
                or p.endswith('.pyc'))

def clean_tarinfo(ti, mtime=None):
    """Clean metadata from a TarInfo object to make it more reproducible.

    - Set uid & gid to 0
    - Set uname and gname to ""
    - Normalise permissions to 644 or 755
    - Set mtime if not None
    """
    ti = copy(ti)
    ti.uid = 0
    ti.gid = 0
    ti.uname = ''
    ti.gname = ''
    ti.mode = common.normalize_file_permissions(ti.mode)
    if mtime is not None:
        ti.mtime = mtime
    return ti


class SdistBuilder:
    def __init__(self, ini_path=Path('flit.ini')):
        self.ini_path = ini_path
        self.ini_info = inifile.read_pkg_ini(ini_path)
        self.module = common.Module(self.ini_info['module'], ini_path.parent)
        self.metadata = common.make_metadata(self.module, self.ini_info)
        self.srcdir = ini_path.parent

    def prep_entry_points(self):
        # Reformat entry points from dict-of-dicts to dict-of-lists
        res = defaultdict(list)
        for groupname, group in self.ini_info['entrypoints'].items():
            for name, ep in sorted(group.items()):
                res[groupname].append('{} = {}'.format(name, ep))

        return dict(res)

    def find_tracked_files(self):
        vcs_mod = identify_vcs(self.srcdir)
        untracked_deleted = vcs_mod.list_untracked_deleted_files(self.srcdir)
        if list(filter(include_path, untracked_deleted)):
            raise VCSError("Untracked or deleted files in the source directory. "
                           "Commit, undo or ignore these files in your VCS.",
                           self.srcdir)

        files = vcs_mod.list_tracked_files(self.srcdir)
        log.info("Found %d files tracked in %s", len(files), vcs_mod.name)
        return sorted(filter(include_path, files))

    def make_setup_py(self):
        before, extra = [], []
        if self.module.is_package:
            packages, package_data = auto_packages(str(self.module.path))
            before.append("packages = \\\n%s\n" % pformat(sorted(packages)))
            before.append("package_data = \\\n%s\n" % pformat(package_data))
            extra.append("packages=packages,")
            extra.append("package_data=package_data,")
        else:
            extra.append("py_modules={!r},".format([self.module.name]))

        install_reqs, extra_reqs = convert_requires(self.ini_info['reqs_by_extra'])
        if install_reqs:
            before.append("install_requires = \\\n%s\n" % pformat(install_reqs))
            extra.append("install_requires=install_requires,")
        if extra_reqs:
            before.append("extras_require = \\\n%s\n" % pformat(extra_reqs))
            extra.append("extras_require=extras_require,")

        entrypoints = self.prep_entry_points()
        if entrypoints:
            before.append("entry_points = \\\n%s\n" % pformat(entrypoints))
            extra.append("entry_points=entry_points,")

        if self.metadata.requires_python:
            extra.append('python_requires=%r,' % self.metadata.requires_python)

        return SETUP.format(
            before='\n'.join(before),
            name=self.metadata.name,
            version=self.metadata.version,
            description=self.metadata.summary,
            author=self.metadata.author,
            author_email=self.metadata.author_email,
            url=self.metadata.home_page,
            extra='\n      '.join(extra),
        ).encode('utf-8')

    def build(self, target_dir:Path =None):
        if target_dir is None:
            target_dir = self.ini_path.parent / 'dist'
        if not target_dir.exists():
            target_dir.mkdir(parents=True)
        target = target_dir / '{}-{}.tar.gz'.format(
                        self.metadata.name, self.metadata.version)
        source_date_epoch = os.environ.get('SOURCE_DATE_EPOCH', '')
        mtime = int(source_date_epoch) if source_date_epoch else None
        gz = GzipFile(str(target), mode='wb', mtime=mtime)
        tf = tarfile.TarFile(str(target), mode='w', fileobj=gz,
                             format=tarfile.PAX_FORMAT)

        try:
            tf_dir = '{}-{}'.format(self.metadata.name, self.metadata.version)

            files_to_add = self.find_tracked_files()

            for relpath in files_to_add:
                path = self.srcdir / relpath
                ti = tf.gettarinfo(str(path), arcname=pjoin(tf_dir, relpath))
                ti = clean_tarinfo(ti, mtime)

                if ti.isreg():
                    with path.open('rb') as f:
                        tf.addfile(ti, f)
                else:
                    tf.addfile(ti)  # Symlinks & ?

            if 'setup.py' in files_to_add:
                log.warning("Using setup.py from repository, not generating setup.py")
            else:
                setup_py = self.make_setup_py()
                log.info("Writing generated setup.py")
                ti = tarfile.TarInfo(pjoin(tf_dir, 'setup.py'))
                ti.size = len(setup_py)
                tf.addfile(ti, io.BytesIO(setup_py))

            pkg_info = PKG_INFO.format(
                name=self.metadata.name,
                version=self.metadata.version,
                summary=self.metadata.summary,
                home_page=self.metadata.home_page,
                author=self.metadata.author,
                author_email=self.metadata.author_email,
            ).encode('utf-8')
            ti = tarfile.TarInfo(pjoin(tf_dir, 'PKG-INFO'))
            ti.size = len(pkg_info)
            tf.addfile(ti, io.BytesIO(pkg_info))

        finally:
            tf.close()
            gz.close()

        log.info("Built sdist: %s", target)
        return target
