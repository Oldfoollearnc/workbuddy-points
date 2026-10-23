; WorkBuddy 积分观测台 · Inno Setup 安装脚本
; 编译：ISCC.exe /DMyVersion=<版本> installer\WorkBuddyPoints.iss
; 产物：installer\Output\WorkBuddyPoints-Setup-<版本>.exe
;
; 设计要点
; 1) 零环境：Python 解释器、pywebview/pythonnet 运行时、前端资源全部由 PyInstaller 打进 dist 目录，
;    安装包只做"解压 + 快捷方式"，目标机不需要装 Python。
; 2) 唯一外部依赖是微软 WebView2 运行时（Win10 1803+ 通常自带，Win11 自带）。
;    安装前检测，缺失时提示下载地址，不静默失败。
; 3) 用户数据写在 %APPDATA%\WorkBuddyPoints，升级/卸载默认保留（登录态、额度缓存、日志）。
; 4) 重复安装：检测已装版本——高于已装版本提示"更新/卸载"，同版本提示"重装/卸载"，
;    低于已装版本提示"降级/卸载"，三个分支都不会动 %APPDATA% 里的用户数据。

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
UninstallDisplayName={#MyAppName}
; lzma2/max 与 ultra64 实测只差 ~60KB，但 ultra64 要 1GB+ 内存、偶发 "Out of memory"，
; 压缩比收益远小于编译失败风险，保持 max
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 左侧品牌图 164x314（modern 样式），第二份是 200% DPI；右上角 55x55 小图
WizardImageFile=art\wizard.png,art\wizard@2x.png
WizardSmallImageFile=art\wizard-small.png,art\wizard-small@2x.png
WizardImageStretch=no
; 文件属性里的版本信息，右键"属性 → 详细信息"能看出是什么包
VersionInfoVersion={#MyVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyVersion}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCompany={#MyAppPublisher}
VersionInfoCopyright=Copyright (C) {#MyAppPublisher}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 允许在向导里改成"仅为我安装"（公司电脑无管理员权限时也能装）
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
DisableWelcomePage=no
; 升级：沿用上次的安装目录与任务勾选，避免装到第二个文件夹
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
; 覆盖安装时先请仍在运行的旧版本退出（托盘程序会占用 exe）
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "startupicon"; Description: "开机自动启动（最小化到托盘）"; GroupDescription: "启动项："; Flags: unchecked

[Files]
Source: "..\dist\WorkBuddyPoints\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装完后在程序目录留一份纯文本说明，记事本直接可读（README 面向 GitHub，不适合放进安装包）
Source: "安装说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--main"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WorkBuddyPoints"; ValueData: """{app}\WorkBuddyPoints.exe"""; Flags: uninsdeletevalue; Tasks: startupicon

[UninstallDelete]
; 只删程序目录；%APPDATA%\WorkBuddyPoints（登录态/缓存/日志）保留，避免卸载后重装要重新扫码
Type: filesandordirs; Name: "{app}"

[Code]
const
  WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/';
  UNINST_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3B2C41-6E7A-4B9D-9C1E-2A5F0D7B4C11}_is1';

function WebView2Installed(): Boolean;
var
  ver: String;
begin
  Result :=
    RegQueryStringValue(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver) or
    RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver) or
    RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID, 'pv', ver);
end;

function GetInstalledVersion(): String;
var
  v: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM64, UNINST_KEY, 'DisplayVersion', v) then
    Result := v
  else if RegQueryStringValue(HKLM32, UNINST_KEY, 'DisplayVersion', v) then
    Result := v
  else if RegQueryStringValue(HKCU, UNINST_KEY, 'DisplayVersion', v) then
    Result := v;
end;

{ 取版本号的第 idx 段：1.0.12 -> idx=3 得到 12 }
function VerPart(s: String; idx: Integer): Integer;
var
  i, cnt: Integer;
  part: String;
begin
  cnt := 1;
  part := '';
  for i := 1 to Length(s) do
  begin
    if s[i] = '.' then
    begin
      Inc(cnt);
      if cnt > idx then
        Break;
    end
    else if cnt = idx then
      part := part + s[i];
  end;
  Result := StrToIntDef(part, 0);
end;

{ 1 = a 更新；0 = 相同；-1 = a 更旧 }
function CompareVer(a, b: String): Integer;
var
  i, x, y: Integer;
begin
  Result := 0;
  for i := 1 to 4 do
  begin
    x := VerPart(a, i);
    y := VerPart(b, i);
    if x > y then
    begin
      Result := 1;
      Exit;
    end;
    if x < y then
    begin
      Result := -1;
      Exit;
    end;
  end;
end;

function UninstallPrevious(): Boolean;
var
  cmd: String;
  rc: Integer;
begin
  Result := False;
  if RegQueryStringValue(HKLM64, UNINST_KEY, 'UninstallString', cmd)
     or RegQueryStringValue(HKLM32, UNINST_KEY, 'UninstallString', cmd)
     or RegQueryStringValue(HKCU, UNINST_KEY, 'UninstallString', cmd) then
  begin
    cmd := RemoveQuotes(cmd);
    if FileExists(cmd) then
      Result := Exec(cmd, '/SILENT /NORESTART', '', SW_SHOW, ewWaitUntilTerminated, rc);
  end;
  if Result then
    MsgBox('已卸载已安装版本。安装程序将退出；如需重新安装，请再次运行本安装包。',
           mbInformation, MB_OK)
  else
    MsgBox('未能自动卸载。请到「设置 → 应用 → 已安装的应用」中手动卸载后重试。',
           mbError, MB_OK);
end;

function InitializeSetup(): Boolean;
var
  cur, question: String;
  cmp: Integer;
begin
  Result := True;

  { ---------- 已安装检测：更新 / 重装 / 降级 / 卸载 ---------- }
  cur := GetInstalledVersion();
  if cur <> '' then
  begin
    cmp := CompareVer('{#MyVersion}', cur);
    if cmp > 0 then
      question := '检测到本机已安装 ' + cur + '，本安装包为 {#MyVersion}。' + #13#10 + #13#10 +
                  '是(Y)：更新到 {#MyVersion}（账号登录、额度缓存、日志等本机数据全部保留）' + #13#10 +
                  '否(N)：卸载已安装版本（卸载同样不删除用户数据）'
    else if cmp = 0 then
      question := '本机已安装相同版本 ' + cur + '。' + #13#10 + #13#10 +
                  '是(Y)：重新安装（覆盖程序文件，用户数据保留）' + #13#10 +
                  '否(N)：卸载已安装版本'
    else
      question := '本机已安装更高版本 ' + cur + '，本安装包为 {#MyVersion}（属于降级安装）。' + #13#10 + #13#10 +
                  '是(Y)：继续降级安装（用户数据保留）' + #13#10 +
                  '否(N)：卸载已安装版本';

    if MsgBox(question, mbConfirmation, MB_YESNO) = IDNO then
    begin
      UninstallPrevious();
      Result := False;
      Exit;
    end;
  end;

  { ---------- WebView2 运行时 ---------- }
  if not WebView2Installed() then
  begin
    if MsgBox('本程序需要 Microsoft Edge WebView2 运行时（Windows 10/11 通常已自带）。' + #13#10 +
              '当前未检测到，是否继续安装？安装完成后请先安装 WebView2 运行时再启动程序。' + #13#10 + #13#10 +
              WEBVIEW2_URL, mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

{ 欢迎页 / 完成页文案：讲清楚"这是什么、数据在哪、要不要重新登录" }
procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel1.Caption := '欢迎安装 {#MyAppName}';
  WizardForm.WelcomeLabel2.Caption :=
    '把本机 WorkBuddy 的额度消耗、调用笔数、模型构成整理成一块看板，' +
    '连上云端后还能逐笔对账。' + #13#10#13#10 +
    '安装前请了解三点：' + #13#10 +
    '· 纯本机运行，无广告与推广组件；' + #13#10 +
    '· 登录状态与历史数据放在用户目录，升级、重装、卸载都不会删除；' + #13#10 +
    '· 依赖 Microsoft Edge WebView2 运行时（Windows 10/11 通常已自带）。';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      '{#MyAppName} {#MyVersion} 安装完成。' + #13#10#13#10 +
      '原来的登录状态和历史数据仍在，直接打开就能用，不需要重新登录。';
end;
