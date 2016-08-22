; -- Example1.iss --
; Demonstrates copying 3 files and creating an icon.

; SEE THE DOCUMENTATION FOR DETAILS ON CREATING .ISS SCRIPT FILES!

[Setup]
AppName=Bismuth
AppVersion=Beta
DefaultDirName={pf}\Bismuth
DefaultGroupName=Bismuth
UninstallDisplayIcon={app}\node.exe
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=bismuth_installer
SetupIconFile=graphics\icon.ico

WizardImageFile=graphics\left.bmp
WizardSmallImageFile=graphics\mini.bmp

[Files]
Source: "Dist\*" ; DestDir: "{app}"; Flags: recursesubdirs;

[Icons]
Name: "{group}\Start"; Filename: "{app}\node.exe"
Name: "{group}\Overview"; Filename: "{app}\gui.exe"
Name: "{group}\Explorer"; Filename: "{app}\explorer.exe"  5492
Name: "{group}\Mining"; Filename: "{app}\miner.exe"
Name: "{group}\Uninstall Bismuth"; Filename: "{uninstallexe}"

Name: "{commondesktop}\Start Node"; Filename: "{app}\node.exe"
Name: "{commondesktop}\Overview"; Filename: "{app}\gui.exe"
Name: "{commondesktop}\Explorer"; Filename: "{app}\explorer.exe"  5492
Name: "{commondesktop}\Mining"; Filename: "{app}\miner.exe"

[Run]
Filename: "{app}\node.exe"; Description: "Start"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\gui.exe"; Description: "Overview"; Flags: postinstall nowait skipifsilent unchecked
Filename: "{app}\explorer.exe 5492"; Description: "Explorer"; Flags: postinstall nowait skipifsilent unchecked
Filename: "{app}\miner.exe"; Description: "Mining"; Flags: postinstall nowait skipifsilent unchecked
