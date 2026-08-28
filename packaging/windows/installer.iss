; Inno Setup script for Bench Buddy.
;
; Requires Inno Setup 6.3 or newer: `ArchitecturesAllowed=x64compatible`
; replaced the older `x64` spelling in 6.3 and is not understood before it.
; `winget install --id JRSoftware.InnoSetup` gives you a current one.
;
; Built by packaging/build_windows.ps1, which passes the version and the
; PyInstaller output directory in so nothing is duplicated here:
;
;   ISCC.exe /DAppVersion=1.0.0 /DSourceDir=...\build\dist\bench-buddy ^
;            /DAssetsDir=...\packaging /O...\build\installer installer.iss
;
; Deliberate choices:
;   * PrivilegesRequired=lowest -- a per-user install into %LOCALAPPDATA%, so
;     no administrator rights and no UAC prompt.  A user who *is* an admin can
;     still choose an all-users install; PrivilegesRequiredOverridesAllowed
;     enables the dialog rather than forcing either mode.
;   * The only registry writes are the uninstall keys Inno creates for
;     Add/Remove Programs.  No file associations, no shell extensions, no Run
;     keys, no protocol handlers.
;   * The application is a PyInstaller one-folder bundle: bench-buddy.exe next
;     to an _internal directory.  Both are installed; the shortcut points at
;     the exe so the folder is never something the user has to look at.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error Define SourceDir: the PyInstaller one-folder output (build\dist\bench-buddy)
#endif
#ifndef AssetsDir
  #define AssetsDir "..\"
#endif

#define AppName        "Bench Buddy"
#define AppShortName   "bench-buddy"
#define AppPublisher   "zombu4"
#define AppExeName     "bench-buddy.exe"

[Setup]
; A stable GUID: upgrades replace in place instead of installing side by side.
AppId={{736B02DE-C1BB-4B6B-8F95-DF7EF099C492}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes

; Per-user by default: no admin rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 64-bit only -- the PyInstaller payload is x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

OutputBaseFilename={#AppShortName}-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#AssetsDir}\icons\icon.ico
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
; The program's own licence is what the installer presents for acceptance.
; The bundled typefaces stay under the SIL OFL and Qt under the LGPL; both of
; those texts ship inside the install directory (see windows\LICENSE-FONTS.txt,
; which PyInstaller places alongside the fonts in _internal).
LicenseFile={#AssetsDir}\..\LICENSE
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder bundle: the launcher plus _internal, which
; holds Python, Qt, numpy, Pillow and the bundled fonts and their licences.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Comment: "Control a Keysight 34461A bench multimeter"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller writes __pycache__ beside the payload on first run; without this
; the uninstaller leaves the directory behind.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
