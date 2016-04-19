; -- Example1.iss --
; Demonstrates copying 3 files and creating an icon.

; SEE THE DOCUMENTATION FOR DETAILS ON CREATING .ISS SCRIPT FILES!

[Setup]
AppName=Bismuth
AppVersion=0.2
DefaultDirName={pf}\Bismuth
DefaultGroupName=Bismuth
UninstallDisplayIcon={app}\node.exe
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=bismuth_installer
SetupIconFile=graphics\icon.ico

[Files]
Source: "Dist\*" ; DestDir: "{app}"; Flags: recursesubdirs;

[Icons]
Name: "{group}\Start Node"; Filename: "{app}\node.exe"
Name: "{group}\Open GUI"; Filename: "{app}\gui.exe"
Name: "{group}\Uninstall Bismuth"; Filename: "{uninstallexe}"

Name: "{commondesktop}\Start Node"; Filename: "{app}\node.exe"
Name: "{commondesktop}\Open GUI"; Filename: "{app}\gui.exe"