# -*- mode: python -*-
a = Analysis(['node.py'],
             pathex=['C:\\Users\\HCLivess\\Documents\\GitHub\\XBM-Bismuth'],
             hiddenimports=[],
             hookspath=None,
             runtime_hooks=None)
pyz = PYZ(a.pure)
exe = EXE(pyz,
          a.scripts,
          exclude_binaries=True,
          name='node.exe',
          debug=False,
          strip=None,
          upx=True,
          console=True , icon='graphics\\icon.ico')
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=None,
               upx=True,
               name='node')
