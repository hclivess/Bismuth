; -- Example1.iss --
; Demonstrates copying 3 files and creating an icon.

; SEE THE DOCUMENTATION FOR DETAILS ON CREATING .ISS SCRIPT FILES!

[Setup]
AppName=Bismuth
AppVersion=Mainnet
DefaultDirName={pf}\Bismuth
DefaultGroupName=Bismuth
UninstallDisplayIcon={app}\node.exe
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=Bismuth_installer_no_blockchain
SetupIconFile=graphics\icon.ico
DisableDirPage=no

WizardImageFile=graphics\left.bmp
WizardSmallImageFile=graphics\mini.bmp

[Files]
Source: "Dist\*" ; DestDir: "{app}"; Flags: recursesubdirs;

[Icons]
Name: "{group}\Start"; Filename: "{app}\node.exe"
Name: "{group}\Interface"; Filename: "{app}\gui.exe"
Name: "{group}\Mining"; Filename: "{app}\miner.exe"
Name: "{group}\Uninstall Bismuth"; Filename: "{uninstallexe}"

Name: "{commondesktop}\Start Node"; Filename: "{app}\node.exe"
Name: "{commondesktop}\Interface"; Filename: "{app}\gui.exe"
Name: "{commondesktop}\Mining"; Filename: "{app}\miner.exe"

[Run]
Filename: "{app}\node.exe"; Description: "Start"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\gui.exe"; Description: "Interface"; Flags: postinstall nowait skipifsilent unchecked
Filename: "{app}\miner.exe"; Description: "Mining"; Flags: postinstall nowait skipifsilent unchecked

[Registry]
Root: "HKCU"; Subkey: "SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; ValueType: String; ValueName: "{app}\node.exe"; ValueData: "RUNASADMIN"; Flags: uninsdeletekeyifempty uninsdeletevalue;
Root: "HKCU"; Subkey: "SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; ValueType: String; ValueName: "{app}\gui.exe"; ValueData: "RUNASADMIN"; Flags: uninsdeletekeyifempty uninsdeletevalue;
Root: "HKCU"; Subkey: "SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; ValueType: String; ValueName: "{app}\miner.exe"; ValueData: "RUNASADMIN"; Flags: uninsdeletekeyifempty uninsdeletevalue;