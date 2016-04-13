pyinstaller.exe --onefile --noconsole gui.py --icon=icon.ico
pyinstaller.exe --onefile node.py --icon=icon.ico
pyinstaller.exe --onefile genesis.py --icon=icon.ico
pyinstaller.exe --onefile send.py --icon=icon.ico
copy ledger.db dist\ledger.db
del genesis.spec
del gui.spec
del node.spec
del send.spec
pause