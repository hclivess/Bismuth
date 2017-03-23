del /f /s /q dist 1>nul
rmdir /s /q dist
rmdir /s /q build
pyinstaller.exe --uac-admin --onefile --noconsole --log-level=INFO gui.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO miner.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO ledger_explorer.py --icon=graphics\icon.ico --hidden-import=ledger_explorer
pyinstaller.exe --uac-admin --onefile --log-level=INFO zircodice.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO zircodice_web.py --icon=graphics\icon.ico --hidden-import=zircodice_web
pyinstaller.exe --uac-admin --onefile --log-level=INFO node.py --icon=graphics\icon.ico

robocopy static dist\static
copy peers.txt dist\peers.txt
copy config.txt dist\config.txt
copy ledger_explorer.cmd dist\ledger_explorer.cmd
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
pause

