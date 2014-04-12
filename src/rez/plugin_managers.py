"""
Manages loading of all types of Rez plugins.
"""
from rez import module_root_path
from rez.settings import settings
from rez.util import LazySingleton
import logging
import os.path
import sys
import re

if sys.version_info < (2, 7):
    from rez.backport.null_handler import NullHandler
else:
    from logging import NullHandler

# modified from pkgutil standard library
def extend_path(path, name):
    """Extend a package's path.

    Intended use is to place the following code in a package's __init__.py:

        from pkgutil import extend_path
        __path__ = extend_path(__path__, __name__)

    This will add to the package's __path__ all subdirectories of
    directories on sys.path named after the package.  This is useful
    if one wants to distribute different parts of a single logical
    package as multiple directories.

    It also looks for *.pkg files beginning where * matches the name
    argument.  This feature is similar to *.pth files (see site.py),
    except that it doesn't special-case lines starting with 'import'.
    A *.pkg file is trusted at face value: apart from checking for
    duplicates, all entries found in a *.pkg file are added to the
    path, regardless of whether they are exist the filesystem.  (This
    is a feature.)

    If the input path is not a list (as is the case for frozen
    packages) it is returned unchanged.  The input path is not
    modified; an extended copy is returned.  Items are only appended
    to the copy at the end.

    It is assumed that sys.path is a sequence.  Items of sys.path that
    are not (unicode or 8-bit) strings referring to existing
    directories are ignored.  Unicode items of sys.path that cause
    errors when used as filenames may cause this function to raise an
    exception (in line with os.path.isdir() behavior).
    """

    if not isinstance(path, list):
        # This could happen e.g. when this is called from inside a
        # frozen package.  Return the path unchanged in that case.
        return path

    pname = os.path.join(*name.split('.')) # Reconstitute as relative path
    # Just in case os.extsep != '.'
    sname = os.extsep.join(name.split('.'))
    sname_pkg = sname + os.extsep + "pkg"
    init_py = "__init__" + os.extsep + "py"

    path = path[:] # Start with a copy of the existing path

    for dir in settings.get("plugin_path"):
        if not isinstance(dir, basestring) or not os.path.isdir(dir):
            continue
        subdir = os.path.join(dir, pname)
        # XXX This may still add duplicate entries to path on
        # case-insensitive filesystems
        initfile = os.path.join(subdir, init_py)
        if subdir not in path and os.path.isfile(initfile):
            path.append(subdir)
        # XXX Is this the right thing for subpackages like zope.app?
        # It looks for a file named "zope.app.pkg"
        pkgfile = os.path.join(dir, sname_pkg)
        if os.path.isfile(pkgfile):
            try:
                f = open(pkgfile)
            except IOError, msg:
                sys.stderr.write("Can't open %s: %s\n" %
                                 (pkgfile, msg))
            else:
                for line in f:
                    line = line.rstrip('\n')
                    if not line or line.startswith('#'):
                        continue
                    path.append(line) # Don't check for existence!
                f.close()

    return path

class RezPluginType(object):
    """An abstract base class representing a single type of plugin.

    'type_name' must correspond with one of the source directories found under
    the 'plugins' directory.
    """
    type_name = None

    def __init__(self):
        if self.type_name is None:
            raise TypeError("Subclasses of RezPluginType must provide a "
                            "'type_name' attribute")
        self.pretty_type_name = self.type_name.replace('_', ' ')
        self.plugin_classes = {}
        self.load_plugins()

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self.plugin_classes.keys())

    def register_plugin(self, plugin_name, plugin_class):
        self.plugin_classes[plugin_name] = plugin_class

    def load_plugins(self):
        import pkgutil
        type_module_name = 'rezplugins.' + self.type_name
        __import__(type_module_name, globals(), locals(), [], -1)
        package = sys.modules[type_module_name]
        # this path has already been extended to existing directories on the plugin
        # search path
        for loader, modname, ispkg in pkgutil.walk_packages(package.__path__,
                                                            package.__name__ + '.'):
            if loader is not None:
                plugin_name = modname.split('.')[-1]
                if plugin_name.startswith('_'):
                    continue
                module = loader.find_module(modname).load_module(modname)
                try:
                    if hasattr(module, 'register_plugin') and \
                            hasattr(module.register_plugin, '__call__'):
                        plugin_class = module.register_plugin()
                        self.register_plugin(plugin_name, plugin_class)
                    else:
                        # delete from sys.modules?
                        pass
                except:
                    import traceback
                    traceback.print_exc()

    def get_plugin_class(self, plugin_name):
        """Returns the class registered under the given plugin name."""
        try:
            return self.plugin_classes[plugin_name]
        except KeyError:
            # TODO add a PluginManagerError
            raise ValueError("Unrecognised %s plugin: '%s'"
                             % (self.pretty_type_name, plugin_name))

    def create_instance(self, plugin, **instance_kwargs):
        """Create and return an instance of the given plugin."""
        return self.get_plugin_class(plugin)(**instance_kwargs)


