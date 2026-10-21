; WorkBuddy 积分观测台 · Inno Setup 安装脚本
; 编译：ISCC.exe /DMyVersion=1.0.2 installer\WorkBuddyPoints.iss
; 产物：installer\Output\WorkBuddyPoints-Setup-<version>.exe
;
; 设计要点
; 1) 零环境：Python 解释器、pywebview/pythonnet 运行时、前端资源全部由 PyInstaller 打进 dist 目录，
;    安装包只做"解压 + 快捷方式"，目标机不需要装 Python。
; 2) 唯一外部依赖是微软 WebView2 运行时（Win10 1803+ 通常自带，Win11 自带）。
;    安装前检测，缺失时提示下载地址，不静默失败。
; 3) 用户数据写在 %APPDATA%\WorkBuddyPoints，升级/卸载默认保留（登录态、额度缓存）。

#define MyAppName "WorkBuddy 积分观测台"
#define MyAppShortName "WorkBuddy Points"
#define MyAppPublisher "WorkBuddy"
#define MyAppExeName "WorkBuddyPoints.exe"
#ifndef MyVersion
  #define MyVersion "1.0.0"
#endif

[Setup]
AppId={{8F3B2C41-6E7A-4B9D-9C1E-2A5F0D7B4C11}
AppName={#MyAppName}
AppVersion={#MyVersion}
AppVerName={#MyAppName} {#MyVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=WorkBuddyPoints-Setup-{#MyVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 允许在向导里改成"仅为我安装"（公司电脑无管理员权限时也能装）
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start with Windows (minimize to tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\WorkBuddyPoints\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WorkBuddyPoints"; ValueData: """{app}\WorkBuddyPoints.exe"""; Flags: uninsdeletevalue; Tasks: startupicon

[UninstallDelete]
; 只删程序目录；%APPDATA%\WorkBuddyPoints（登录态/缓存/日志）保留，避免卸载后重装要重新扫码
Type: filesandordirs; Name: "{app}"

[Code]
const
  WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/';

function WebView2Installed(): Boolean;
var
  ver: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
  begin
    if MsgBox('本程序需要 Microsoft Edge WebView2 运行时（Windows 10/11 通常已自带）。' + #13#10 +
              '当前未检测到，是否继续安装？安装后请先安装 WebView2 运行时再启动程序。' + #13#10 + #13#10 +
              WEBVIEW2_URL, mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
