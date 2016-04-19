pyinstaller.exe --log-level=INFO --hidden-import=tcl --hidden-import=tk --onefile --noconsole gui.py --icon=graphics\icon.ico
pyinstaller.exe --log-level=INFO --onefile node.py --icon=graphics\icon.ico
copy peers.txt dist\
copy ledger.db dist\
xcopy graphics dist\graphics\
pause