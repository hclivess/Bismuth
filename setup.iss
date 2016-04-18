; -- Example1.iss --
; Demonstrates copying 3 files and creating an icon.

; SEE THE DOCUMENTATION FOR DETAILS ON CREATING .ISS SCRIPT FILES!

[Setup]
AppName=Bismuth
AppVersion=0.1
DefaultDirName={pf}\Bismuth
DefaultGroupName=Bismuth
UninstallDisplayIcon={app}\node.exe
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=Bismuth_installer
SetupIconFile=graphics\icon.ico

[Files]
Source: "Dist\*" ; DestDir: "{app}"

[Icons]
Name: "{group}\Start Node"; Filename: "{app}\node.exe"
Name: "{group}\Open GUI"; Filename: "{app}\gui.exe"
Name: "{group}\Uninstall Bismuth"; Filename: "{uninstallexe}"

