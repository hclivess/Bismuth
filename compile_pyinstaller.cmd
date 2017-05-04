del /f /s /q dist 1>nul
rmdir /s /q dist
del /f /s /q build 1>nul
rmdir /s /q build

pyinstaller.exe --uac-admin --noconsole --log-level=INFO gui.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --log-level=INFO miner.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --log-level=INFO ledger_explorer.py --icon=graphics\icon.ico --hidden-import=ledger_explorer
pyinstaller.exe --uac-admin --log-level=INFO zircodice_dappie.py --icon=graphics\icon.ico
pyinstaller.exe --uac-admin --log-level=INFO zircodice_web.py --icon=graphics\icon.ico --hidden-import=zircodice_web
pyinstaller.exe --uac-admin --log-level=INFO node.py --icon=graphics\icon.ico

robocopy dist\gui dist\ /move /E
rmdir /s /q dist\gui
robocopy dist\miner dist\ /move /E
rmdir /s /q dist\miner
robocopy dist\ledger_explorer dist\ /move /E
rmdir /s /q dist\ledger_explorer
robocopy dist\zircodice_dappie dist\ /move /E
rmdir /s /q dist\zircodice_dappie
robocopy dist\zircodice_web dist\ /move /E
rmdir /s /q dist\zircodice_web
robocopy dist\node dist\ /move /E
rmdir /s /q dist\node

robocopy static dist\static
copy peers.txt dist\peers.txt
copy config.txt dist\config.txt
copy ledger_explorer.cmd dist\ledger_explorer.cmd

"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
del dist\static\ledger.db
"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup_no_blockchain.iss"
pause

