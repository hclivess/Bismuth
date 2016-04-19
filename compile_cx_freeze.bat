start C:\Python27\Scripts\cxfreeze gui.py --base-name=Win32GUI --target-dir dist --icon graphics\icon.ico
start C:\Python27\Scripts\cxfreeze node.py --target-dir dist --icon graphics\icon.ico
copy peers.txt dist\
copy ledger.db dist\
xcopy graphics dist\graphics\
