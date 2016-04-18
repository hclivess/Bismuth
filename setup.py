from distutils.core import setup
import py2exe, sys, os

sys.argv.append('py2exe')

setup(
    options = {'py2exe': {'bundle_files': 1}},
    zipfile = None,
    windows = [{
            "script":"gui.py",
            "icon_resources": [(1, "graphics\icon.ico")],
            }],
)

setup(
    options = {'py2exe': {'bundle_files': 1}},
    zipfile = None,
    console = [{
            "script":"node.py",
            "icon_resources": [(1, "graphics\icon.ico")],
            }],
)

setup(
    options = {'py2exe': {'bundle_files': 1}},
    zipfile = None,
    console = [{
            "script":"genesis.py",
            "icon_resources": [(1, "graphics\icon.ico")],
            }],
)
