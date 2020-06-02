import os
import re
import fmf
import tmt
import shutil
import click
import tmt.steps.discover

# Regular expressions for beakerlib libraries
LIBRARY_REGEXP_RPM = re.compile(r'^library\(([^)]+)\)$')
LIBRARY_REGEXP_FMF = re.compile(r'^library({[^}]+})$')

# Default beakerlib libraries location
DEFAULT_REPOSITORY = 'https://github.com/beakerlib-libraries'

class DiscoverFmf(tmt.steps.discover.DiscoverPlugin):
    """
    Discover available tests from fmf metadata

    By default all available tests from the current repository are used
    so the minimal configuration looks like this:

        discover:
            how: fmf

    Full config example:

        discover:
            how: fmf
            url: https://github.com/psss/tmt
            ref: master
            path: /fmf/root
            test: /tests/basic
            filter: 'tier: 1'
    """

    # Supported methods
    _methods = [tmt.steps.Method(name='fmf', doc=__doc__, order=50)]

    @classmethod
    def options(cls, how=None):
        """ Prepare command line options for given method """
        return [
            click.option(
                '-u', '--url', metavar='REPOSITORY',
                help='URL of the git repository with fmf metadata.'),
            click.option(
                '-r', '--ref', metavar='REVISION',
                help='Branch, tag or commit specifying the git revision.'),
            click.option(
                '-p', '--path', metavar='ROOT',
                help='Path to the metadata tree root.'),
            click.option(
                '-t', '--test', metavar='NAMES', multiple=True,
                help='Select tests by name.'),
            click.option(
                '-F', '--filter', metavar='FILTERS', multiple=True,
                help='Include only tests matching the filter.'),
            ] + super().options(how)

    def default(self, option, default=None):
        """ Return default data for given option """
        # Git revision defaults to master if url provided
        if option == 'ref' and self.get('url'):
            return 'master'
        # No other defaults available
        return default

    def show(self):
        """ Show discover details """
        super().show(['url', 'ref', 'path', 'test', 'filter'])

    def wake(self):
        """ Wake up the plugin (override data with command line) """

        # Handle backward-compatible stuff
        if 'repository' in self.data:
            self.data['url'] = self.data.pop('repository')
        if 'revision' in self.data:
            self.data['ref'] = self.data.pop('revision')

        # Make sure that 'filter' and 'test' keys are lists
        for key in ['filter', 'test']:
            if key in self.data and not isinstance(self.data[key], list):
                self.data[key] = [self.data[key]]

        # Process command line options, apply defaults
        for option in ['url', 'ref', 'path', 'test', 'filter']:
            value = self.opt(option)
            if value:
                self.data[option] = value

    def fetch_library(self, identifier):
        """
        Fetch beakerlib library for given identifier

        Handle both basic 'library(component/lib)' syntax and full fmf
        identifier. Clones library repository, checks for library
        requires and fetches possible other dependent libraries.
        Returns list of packages required by the library/libraries.
        """
        # Prepare url, ref and name
        if isinstance(identifier, str):
            component, name = identifier.split('/')
            url = os.path.join(DEFAULT_REPOSITORY, component)
            ref = 'master'
        elif isinstance(identifier, dict):
            url = identifier.get('url')
            ref = identifier.get('ref', 'master')
            name = identifier.get('name', 'main')
            try:
                component = re.search(r'/([^/]+?)(/|\.git)?$', url).group(1)
            except:
                raise tmt.utils.DiscoverError(
                    f"Unable to parse component from '{url}'.")

        # Fetch the repository
        directory = os.path.join(self.workdir, 'libs', component)
        if os.path.isdir(directory):
            self.debug(f"Library '{identifier}' already fetched.")
        else:
            self.run(['git', 'clone', url, directory], shell=False)
            self.run(['git', 'checkout', ref], shell=False, cwd=directory)

        # Check library metadata for requires
        tree = fmf.Tree(directory)
        library = tree.find(f"/{name}")
        requires = tmt.utils.listify(library.get('require', []))
        return self.check_library_require(requires)

    def check_library_require(self, requires):
        """
        Check test requires for libraries

        Fetch all identified libraries, process their requires and
        return only regular package requires extended with possible
        additional requires aggregated from fetched libraries.
        """
        processed_requires = []
        for require in requires:
            require = require.strip()
            rpm_require = LIBRARY_REGEXP_RPM.search(require)
            fmf_require = LIBRARY_REGEXP_FMF.search(require)
            # Basic library syntax: library(httpd/http)
            if rpm_require:
                identifier = rpm_require.group(1)
            # Full fmf identifier: library{url: "...", name: "..."}
            elif fmf_require:
                identifier = tmt.utils.yaml_to_dict(fmf_require.group(1))
            # No further processing for regular package requires
            else:
                processed_requires.append(require)
                continue
            # Fetch the library, add its requires
            self.debug(f"Fetch beakerlib library '{identifier}'.")
            processed_requires.extend(self.fetch_library(identifier))
        return processed_requires

    def go(self):
        """ Discover available tests """
        super(DiscoverFmf, self).go()

        # Check url and path, prepare test directory
        url = self.get('url')
        path = self.get('path')
        testdir = os.path.join(self.workdir, 'tests')

        # Clone provided git repository (if url given)
        if url:
            self.info('url', url, 'green')
            self.debug(f"Clone '{url}' to '{testdir}'.")
            self.run(f'git clone {url} {testdir}')
        # Copy git repository root to workdir
        else:
            if path and not os.path.isdir(path):
                raise tmt.utils.DiscoverError(
                    f"Provided path '{path}' is not a directory.")
            fmf_root = path or self.step.plan.run.tree.root
            # Check git repository root (use fmf root if not found)
            try:
                output = self.run(
                    'git rev-parse --show-toplevel', cwd=fmf_root, dry=True)
                git_root = output[0].strip('\n')
            except tmt.utils.RunError:
                self.debug(f"Git root not found, using '{fmf_root}.'")
                git_root = fmf_root
            # Set path to relative path from the git root to fmf root
            path = os.path.relpath(fmf_root, git_root)
            self.info('directory', git_root, 'green')
            self.debug(f"Copy '{git_root}' to '{testdir}'.")
            if not self.opt('dry'):
                shutil.copytree(git_root, testdir)

        # Checkout revision if requested
        ref = self.get('ref')
        if ref:
            self.info('ref', ref, 'green')
            self.debug(f"Checkout ref '{ref}'.")
            self.run(f"git checkout -f {ref}", cwd=testdir)

        # Adjust path and optionally show
        if path is None or path == '.':
            path = ''
        else:
            self.info('path', path, 'green')

        # Prepare the whole tree path and test path prefix
        tree_path = os.path.join(testdir, path.lstrip('/'))
        if not os.path.isdir(tree_path) and not self.opt('dry'):
            raise tmt.utils.DiscoverError(
                f"Metadata tree path '{path}' not found.")
        prefix_path = os.path.join('/tests', path.lstrip('/'))

        # Show filters and test names if provided
        filters = self.get('filter', [])
        for filter_ in filters:
            self.info('filter', filter_, 'green')
        names = self.get('test', [])
        if names:
            self.info('names', fmf.utils.listed(names), 'green')

        # Initialize the metadata tree, search for available tests
        self.debug(f"Check metadata tree in '{tree_path}'.")
        if self.opt('dry'):
            self._tests = []
            return
        self._tests = tmt.Tree(tree_path).tests(filters=filters, names=names)

        # Prefix tests and handle library requires
        for test in self._tests:
            # Prefix test path with 'tests' and possible 'path' prefix
            test.path = os.path.join(prefix_path, test.path.lstrip('/'))
            # Process the special library requires 'library(component/lib)'
            if test.require:
                test.require = self.check_library_require(test.require)

    def tests(self):
        """ Return all discovered tests """
        return self._tests
