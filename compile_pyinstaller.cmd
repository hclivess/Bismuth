del /f /s /q dist 1>nul
rmdir /s /q dist
del /f /s /q build 1>nul
rmdir /s /q build

pyinstaller --uac-admin --log-level=INFO commands.py --icon=graphics\icon.ico --hidden-import=pycryptodomex --hidden-import=PySocks
pyinstaller --uac-admin --log-level=INFO wallet.py --icon=graphics\icon.ico --hidden-import=pycryptodomex --hidden-import=PySocks
pyinstaller --uac-admin --log-level=INFO node.py --icon=graphics\icon.ico --hidden-import=pycryptodomex --hidden-import=PySocks

robocopy graphics dist\graphics
robocopy themes dist\themes
robocopy dist\wallet dist\ /move /E
rmdir /s /q dist\wallet
robocopy dist\node dist\ /move /E
rmdir /s /q dist\node
robocopy dist\commands dist\ /move /E
rmdir /s /q dist\commands

mkdir dist\static
copy static\backup.py dist\static\backup.py
copy static\bg.jpg dist\static\bg.jpg
copy static\Chart.js dist\static\Chart.js
copy static\explorer.ico dist\static\explorer.ico
copy static\explorer_bg.png dist\static\explorer_bg.png
copy static\style.css dist\static\style.css
copy static\style_zircodice.css dist\static\style_zircodice.css
copy static\zircodice.ico dist\static\zircodice.ico

copy peers.txt dist\peers.txt
copy config.txt dist\config.txt

"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup.iss"
pause

