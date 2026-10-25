<div align="center">

<img src="assets/icon.png" width="88" alt="WorkBuddy 积分观测台">

<h1>WorkBuddy 积分观测台</h1>

<p>把 WorkBuddy 的积分余额、消耗与逐笔流水，做成一扇常驻托盘的桌面看板。</p>

<p>
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D4" alt="Windows 10 / 11">
  <img src="https://img.shields.io/badge/python-3.13-3776AB" alt="Python 3.13">
  <img src="https://img.shields.io/badge/UI-pywebview%20%2B%20WebView2-2F6F4E" alt="pywebview + WebView2">
  <img src="https://img.shields.io/badge/license-MIT-3DA639" alt="MIT">
</p>

</div>

[截图](#界面) · [下载](#下载安装) · [数据口径](#数据从哪来) · [源码运行](#从源码运行) · [更新日志](CHANGELOG.md)

![总览](docs/screenshots/overview.png)

## 这是什么

一个只在本机运行的 Windows 桌面小工具，读两类数据：

- **本机会话记录** `~/.workbuddy/projects/**/*.jsonl` —— 逐笔、实时，带项目与 Token
- **账号云端接口** —— 余额、周期、账号级逐笔（只读）

托盘小窗瞄一眼今天用了多少；完整面板查这一个月花在哪个模型、哪个项目、哪个小时。顺带把每天要手动点的事办了：自动签到、自动派猫、到家自动领。

不需要 Python 环境，不需要服务端，不上传任何数据。

## 功能

| | |
|---|---|
| **托盘常驻** | 单击弹小窗、再点收回；右键菜单可打开主界面 / 同步云端 / 检查更新 / 切设置 / 开关开机自启 |
| **两个界面** | 右下角小窗（424×656）+ 完整面板（1408×916，四页，可缩放） |
| **总览** | 可用积分、今日消耗、笔数、Token 四张 KPI；时段分布、额度仪表、30 天趋势、消耗构成 |
| **用量分析** | 消耗日历热力图、模型排行、项目排行、今日洞察 |
| **积分流水** | 云端逐笔 + 本机记录合并，按分钟聚合，可搜索，可按模型 / 项目筛选 |
| **自动签到** | 每次启动领当日积分，已签自动跳过；结论以「成功 / 已签过 / 失败」显示在总览顶部 |
| **猫猫旅行** | 到家自动领，随后重新派出（1–4 小时带回 5–10 分）；每天只能派 1 次，派过就跳过。今日是否已派、是否已收回、收回多少都存在本地，重启直接显示 |
| **卡片轮播** | 额度 / 签到 / 猫猫 / 任务四张同尺寸卡，可拖动、点指示点、自动翻（间隔可调） |
| **额度预警** | 可用积分低于阈值（默认 500 分，可改）或今日消耗超近 7 日均值 3 倍时提醒，每天最多一次 |
| **开机自启** | 设置里开关，开机后静默驻留托盘（不弹窗），签到照常跑 |
| **导出** | 流水可导出逐笔 CSV，或先按模型 / 按月汇总（Excel 直接打开）；诊断信息打包 zip，反馈问题用 |
| **维护** | 设置里查看并清空解析缓存与运行日志，点两次确认，会话记录与账号不受影响 |

零图表库：柱状、环图、仪表、日历、迷你趋势全部手写 SVG，没有 ECharts / D3。

## 界面

<p align="center">
  <img src="docs/screenshots/analytics.png" width="49%" alt="用量分析">
  <img src="docs/screenshots/ledger.png" width="49%" alt="积分流水">
</p>

<p align="center">
  <img src="docs/screenshots/popup-light.png" width="300" alt="右下角小窗">
  <br>
  <sub>小窗：今日使用积分 / 额度 / 分时分布</sub>
</p>

## 下载安装

**推荐绿色版**（不会被浏览器拦）：从 [Releases](../../releases/latest) 下载 `WorkBuddyPoints-<版本>-portable.zip`，解压到任意目录，双击 `WorkBuddyPoints.exe` 即可，不需要安装。

**安装包**：下载 `WorkBuddyPoints-Setup-<版本>.exe`，双击安装（会创建开始菜单项；无管理员权限时选「仅为我安装」）。安装包内置 Python 运行时，目标机不需要装 Python。

> **遇到「检测到病毒」？** 本项目没有购买代码签名证书，PyInstaller 打出来的 exe 在微软看来是"来源不明的新文件"，Defender 常给 `Program:Win32:Contebrew.A!ml` 这类**机器学习误报**（`!ml` = ML 推断）。实测同一份程序，`WorkBuddyPoints.exe` 扫描是干净的，只有安装器会被标记。
>
> 放行方法：Edge 下载面板点「···」→「保留」；运行时点「更多信息」→「仍要运行」。或者直接用绿色版，不经过安装器那条判定。

**系统要求**：Windows 10 1803+ / Windows 11，x64。唯一外部依赖是系统自带的 WebView2 运行时（Win11 自带，Win10 多数已随 Edge 安装）。

**数据与卸载**：程序目录和用户数据是分开的，登录态、缓存、日志都在 `%APPDATA%\WorkBuddyPoints`：

```
%APPDATA%\WorkBuddyPoints\
  ├─ .webview-profile\   登录态（约 500 MB，最大的一块）
  ├─ account.json        账号缓存
  ├─ checkin.json        签到与猫猫的今日结论（含到达时刻、今日已领）
  └─ logs\               运行日志
```

升级、重装、卸载都**不会**删除这个目录（重装后不用重新扫码）。确定不再使用时手动删掉它即可释放约 500 MB。

## 数据从哪来

| 指标 | 来源 | 覆盖范围 | 跨设备一致 |
|---|---|---|---|
| 额度 / 余额 / 周期 / 有效期 | 云端 `billing/meter/*` | 账号级 | 是 |
| 今日消耗、模型构成、消耗日历 | 云端计费记录优先，本机 JSONL 兜底 | 账号级（云端逐笔保留最近 29 天） | 是 |
| 笔数、积分流水 | 云端 `get-user-request-usage` | 账号级（最近 29 天、最多 1500 条逐笔） | 是 |
| 项目排行 / 热力图 | 本机会话的 `cwd` | **仅这台机器** | 否 |
| Token | 本机 `usage.totalTokens` | **仅这台机器** | 否 |

两条必须知道的限制：

1. **云端返回的是计费记录，不是调用次数**。记录都落在 5 分钟整点上（按「5 分钟 × 模型」聚合），所以界面写「计费笔数」，一笔 ≠ 一次调用。
2. **金额准，笔数只能算批次**。周期内逐笔合计与账号自报「已用」实测相差约 2.4%，≥3% 时顶部会提示，并以云端已用为准。

## 隐私与安全

- **纯本机运行**：没有服务端，不上传使用记录，无广告与统计 SDK。
- **只读为主**：统计只调用 `billing/meter/*` 与 `console/accounts` 只读接口。唯一的写操作是签到与派猫（签到接口服务端幂等），都不扣费、不改账号配置，可在设置里单独关掉。
- **先查后写**：已签到就不发签到请求；已到每日派遣上限就不发派出请求。
- **今日结论落在本地**：今天签过没、猫派过没、收回没、收回多少，按天写进 `checkin.json`；启动第一帧直接照着渲染，不必等远端。今天该做的都做完了就整轮跳过接口，只在跨天或还有待领积分时才联网。
- **请求很稀疏**：拿到猫到家时间后就一觉睡到点，中途不轮询；派满后直接睡到次日 0:05。当天签成功后不再重复查询签到状态。
- **凭证加密**：登录 Cookie 用 Windows DPAPI 加密后存本机 `session.bin`，不打印、不外传。
- **本地不存敏感标识**：手机号由服务端返回时已是掩码（`185****4980`）；UID 只留不可逆哈希用于区分账号。
- **不留后门**：正式运行不开放远程调试端口（开发期需显式设 `WB_CDP=1`）；登录窗加载远端页面但不挂 `js_api`，页面拿不到任何桌面能力。

## 从源码运行

需要 Python 3.13（仓库按 3.13 开发，其它版本未验证）。

```bat
git clone https://github.com/Oldfoollearnc/workbuddy-points.git
cd workbuddy-points
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python desktop.py
```

启动参数：

| 参数 | 作用 |
|---|---|
| 无 | 打开主界面，托盘同时常驻 |
| `--show` | 启动只弹右下角小窗 |
| `--tray` | 启动不弹窗，只驻留托盘（开机自启用的就是这个） |
| `--sync` | 启动后立即同步一次并写日志（诊断用） |
| `--debug` | 打开 devtools |

只想看界面、不想装桌面依赖时，双击 `以浏览器模式预览.bat`：浏览器里用演示数据渲染同样的页面。

测试：

```bat
node tests\metrics.test.js
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 打包

```bat
:: 1) PyInstaller，产物在 dist\WorkBuddyPoints\
.venv\Scripts\python.exe -m PyInstaller --noconfirm build.spec

