# -*- mode: python -*-
a = Analysis(['genesis.py'],
             pathex=['C:\\Users\\HCLivess\\Documents\\GitHub\\hopium'],
             hiddenimports=[],
             hookspath=None,
             runtime_hooks=None)
pyz = PYZ(a.pure)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          name='genesis.exe',
          debug=False,
          strip=None,
          upx=True,
          console=True )
