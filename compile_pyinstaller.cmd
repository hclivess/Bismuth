set __COMPAT_LAYER=Win7

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
pyinstaller.exe --uac-admin --log-level=INFO %var%\node.py --icon=%var%\graphics\icon.ico

robocopy %var%\dist\gui %var%\dist\ /move /E
rmdir /s /q %var%\dist\gui
robocopy %var%\dist\miner %var%\dist\ /move /E
rmdir /s /q %var%\dist\miner
robocopy %var%\dist\node %var%\dist\ /move /E
rmdir /s /q dist\node

mkdir %var%\dist\static
copy %var%\static\backup.py %var%\dist\static\backup.py
copy %var%\static\bg.jpg %var%\dist\static\bg.jpg
copy %var%\static\Chart.js %var%\dist\static\Chart.js
copy %var%\static\explorer.ico %var%\dist\static\explorer.ico
copy %var%\static\explorer_bg.png %var%\dist\static\explorer_bg.png
copy %var%\static\style.css %var%\dist\static\style.css
copy %var%\static\style_zircodice.css %var%\dist\static\style_zircodice.css
copy %var%\static\zircodice.ico %var%\dist\static\zircodice.ico

copy %var%\peers.txt %var%\dist\peers.txt
copy %var%\config.txt %var%\dist\config.txt

"C:\Program Files (x86)\Inno Setup 5\iscc" /q "%var%\setup.iss"
pause