:: 2) Inno Setup，产物在 installer\Output\WorkBuddyPoints-Setup-<版本>.exe
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyVersion=<版本> installer\WorkBuddyPoints.iss
```

`<版本>` 必须与 `version.json` 一致，否则程序内显示和安装包版本会对不上。

> 如果 `dist\WorkBuddyPoints` 已存在，PyInstaller 会删掉它——文件多时可能被安全软件拦。
> 可以改用 `--distpath dist_build --workpath build_tmp` 输出到独立目录，装完再改名回 `dist`。

安装包约 13 MB，大头是 Python 运行时、OpenSSL 与 sqlite3；`build.spec` 已剔除 pywebview 的其它平台后端。

发布流程见 [`docs/发布流程.md`](docs/发布流程.md)。

## 目录结构

```
desktop.py            窗口 / 托盘 / 自绘标题栏 / js_api（唯一入口）
win32_utils.py        DPI、窗口样式、几何钳制、单实例信号
tray.py               原生 Win32 托盘与自绘右键菜单
usage_source.py       本机 JSONL 解析与聚合（只读）
account_service.py    云端账号：扫码登录、只读接口、Cookie 持久化
checkin_service.py    每日签到与派猫（复用网页会话，先查后写）
autostart.py          开机自启（读写 HKCU Run 键）
update_check.py       版本更新检查（GitHub Release，一天缓存）
diagnostics.py        诊断信息打包
fsutil.py             原子写 JSON（避免读到半截）
data_source.py        本机数据目录解析 + SQLite 快照读取
app_paths.py          资源目录 / 可写数据目录分离
dpapi.py              Windows DPAPI 加解密
logutil.py            日志追加写 + 轮转
index.html            双形态入口（#main / #popup）
assets/css/           设计令牌与布局
assets/js/            app.js / format.js / swipe.js / charts.js / metrics.js
build.spec            PyInstaller 配置
installer/            Inno Setup 脚本与引导图
tests/                单元测试
docs/                 架构、设计、数据源方案、截图
```

## 常见问题

**启动没反应？** 看 `%APPDATA%\WorkBuddyPoints\logs\app.log`。最常见是已有实例在跑，托盘图标被折叠进了通知区。

**找不到本机数据？** 设置 → 数据源 → 选择目录，或设环境变量 `WORKBUDDY_HOME`。

**云端一直「未连接」？** 点头像 → 连接账号重新扫码。接口属于非公开接口，服务端调整后可能需要更新版本。

**为什么项目排行是空的？** 项目来自本机会话的 `cwd`，云端不返回；换一台机器就没有历史项目。

## 许可

[MIT](LICENSE) © WorkBuddy Points contributors

本项目是个人开发的第三方工具，与 WorkBuddy、腾讯无隶属关系，也未获得其官方支持。程序依赖的云端接口并非公开 API，可能随服务端调整而失效；请自行评估使用风险。
