#run from vs command line
nuitka --recurse-all node.py --windows-icon="graphics\icon.ico"
nuitka --recurse-all gui.py --windows-icon="graphics\icon.ico" --windows-disable-console
pause