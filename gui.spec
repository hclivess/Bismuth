# -*- mode: python -*-
a = Analysis(['gui.py'],
             pathex=['C:\\Users\\HCLivess\\Documents\\GitHub\\XBM-Bismuth'],
             hiddenimports=[],
             hookspath=None,
             runtime_hooks=None)
pyz = PYZ(a.pure)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          name='gui.exe',
          debug=False,
          strip=None,
          upx=True,
          console=False , icon='graphics\\icon.ico')
