; Inno Setup 6 script. Per-user install, no administrator rights.
; Build:  ISCC /DAppVersion=<version> /DBundleDir=..\..\dist\ScreenTime installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BundleDir
  #define BundleDir "..\..\dist\ScreenTime"
#endif

[Setup]
AppId={{B7C1D3A2-5E1F-4C8A-9A55-5C0DE5C4EE01}
AppName=ScreenTime
AppVersion={#AppVersion}
AppPublisher=ScreenTime
DefaultDirName={autopf}\ScreenTime
DefaultGroupName=ScreenTime
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\dist
OutputBaseFilename=ScreenTime-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\screentime.ico
SetupIconFile={#BundleDir}\screentime.ico
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Start Menu only (Linux installs a menu entry, not a desktop icon -- same convention).
Name: "{autoprograms}\ScreenTime"; Filename: "{app}\bin\screentime-gui.exe"; Parameters: "-m screentime.gui.app"; \
  IconFilename: "{app}\screentime.ico"; WorkingDir: "{app}"
Name: "{autoprograms}\ScreenTime diagnostics"; Filename: "{app}\screentime-diagnose.cmd"; IconFilename: "{app}\screentime.ico"

[Run]
Filename: "{app}\bin\screentime-gui.exe"; Parameters: "-m screentime.gui.app"; Description: "Launch ScreenTime"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Always: stop the daemon gracefully and remove the logon task. Data is handled in [Code].
Filename: "{app}\bin\screentime-cli.exe"; Parameters: "-m screentime.platform.windows.cleanup uninstall"; \
  Flags: runhidden; RunOnceId: "StopAndUnregister"

[Code]
var
  DeleteData: Boolean;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  // Silent uninstalls keep data unless asked explicitly: /DELETEDATA
  DeleteData := False;
  if UninstallSilent then
    DeleteData := ExpandConstant('{param:DELETEDATA|0}') <> '0'
  else
    DeleteData := MsgBox('Also delete your ScreenTime usage history and its encryption key?' + #13#10 + #13#10 +
      'Choose No to keep your data (recommended if you plan to reinstall). Choosing Yes cannot be undone.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
begin
  // The cleanup tool lives in {app}, so it must run BEFORE the files are removed.
  if (CurUninstallStep = usUninstall) and DeleteData then
    Exec(ExpandConstant('{app}\bin\screentime-cli.exe'),
         '-m screentime.platform.windows.cleanup uninstall --delete-data', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;
