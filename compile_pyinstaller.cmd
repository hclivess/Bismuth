del /f /s /q dist 1>nul
rmdir /s /q dist
pyinstaller.exe --onefile --log-level=INFO gui.py --icon=graphics\icon.ico
pyinstaller.exe --onefile --log-level=INFO node.py --icon=graphics\icon.ico
copy peers.txt dist\peers.txt
copy ledger.db dist\ledger.db
robocopy /e graphics dist\graphics\
robocopy /move /e dist\node\ dist\
robocopy /move /e dist\gui\ dist\
del /f /s /q dist\gui 1>nul
rmdir /s /q dist\gui
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
pause

