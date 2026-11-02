; Inno Setup recipe for the Studio Sync Windows installer.
; Built by Build-Windows-Installer.bat after PyInstaller has made dist\Studio Sync.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9E6C7E2B-5B7A-4C1E-9D3F-0C2A7F4B8E11}
AppName=Studio Sync
AppVersion={#AppVersion}
AppVerName=Studio Sync {#AppVersion}
AppPublisher=We Made It Recording Studio
DefaultDirName={autopf}\Studio Sync
DefaultGroupName=Studio Sync
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Studio-Sync-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Studio Sync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Needed for Program Files and the firewall rule.
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "flscript"; Description: "Install the FL Studio controller script"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; Remove files from older versions that a new version no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Studio Sync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\FL-Script\Studio-Sync\device_studio_sync.py"; DestDir: "{userdocs}\Image-Line\FL Studio\Settings\Hardware\Studio-Sync"; Tasks: flscript; Flags: ignoreversion; Check: FLUserFolderExists

[Icons]
Name: "{autoprograms}\Studio Sync"; Filename: "{app}\Studio Sync.exe"
Name: "{autodesktop}\Studio Sync"; Filename: "{app}\Studio Sync.exe"; Tasks: desktopicon

[Run]
; Let the other laptops reach this one (session, discovery and file sharing).
; Packets and files are signed with the room code, so any network profile is allowed.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Studio Sync"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Studio Sync"" dir=in action=allow program=""{app}\Studio Sync.exe"" enable=yes profile=any"; Flags: runhidden waituntilterminated
Filename: "https://www.tobias-erichsen.de/software/loopmidi.html"; Description: "Download loopMIDI (needed for the two MIDI ports)"; Flags: postinstall shellexec skipifsilent; Check: not LoopMidiInstalled
Filename: "https://go.microsoft.com/fwlink/p/?LinkId=2124703"; Description: "Download Microsoft WebView2 (needed to show the Studio Sync window)"; Flags: postinstall shellexec skipifsilent; Check: not WebView2Installed
Filename: "{app}\Studio Sync.exe"; Description: "Open Studio Sync"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Studio Sync"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[Code]
function FLUserFolderExists: Boolean;
begin
  Result := DirExists(ExpandConstant('{userdocs}\Image-Line\FL Studio'));
end;

function WebView2Installed: Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) then
    Result := (Version <> '') and (Version <> '0.0.0.0');
  if not Result and RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) then
    Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function LoopMidiInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf32}\Tobias Erichsen\loopMIDI\loopMIDI.exe')) or
            FileExists(ExpandConstant('{commonpf64}\Tobias Erichsen\loopMIDI\loopMIDI.exe'));
end;
