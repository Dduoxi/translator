; Inno Setup script: single-file installer for Translator.
; Build with packaging\make_installer.ps1

#define AppName "Translator"
#define AppVersion "0.1.8"
#define AppExe "Translator.exe"

[Setup]
AppId={{8F1C3A52-4C7E-4E2B-9A1D-2B6E7C0D5A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Translator
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=Translator-Setup
OutputDir=..\dist\installer
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install: no admin prompt, and the credential store entry lands in the
; user's own Windows Credential Manager, which is what keyring expects.
PrivilegesRequired=lowest
; "x64" works on every Inno Setup 6.x; "x64compatible" needs 6.3+.
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

; Chinese is NOT bundled with Inno Setup (it is an unofficial community
; translation), so referencing it aborts the compile with "Could not read file".
; The installer wizard stays English; the application itself is Chinese.
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "..\dist\Translator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %APPDATA%\Translator alone: it holds the user's config and regions.
Type: filesandordirs; Name: "{app}"