class RezPluginManager(object):
    """
    Primary interface for working with registered plugins.

    Custom plugins are organized under a python package named 'rezplugins'.
    The direct sub-packages of 'rezplugins' are the known plugin types supported by
    rez, and the modules under that are indivudal custom plugins extending that
    type.

    For example, rez provides 2 plugins of type 'build_system'::

        rezplugins/
          __init__.py
          build_system/
            __init__.py
            cmake.py
            make.py

    If you would like to provide your own build system plugin, create a similar
    directory structure, placing your plugin module into the appropriate sub-package::

        rezplugins/
          __init__.py
          build_system/
            __init__.py
            scons.py

    In your plugin module, 'scons.py' in this example, add a 'register_plugin' function
    that returns the class for your function::

        def register_plugin():
            return SCons

    Next, use the `rez settings` command to configure the `plugin_path` to point
    to the directory above *your* 'rezplugins' directory. Alternately, you may
    set the REZ_PLUGIN_PATH environment variable. If multiple directories
    are added to the search path, they will all be merged into a single
    'rezplugins' python package.
    see `pkgutil <https://docs.python.org/2/library/pkgutil.html>` for more info.
    """
    def __init__(self):
        self._plugin_types = {}

    # -- plugin types

    def _get_plugin_type(self, plugin_type):
        try:
            return self._plugin_types[plugin_type]()
        except KeyError:
            # TODO add a PluginManagerError
            raise ValueError("Unrecognised plugin type: '%s'" % (plugin_type))

    def register_plugin_type(self, type_class):
        if not issubclass(type_class, RezPluginType):
            raise TypeError("'type_class' must be a RezPluginType sub class")
        if type_class.type_name is None:
            raise TypeError("Subclasses of RezPluginType must provide a "
                            "'type_name' attribute")
        self._plugin_types[type_class.type_name] = LazySingleton(type_class)

    def get_plugin_types(self):
        """Return a list of the registered plugin types."""
        return self._plugin_types.keys()

    # -- plugins

    def get_plugin_class(self, plugin_type, plugin_name):
        """Return the class registered under the given plugin name."""
        try:
            return self._get_plugin_type(plugin_type).get_plugin_class(plugin_name)
        except KeyError:
            # TODO add a PluginManagerError
            raise ValueError("Unrecognised %s plugin: '%s'"
                             % (self.pretty_type_name, plugin_name))

    def get_plugins(self, plugin_type):
        """Return a list of the registered names available for the given plugin type."""
        return self._get_plugin_type(plugin_type).plugin_classes.keys()

    def create_instance(self, plugin_type, plugin_name, **instance_kwargs):
        """Create and return an instance of the given plugin."""
        plugin_type = self._get_plugin_type(plugin_type)
        return plugin_type().create_instance(plugin_name, **instance_kwargs)

#------------------------------------
# Plugin Types
#------------------------------------

class SourceRetrieverPluginType(RezPluginType):
    """Source retrievers download data from sources such as archives or repositories.
    """
    type_name = "source_retriever"

    def __init__(self):
        super(SourceRetrieverPluginType, self).__init__()
        self.ext_to_type = []
        self.extensions = set()

        for plugin_name, plugin_class in self.plugin_classes.iteritems():
            exts = plugin_class.supported_url_types()
            self.ext_to_type += [(x, plugin_name) for x in exts]
            self.extensions = self.extensions | set(exts)

        # ensures '.tar.gz' is seen before '.gz', for example
        self.ext_to_type = sorted(self.ext_to_type, key=lambda x: -len(x[0]))

    def create_instance(self, url, type=None, cache_path=None, cache_filename=None,
                        dry_run=False, **retriever_kwargs):
        plugin = type
        if not plugin:
            for ext, plug in self.ext_to_type:
                if url.endswith(ext):
                    plugin = plug
                    break

        if plugin is None:
            raise RuntimeError(("No source retriever is associated with the url: '%s'. "
                "Supported extensions are: %s") % (url, ', '.join(self.extensions)))

        return super(SourceRetrieverPluginType, self).create_instance(plugin,
            url=url,
            cache_path=cache_path,
            cache_filename=cache_filename,
            dry_run=dry_run,
            **retriever_kwargs)


class ShellPluginType(RezPluginType):
    """Support for different types of target shells, such as bash, tcsh.
    """
    type_name = "shell"


class ReleaseVCSPluginType(RezPluginType):
    """Support for different version control systems when releasing packages.
    """
    type_name = "release_vcs"


class ReleaseHookPluginType(RezPluginType):
    """Support for different version control systems when releasing packages.
    """
    type_name = "release_hook"


class BuildSystemPluginType(RezPluginType):
    """Support for different build systems when building packages.
    """
    type_name = "build_system"


plugin_manager = RezPluginManager()

plugin_manager.register_plugin_type(SourceRetrieverPluginType)
plugin_manager.register_plugin_type(ShellPluginType)
plugin_manager.register_plugin_type(ReleaseVCSPluginType)
plugin_manager.register_plugin_type(ReleaseHookPluginType)
plugin_manager.register_plugin_type(BuildSystemPluginType)
