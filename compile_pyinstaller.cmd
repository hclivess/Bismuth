del /f /s /q dist 1>nul
rmdir /s /q dist
pyinstaller.exe --uac-admin --onefile --noconsole --log-level=INFO gui.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --onefile --log-level=INFO node.py --icon=graphics\icon.ico
copy peers.txt dist\peers.txt
copy ledger.db dist\ledger.db
robocopy /e graphics dist\graphics\
del /f /s /q dist\gui 1>nul
rmdir /s /q dist\gui
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
pause

