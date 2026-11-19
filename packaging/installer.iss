; Inno Setup script for Maxcotech Video Downloader (formerly Zoom Batch Downloader).
; Built by packaging\build.ps1, which passes AppVersion, SourceDir, OutDir, IconFile,
; GuideFile and NoticesFile.

#define AppName "Maxcotech Video Downloader"
#define AppExe "MaxcotechVideoDownloader.exe"
#define Publisher "Maxcotech LTD"

[Setup]
; Same AppId as 1.0.x, so installing this upgrades an existing install in place.
AppId={{8C1B5E2A-4D7F-4C61-9B3E-2F6A1D9E7C45}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppCopyright=Powered by {#Publisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription={#AppName} Setup
VersionInfoCopyright=Powered by {#Publisher}
; Per-user install: no administrator password needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=MaxcotechVideoDownloader-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Messages]
; Shown at the bottom-left of every installer page.
BeveledLabel=Powered by {#Publisher}
WelcomeLabel2=This will install [name/ver] on your computer.%n%nPaste Zoom recording links (with passcodes) or YouTube videos and playlists, and download them all at once, fast.%n%nPowered by {#Publisher}.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Leftovers from 1.0.x (when the app was called Zoom Batch Downloader).
Type: files; Name: "{app}\ZoomBatchDownloader.exe"
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{autoprograms}\Zoom Batch Downloader.lnk"
Type: files; Name: "{autoprograms}\Zoom Batch Downloader - How to use.lnk"
Type: files; Name: "{autodesktop}\Zoom Batch Downloader.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#GuideFile}"; DestDir: "{app}"; DestName: "How to use.txt"; Flags: ignoreversion
Source: "{#NoticesFile}"; DestDir: "{app}"; DestName: "Third-party notices.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} - How to use"; Filename: "{app}\How to use.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Engine updates the app downloaded for itself (settings and the user's videos are kept).
Type: filesandordirs; Name: "{localappdata}\ZoomBatchDownloader\engine"
