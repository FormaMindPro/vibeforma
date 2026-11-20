; VibeForma setup: per-user, no Python, shortcuts, in-place upgrade, uninstall.
; Built by tools/build_installer.py:
;   VIBEFORMA_VERSION  - product version (app/config.py)
;   VIBEFORMA_DIST     - the PyInstaller one-folder output (dist\VibeForma)
;   VIBEFORMA_OUT      - where the setup exe goes (dist)
; The installer only owns the program folder; user data stays in
; %LOCALAPPDATA%\VibeForma until the user asks to delete it on uninstall.

#ifndef VIBEFORMA_VERSION
  #define VIBEFORMA_VERSION "0.0.0"
#endif
#ifndef VIBEFORMA_DIST
  #define VIBEFORMA_DIST "..\dist\VibeForma"
#endif
#ifndef VIBEFORMA_OUT
  #define VIBEFORMA_OUT "..\dist"
#endif

[Setup]
AppId={{6F2B1E4A-7C3D-4A58-9B1F-3E9D2A6C8B41}
AppName=VibeForma
AppVersion={#VIBEFORMA_VERSION}
AppVerName=VibeForma {#VIBEFORMA_VERSION}
AppPublisher=FormaMindPro
AppPublisherURL=https://github.com/FormaMindPro/vibeforma
AppSupportURL=https://github.com/FormaMindPro/vibeforma/issues
DefaultDirName={localappdata}\Programs\VibeForma
DefaultGroupName=VibeForma
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir={#VIBEFORMA_OUT}
OutputBaseFilename=VibeForma-Setup-{#VIBEFORMA_VERSION}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=VibeForma
UninstallDisplayIcon={app}\VibeForma.exe
SetupIconFile=..\app\assets\icon.ico
CloseApplications=yes
CloseApplicationsFilter=VibeForma.exe
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked
Name: "autostart"; Description: "Start VibeForma at logon"; Flags: unchecked
Name: "deletedata"; Description: "Remove settings, models and history on uninstall"; Flags: unchecked

[Files]
Source: "{#VIBEFORMA_DIST}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VibeForma"; Filename: "{app}\VibeForma.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall VibeForma"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VibeForma"; Filename: "{app}\VibeForma.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\VibeForma"; Filename: "{app}\VibeForma.exe"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\VibeForma.exe"; Description: "{cm:LaunchProgram,VibeForma}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\VibeForma"; Tasks: deletedata
