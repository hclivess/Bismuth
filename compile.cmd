pyinstaller.exe --onefile --noconsole gui.py
pyinstaller.exe --onefile node.py
pyinstaller.exe --onefile genesis.py
pyinstaller.exe --onefile send.py
del genesis.spec
del gui.spec
del node.spec
del send.spec
del build
pause