"""
Plugin manager, based upon Michael E. Cotterell gist, MIT Licence.
Copyright 2013, Michael E. Cotterell
Copyright 2018, EggPool
Copyright 2018, BismuthFoundation

See doc/27-plugins.md for the plugin system and compatible plugins.
"""


import importlib
import importlib.util
import importlib.machinery
import os
import logging
import collections


__version__ = '1.0.3'


class PluginManager:
    """
    A simple plugin manager
    """

    def __init__(self, app_log=None, main_module: str='__init__', plugin_folder: str='./plugins', config=None,
                 verbose: bool=True, init: bool=False, node=None):
        if app_log:
            self.app_log = app_log
        else:
            logging.basicConfig(level=logging.DEBUG)
            self.app_log = logging
        self.config = config
        self.node = node
        self.base_folder = config.ledger_path.replace("static/ledger.db", "")
        # path of mempool, colored.json and related files.
        self.plugin_folder = plugin_folder
        self.main_module = main_module
        self.verbose = verbose
        self.available_plugins = self.get_available_plugins()
        if self.verbose:
            self.app_log.info("Available plugins: {}".format(', '.join(self.available_plugins.keys())))
        self.loaded_plugins = collections.OrderedDict({})
        # --- modern (class-based) plugin state (doc/27) — alongside the legacy action_/filter_ hooks ----
        self.class_plugins = []        # BismuthPlugin instances discovered in loaded modules
        self.services = {}             # name -> object a plugin exposes (e.g. 'token_index' -> its store)
        self._peer_commands = {}       # command_str -> handler(data, socket)
        self._rest_routes = {}         # (method, (segment, ...)) -> handler(route_list, query)
        self._started = False
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
        self._collect_class_plugins()
        self.execute_action_hook('init', {'manager': self})

    # ----------------------------------------------------------------------------------------------
    # Modern, class-based plugins (doc/27). A loaded module may expose a ``PLUGIN`` instance (or a
    # ``get_plugin()`` factory) that subclasses ``plugin_base.BismuthPlugin``. These get a typed lifecycle
    # (setup / backfill / on_block / on_rollback) and may register services, wire commands and REST routes —
    # so an optional feature (tokens, aliases, …) lives entirely outside the node core. The legacy
    # ``action_*`` / ``filter_*`` module-function hooks above are untouched (BismuthPlugins stay compatible).
    # ----------------------------------------------------------------------------------------------
    def _collect_class_plugins(self):
        for name, info in self.loaded_plugins.items():
            module = info['module']
            plugin = None
            if hasattr(module, 'PLUGIN'):
                plugin = getattr(module, 'PLUGIN')
            elif hasattr(module, 'get_plugin'):
                try:
                    plugin = module.get_plugin()
                except Exception as e:
                    self.app_log.warning("Plugin '{}' get_plugin() failed: {}".format(name, e))
            if plugin is not None:
                self.class_plugins.append(plugin)
                if self.verbose:
                    self.app_log.info("Modern plugin '{}' discovered".format(getattr(plugin, 'name', name)))

    def start(self, node):
        """Run the class-plugin lifecycle once the net type + ledger path are finalized and the db is ready:
        build each plugin's context, ``setup`` it, register its services, ``backfill`` history, and collect
        its wire commands + REST routes. Idempotent (guarded by ``_started``)."""
        if self._started:
            return
        self._started = True
        self.node = node or self.node
        if not self.class_plugins:
            return
        import os
        import plugin_base
        ledger_path = getattr(node, 'ledger_path', None) or self.config.ledger_path
        data_root = os.path.join(os.path.dirname(ledger_path) or '.', 'plugin_data')
        for plugin in self.class_plugins:
            pname = getattr(plugin, 'name', 'plugin')
            try:
                ctx = plugin_base.PluginContext(
                    logger=self.app_log,
                    data_dir=os.path.join(data_root, pname),
                    ledger_path=ledger_path,
                    config=self.config,
                    is_regnet=getattr(node, 'is_regnet', False),
                    is_testnet=getattr(node, 'is_testnet', False),
                    is_mainnet=getattr(node, 'is_mainnet', True))
                plugin.setup(ctx)
                for sname, sobj in (plugin.services() or {}).items():
                    self.services[sname] = sobj
                plugin.backfill()
                for cmd, handler in (plugin.peer_commands() or {}).items():
                    self._peer_commands[cmd] = handler
                for route_key, handler in (plugin.rest_routes() or {}).items():
                    self._rest_routes[route_key] = handler
            except Exception as e:
                self.app_log.warning("Modern plugin '{}' start failed: {}".format(pname, e))

    def get_service(self, name):
        """The object a plugin registered under ``name`` (e.g. the token_index store), or None."""
        return self.services.get(name)

    def dispatch_block(self, height, transactions):
        """Notify every class plugin of a freshly-committed block (derived-state maintenance)."""
        for plugin in self.class_plugins:
            try:
                plugin.on_block(height, transactions)
            except Exception as e:
                self.app_log.warning("Plugin '{}' on_block({}) failed: {}".format(
                    getattr(plugin, 'name', '?'), height, e))

    def dispatch_rollback(self, height):
        """Notify every class plugin of a reorg dropping blocks at block_height >= height."""
        for plugin in self.class_plugins:
            try:
                plugin.on_rollback(height)
            except Exception as e:
                self.app_log.warning("Plugin '{}' on_rollback({}) failed: {}".format(
                    getattr(plugin, 'name', '?'), height, e))

    def peer_command_handler(self, data):
        """The handler for a wire command a plugin registered, or None. ``data`` is the raw command string;
        a plugin command matches by exact name (the command is the first token)."""
        cmd = data.split(" ")[0] if isinstance(data, str) else data
        return self._peer_commands.get(cmd)

    @staticmethod
    def _route_matches(pattern, route):
        if pattern and pattern[-1] == "*":
            prefix = pattern[:-1]
            return len(route) > len(prefix) and tuple(route[:len(prefix)]) == prefix
        return tuple(route) == tuple(pattern)

    def rest_handle(self, method, route, query):
        """Dispatch a REST request to a plugin route. Returns (handled: bool, result). ``result`` None with
        handled=True means the plugin owns the route but found nothing (the caller maps that to 404)."""
        for (m, pattern), handler in self._rest_routes.items():
            if m == method and self._route_matches(pattern, list(route)):
                try:
                    return True, handler(list(route), query)
                except Exception as e:
                    self.app_log.warning("Plugin REST {} {} failed: {}".format(method, route, e))
                    return True, None
        return False, None

    def teardown(self):
        for plugin in self.class_plugins:
            try:
                plugin.teardown()
            except Exception:
                pass

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
                self._unload_plugin(plugin_name)
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
    print("See doc/27-plugins.md for the plugin system and compatible plugins.")
