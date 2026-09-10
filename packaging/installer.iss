#define AppVersion "0.3.0"
[Setup]
AppId={{BB3EFEE4-969E-4722-97EA-DA590EE610C3}
AppName=YouTube Likes Sync
AppVersion={#AppVersion}
AppPublisher=YouTube Likes Sync contributors
AppPublisherURL=https://github.com/Cammiie/youtube-likes-sync
DefaultDirName={localappdata}\Programs\YouTubeLikesSync
DefaultGroupName=YouTube Likes Sync
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\artifacts
OutputBaseFilename=YouTubeLikesSync-Setup-{#AppVersion}-windows-x64
SetupIconFile=app.ico
UninstallDisplayIcon={app}\YouTubeLikesSync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE

[Files]
Source: "..\dist\YouTubeLikesSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Connect YouTube Music"; Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "setup"
Name: "{group}\Pause Sync"; Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "cli pause --quiet"
Name: "{group}\Resume Sync"; Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "cli resume --quiet"
Name: "{group}\Open downloader"; Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "cli open-monochrome --quiet"
Name: "{group}\Uninstall YouTube Likes Sync"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "install-integration"; Flags: runhidden waituntilterminated; Check: EnableIntegration
Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "setup"; Description: "Connect YouTube Music"; Flags: postinstall nowait skipifsilent; Check: EnableIntegration

[UninstallRun]
Filename: "{app}\YouTubeLikesSync.exe"; Parameters: "uninstall-integration"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveOwnedIntegration"

[Code]
function EnableIntegration(): Boolean;
begin
  { Used only by isolated installer smoke tests; normal installs integrate fully. }
  Result := ExpandConstant('{param:NOINTEGRATION|0}') <> '1';
end;
