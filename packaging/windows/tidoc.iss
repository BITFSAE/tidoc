#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\tidoc"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif

[Setup]
AppId={{C4B0DD63-6D7E-4D9E-A492-6925D73BE91A}
AppName=Tidoc
AppVersion={#MyAppVersion}
AppVerName=Tidoc {#MyAppVersion}
AppPublisher=totok22
AppPublisherURL=https://github.com/totok22/tidoc
AppSupportURL=https://github.com/totok22/tidoc/issues
AppUpdatesURL=https://github.com/totok22/tidoc/releases
DefaultDirName={localappdata}\Programs\Tidoc
DefaultGroupName=Tidoc
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=tidoc-core-windows-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\tidoc.exe
VersionInfoVersion={#MyAppVersion}.0

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Tidoc"; Filename: "{app}\tidoc.exe"
Name: "{autodesktop}\Tidoc"; Filename: "{app}\tidoc.exe"; Tasks: desktopicon

; .tidoc 绑定包关联：双击用 Tidoc 打开，图标取自 tidoc.exe 内嵌 logo。
[Registry]
Root: HKA; Subkey: "Software\Classes\.tidoc"; ValueType: string; ValueName: ""; ValueData: "Tidoc.Bindle"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Tidoc.Bindle"; ValueType: string; ValueName: ""; ValueData: "Tidoc 绑定包"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Tidoc.Bindle\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\tidoc.exe,0"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Tidoc.Bindle\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\tidoc.exe"" ""%1"""; Flags: uninsdeletekey

[Run]
Filename: "{app}\tidoc.exe"; Description: "启动 Tidoc"; Flags: nowait postinstall skipifsilent unchecked
