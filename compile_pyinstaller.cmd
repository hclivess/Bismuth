del /f /s /q dist 1>nul
rmdir /s /q dist
pyinstaller.exe --uac-admin --onefile --noconsole --log-level=INFO gui.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO node.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO miner.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO explorer.py --icon=graphics\icon.ico --hidden-import=explorer
pyinstaller.exe --uac-admin --onefile --log-level=INFO plotter_html.py --icon=graphics\icon.ico

robocopy static dist\static
copy peers.txt dist\peers.txt
copy ledger.db dist\ledger.db
copy explorer_custom_port.cmd dist\explorer_custom_port.cmd
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
pause

