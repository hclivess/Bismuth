"""
Plugin manager, based upon Michael E. Cotterell gist, MIT Licence.
Copyright 2013, Michael E. Cotterell
Copyright 2018, EggPool
Copyright 2018, BismuthFoundation

See https://github.com/bismuthfoundation/BismuthPlugins for compatible plugins and doc.
"""


import importlib
import importlib.util
import importlib.machinery
import os
import logging
import collections


__version__ = '1.0.2'


class PluginManager:
    """
    A simple plugin manager
    """

    def __init__(self, app_log=None, main_module='__init__', plugin_folder='./plugins', verbose=True, init=False):
        if app_log:
            self.app_log = app_log
        else:
            logging.basicConfig(level=logging.DEBUG)
            self.app_log = logging
        self.plugin_folder = plugin_folder
        self.main_module = main_module
        self.verbose = verbose
        self.available_plugins = self.get_available_plugins()
        if self.verbose:
            self.app_log.info("Available plugins: {}".format(', '.join(self.available_plugins.keys())))
        self.loaded_plugins = collections.OrderedDict({})
        if init:
            self.init()

    def init(self):
        """
        loads all available plugins and inits them.
        :return:
        """
        for plugin in self.available_plugins:
            # TODO: only load "auto load" plugins
            self.load_plugin(plugin)
        self.execute_action_hook('init', {'manager': self})

    def get_available_plugins(self):
        """
        Returns a dictionary of plugins available in the plugin folder
        """
        plugins = collections.OrderedDict({})
        try:
            for possible in os.listdir(self.plugin_folder):
                location = os.path.join(self.plugin_folder, possible)
                if os.path.isdir(location) and self.main_module + '.py' in os.listdir(location):
                    info = importlib.machinery.PathFinder().find_spec(self.main_module, [location])
                    plugins[possible] = {
                        'name': possible,
                        'info': info,
                        'autoload': True  # Todo
                    }
        except Exception as e:
            self.app_log.info("Can't list plugins from '{}'.".format(self.plugin_folder))
        # TODO: sort by name or priority, add json specs file.
        return plugins

    def get_loaded_plugins(self):
        """
        Returns a dictionary of the loaded plugin modules
        """
        return self.loaded_plugins.copy()

    def load_plugin(self, plugin_name):
        """
        Loads a plugin module
        """
        if plugin_name in self.available_plugins:
            if plugin_name not in self.loaded_plugins:
                spec = self.available_plugins[plugin_name]['info']
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.loaded_plugins[plugin_name] = {
                    'name': plugin_name,
                    'info': self.available_plugins[plugin_name]['info'],
                    'module': module
                }
                if self.verbose:
                    self.app_log.info("Plugin '{}' loaded".format(plugin_name))
            else:
                self.app_log.warning("Plugin '{}' already loaded".format(plugin_name))
        else:
            self.app_log.error("Cannot locate plugin '{}'".format(plugin_name))
            raise Exception("Cannot locate plugin '{}'".format(plugin_name))

    def _unload_plugin(self, plugin_name):
        del self.loaded_plugins[plugin_name]
        if self.verbose:
            self.app_log.info("Plugin '{}' unloaded".format(plugin_name))

    def unload_plugin(self, plugin_name=''):
        """
        Unloads a single plugin module or all if plugin_name is empty
        """
        try:
            if plugin_name:
                self.unload_plugin(plugin_name)
            else:
                for plugin in self.get_loaded_plugins():
                    self._unload_plugin(plugin)
        except:
            pass

    def execute_action_hook(self, hook_name, hook_params=None, first_only=False):
        """
        Executes action hook functions of the form action_hook_name contained in
        the loaded plugin modules.
        """
        for key, plugin_info in self.loaded_plugins.items():
            try:
                module = plugin_info['module']
                hook_func_name = "action_{}".format(hook_name)
                if hasattr(module, hook_func_name):
                    hook_func = getattr(module, hook_func_name)
                    hook_func(hook_params)
                    if first_only:
                        # Avoid deadlocks on specific use cases
                        return
            except Exception as e:
                self.app_log.warning("Plugin '{}' exception '{}' on action '{}'".format(key, e, hook_name))

    def execute_filter_hook(self, hook_name, hook_params, first_only=False):
        """
        Filters the hook_params through filter hook functions of the form
        filter_hook_name contained in the loaded plugin modules.
        """
        try:
            hook_params_keys = hook_params.keys()
            for key, plugin_info in self.loaded_plugins.items():
                try:
                    module = plugin_info['module']
                    hook_func_name = "filter_{}".format(hook_name)
                    if hasattr(module, hook_func_name):
                        hook_func = getattr(module, hook_func_name)
                        hook_params = hook_func(hook_params)
                        for nkey in hook_params_keys:
                            if nkey not in hook_params.keys():
                                msg = "Function '{}' in plugin '{}' is missing '{}' in the dict it returns".format(
                                    hook_func_name, plugin_info['name'], nkey)
                                self.app_log.error(msg)
                                raise Exception(msg)
                            if first_only:
                                # Avoid deadlocks on specific use cases
                                return  # will trigger the finally section
                except Exception as e:
                    self.app_log.warning("Plugin '{}' exception '{}' on filter '{}'".format(key, e, hook_name))
        except Exception as e:
            self.app_log.warning("Exception '{}' on filter '{}'".format(e, hook_name))
        finally:
            return hook_params


if __name__ == "__main__":
    print("This is Bismuth core plugin module.")
    print("See https://github.com/bismuthfoundation/BismuthPlugins for compatible plugins and doc.")
