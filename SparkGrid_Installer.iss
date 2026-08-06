#define MyAppName "SparkGrid Web Client"
#define MyAppVersion "2.20.14-beta.1"
#define MyAppPublisher "SparkGrid"
#define MyAppURL "http://127.0.0.1:8770"
#define SourceDir "C:\Program Files\testovaya versia"

[Setup]
AppId={{B8F3E7A2-4D5C-4E9B-A1F6-3C7D8E9F0A12}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\SparkGrid Web Client
DefaultGroupName=SparkGrid Web Client
DisableProgramGroupPage=yes
OutputDir=C:\Users\Warda\Downloads\SparkGrid_Build\Output
OutputBaseFilename=SparkGrid_Web_Client_Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\_internal\assets\branding\SparkGrid.ico
UninstallDisplayName=SparkGrid Web Client
SetupIconFile={#SourceDir}\_internal\assets\branding\SparkGrid.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Application code and all PyInstaller-bundled packages
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "app.py,automation_worker.py,connection_scheduler.py,instagram_web_upload.py,instagram_web_profile_workflow.py,instagram_private_web_api_upload.py,instagram_publication_verifier.py,web_warmup.py,proxy_telemetry.py,view_analytics.py,quality_account_onboarding.py,windows_playwright_guard.py"

; Patched files with logging system
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\app.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\automation_worker.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\connection_scheduler.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\instagram_web_upload.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\instagram_web_profile_workflow.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\instagram_private_web_api_upload.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\instagram_publication_verifier.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\web_warmup.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\proxy_telemetry.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\view_analytics.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\quality_account_onboarding.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\patches\windows_playwright_guard.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\log_config.py"; DestDir: "{app}\_internal"; Flags: ignoreversion overwritereadonly

; Portable Python 3.12.13 (embeddable, no external dependencies)
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; Launcher
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\SparkGrid.bat"; DestDir: "{app}"; Flags: ignoreversion

; tqdm stub
Source: "C:\Users\Warda\Downloads\SparkGrid_Build\lib\tqdm\*"; DestDir: "{app}\lib\tqdm"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\logs"; Permissions: users-full
Name: "{app}\data"; Permissions: users-full

[Icons]
Name: "{group}\SparkGrid Web Client"; Filename: "{app}\SparkGrid.bat"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\branding\SparkGrid.ico"
Name: "{group}\Uninstall SparkGrid"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SparkGrid Web Client"; Filename: "{app}\SparkGrid.bat"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\branding\SparkGrid.ico"; Tasks: desktopicon

[Run]
; Install missing Python dependencies into user AppData
Filename: "{app}\python\python.exe"; Parameters: "-m pip install numpy urllib3 certifi charset_normalizer idna annotated-doc anyio sniffio h11 click typing-extensions typing-inspection annotated-types pyee pysocks greenlet --target ""{%LOCALAPPDATA}\SparkGrid\lib"" --quiet --upgrade"; StatusMsg: "Installing runtime dependencies..."; Flags: runhidden waituntilterminated

; Launch the app (opens browser automatically via start command in .bat)
Filename: "{app}\SparkGrid.bat"; Description: "{cm:LaunchProgram,SparkGrid Web Client}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
