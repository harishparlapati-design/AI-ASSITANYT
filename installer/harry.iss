; Inno Setup script for Harry — portable-friendly Windows installer.
;
; Build (after `python build_windows.py` has produced dist\harry_dist):
;   1. Install Inno Setup 6 (free):  winget install JRSoftware.InnoSetup
;      or https://jrsoftware.org/isdl.php  (no paid service involved)
;   2. Run:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\harry.iss
;   3. Output: installer\Output\HarrySetup.exe
;
; Design notes (deliberate):
; - Uninstall NEVER touches user data: memories, settings and logs live in
;   %LOCALAPPDATA%\Harry and the uninstaller leaves them in place; a task lets
;   the user remove them explicitly with a checkbox (default off).
; - The vosk speech model is installed into the app folder, where find_asset
;   resolves it ("next to the EXE").
; - No admin rights required: machine-wide install to {autopf} would need
;   elevation, so we default to a per-user directory ({localappdata}\HarryApp).

[Setup]
AppId={{7E2C41A3-3E15-4B36-9A63-9C0D5A7F8B21}
AppName=Harry AI Assistant
AppVersion=2.0.0
AppVerName=Harry 2.0.0
DefaultDirName={localappdata}\HarryApp
DefaultGroupName=Harry
UninstallDisplayIcon={app}\Harry.exe
OutputBaseFilename=HarrySetup
OutputDir=Output
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; The whole onedir build EXcept the user's own local files that must never
; ship inside the installer (examples/templates ARE included on purpose):
Source: "..\dist\harry_dist\*"; Excludes: "*.pyc,harry_build_info.py,logs\*,data\*,config.json"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs replacesameversion

[Icons]
Name: "{group}\Harry AI Assistant"; Filename: "{app}\Harry.exe"
Name: "{group}\Harry System Check"; Filename: "{app}\Harry.exe"; Parameters: "--check"
Name: "{group}\Uninstall Harry"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Harry AI Assistant"; Filename: "{app}\Harry.exe"; Tasks: desktopicon

[Dirs]
; Data is created by Harry itself in {localappdata}\Harry on first run.
; We make sure it exists only as a courtesy (no overrides, never deleted).
Name: "{localappdata}\Harry"; Permissions: users-modify

[Run]
Filename: "{app}\Harry.exe"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; nothing automatic - user data survives uninstall unless double-confirmed below

[Code]
// First-run instructions shown after install (no paid services involved).
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then exit;
  MsgBox(
    'Harry is installed.' #13#13 +
    'Configuration, memories and logs are kept in:'#13#13 +
    ExpandConstant('{localappdata}') + '\Harry' #13#13 +
    'First run tips:'#13#13 +
    '1. Start Ollama if you use the AI brain:  ollama serve'#13 +
    '2. Double-click Harry for chat + dashboard (~http://127.0.0.1:8765),'#13#13 +
    '3. Run Harry --check any time to see the health report.',
    mbInformation, MB_OK);
end;

// On the final uninstall page, only if user has BOTH boxes ticked
// delete user data AND user has confirmed, else leave data untouched.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Delete your Harry memories, settings and logs in' + ExpandConstant('{localappdata}') + '\Harry as well?',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      if MsgBox('Last confirmation: memory cannot be undone once deleted. Delete ' + ExpandConstant('{localappdata}') + '\Harry?',
              mbConfirmation, MB_YESNO) = IDYES then
      begin
        DelTree(ExpandConstant('{localappdata}') + '\Harry', True, True, True);
      end;
    end;
  end;
end;
