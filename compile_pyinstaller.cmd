pyinstaller.exe --onefile --noconsole gui.py --icon=graphics\icon.ico
pyinstaller.exe --onefile node.py --icon=graphics\icon.ico
pyinstaller.exe --onefile genesis.py --icon=graphics\icon.ico
pyinstaller.exe --onefile send.py --icon=graphics\icon.ico
copy ledger.db dist\ledger.db
pause