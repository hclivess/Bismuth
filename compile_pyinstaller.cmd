d:
cd D:\Bismuth

SET var=%cd%
ECHO %var%

del /f /s /q %var%\dist 1>nul
rmdir /s /q %var%\dist
del /f /s /q %var%\build 1>nul
rmdir /s /q %var%\build

pyinstaller.exe --uac-admin --log-level=INFO %var%\miner.py --icon=%var%\graphics\icon.ico
pyinstaller.exe --uac-admin --noconsole --log-level=INFO %var%\gui.py --icon=%var%\graphics\icon.ico
pyinstaller.exe --uac-admin --log-level=INFO %var%\ledger_explorer.py --icon=%var%\graphics\icon.ico --hidden-import=ledger_explorer
pyinstaller.exe --uac-admin --log-level=INFO %var%\zircodice_dappie.py --icon=%var%\graphics\icon.ico
pyinstaller.exe --uac-admin --log-level=INFO %var%\zircodice_web.py --icon=%var%\graphics\icon.ico --hidden-import=zircodice_web
pyinstaller.exe --uac-admin --log-level=INFO %var%\node.py --icon=%var%\graphics\icon.ico

robocopy %var%\dist\gui %var%\dist\ /move /E
rmdir /s /q %var%\dist\gui
robocopy %var%\dist\miner %var%\dist\ /move /E
rmdir /s /q %var%\dist\miner
robocopy %var%\dist\ledger_explorer %var%\dist\ /move /E
rmdir /s /q %var%\dist\ledger_explorer
robocopy %var%\dist\zircodice_dappie %var%\dist\ /move /E
rmdir /s /q %var%\dist\zircodice_dappie
robocopy %var%\dist\zircodice_web %var%\dist\ /move /E
rmdir /s /q %var%\dist\zircodice_web
robocopy %var%\dist\node %var%\dist\ /move /E
rmdir /s /q dist\node

robocopy %var%\static %var%\dist\static
copy %var%\peers.txt %var%\dist\peers.txt
copy %var%\config.txt %var%\dist\config.txt
copy %var%\ledger_explorer.cmd %var%\dist\ledger_explorer.cmd

"C:\Program Files (x86)\Inno Setup 5\iscc" /q "%var%\setup.iss"
del %var%\dist\static\ledger.db
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "%var%\setup_no_blockchain.iss"
pause

