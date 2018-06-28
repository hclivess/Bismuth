d:
cd D:\Bismuth

SET var=%cd%
ECHO %var%

del /f /s /q %var%\dist 1>nul
rmdir /s /q %var%\dist
del /f /s /q %var%\build 1>nul
rmdir /s /q %var%\build

C:\Python36\Scripts\pyinstaller.exe --uac-admin --log-level=INFO %var%\commands.py --icon=%var%\graphics\icon.ico
C:\Python36\Scripts\pyinstaller.exe --uac-admin --noconsole --log-level=INFO %var%\wallet.py --icon=%var%\graphics\icon.ico
C:\Python36\Scripts\pyinstaller.exe --uac-admin --log-level=INFO %var%\node.py --icon=%var%\graphics\icon.ico

robocopy %var%\graphics %var%\dist\graphics
robocopy %var%\themes %var%\dist\themes
robocopy %var%\dist\wallet %var%\dist\ /move /E
rmdir /s /q %var%\dist\wallet
robocopy %var%\dist\node %var%\dist\ /move /E
rmdir /s /q dist\node
robocopy %var%\dist\commands %var%\dist\ /move /E
rmdir /s /q dist\commands

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

