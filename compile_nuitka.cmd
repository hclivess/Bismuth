del /f /s /q dist 1>nul
rmdir /s /q dist
mkdir dist

python -m nuitka --follow-imports commands.py --windows-icon=graphics\icon.ico --standalone --show-progress -j 8 --recurse-all
python -m nuitka --follow-imports node.py --windows-icon=graphics\icon.ico --standalone --show-progress -j 8 --recurse-all
python -m nuitka --follow-imports wallet.py --windows-icon=graphics\icon.ico --standalone --show-progress -j 8 --recurse-all
python -m nuitka --follow-imports node_stop.py --windows-icon=graphics\icon.ico --standalone --show-progress -j 8 --recurse-all

robocopy node.dist dist\files /MOVE /E
robocopy wallet.dist dist\files /MOVE /E
robocopy commands.dist dist\files /MOVE /E
robocopy node_stop.dist dist\files /MOVE /E

robocopy nuitka\Cryptodome dist\files\Cryptodome /MIR
robocopy nuitka\lib dist\lib /MIR

mkdir dist\files\static
copy static\backup.py dist\files\static\backup.py
copy static\bg.jpg dist\files\static\bg.jpg
copy static\Chart.js dist\files\static\Chart.js
copy static\explorer.ico dist\files\static\explorer.ico
copy static\explorer_bg.png dist\files\static\explorer_bg.png
copy static\style.css dist\files\static\style.css
copy static\style_zircodice.css dist\files\static\style_zircodice.css
copy static\zircodice.ico dist\files\static\zircodice.ico

copy peers.txt dist\files\peers.txt
copy peers.txt dist\files\suggested_peers.txt
copy config.txt dist\files\config.txt

"C:\Program Files (x86)\Inno Setup 5\iscc" /q "setup_nuitka.iss"
pause

