# WorkBuddy 积分观测台

Windows 桌面小组件：常驻托盘 → 右下角快捷面板 → 完整数据面板。
读**本机会话记录**（逐笔、实时）+ **账号云端接口**（余额、周期、账号级逐笔），
零第三方图表库（全部手写 SVG），深浅双主题。

- 版本号：`version.json`，每次 `git commit` 由 `.githooks/pre-commit` 自动递增
- 当前形态：主窗 1408×916（可缩放、可最大化）、小窗 424×656 贴右下角
- 技术栈：Python 3.13 + pywebview(WebView2) + 原生 HTML/CSS/JS

---

## 1. 先看这里：数据到底从哪来

| 口径 | 来源 | 覆盖范围 | 是否跨设备一致 |
|---|---|---|---|
| **账号额度 / 余额 / 周期 / 有效期** | 云端 `billing/meter/*` | 账号级 | 是 |
| **今日消耗、调用次数、模型构成、消耗日历** | 云端逐笔优先，本机 JSONL 兜底 | 账号级（近 30 天） | 是（云端 30 天内） |
| **积分流水** | 云端逐笔优先（`get-user-request-usage`） | 账号级（近 30 天） | 是 |
| **项目排行 / 热力图** | 本机会话 `cwd` | 仅这台机器 | 否（云端不返回项目） |
| **Token** | 本机 `usage.totalTokens` | 仅这台机器 | 否（云端不返回 Token） |

**换电脑怎么办？** 登录同一账号即可：余额、日历、模型、流水这些"账号级"数据会自动一致；
只有"项目"和"Token"两列依赖本机记录，云端行里显示 `—`。
本地明细**不做跨机同步**（见第 5 节「为什么不合并本地明细」）。

界面上凡是混用两种口径的地方都会标注：顶部提示条、日历来源 chip、流水副标题、KPI 脚注。

---

## 2. 运行

### 开发态
```bat
启动应用.bat                :: 托盘常驻（推荐）
python desktop.py --show    :: 启动即弹出小窗
python desktop.py --main    :: 启动即打开主界面
python desktop.py --debug   :: 打开 devtools
```

### 安装包（零环境）
```bat
:: 1) 打包（产出 dist\WorkBuddyPoints\）
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean build.spec

:: 2) 编译安装包（产出 installer\Output\WorkBuddyPoints-Setup-<版本>.exe）
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyVersion=1.0.3 installer\WorkBuddyPoints.iss
```
安装包已内置 Python 解释器与 pywebview/pythonnet 运行时，目标机**不需要装 Python**；
唯一外部依赖是系统自带的 **WebView2 运行时**（Win10 1803+/Win11 自带，安装时会检测并提示）。

支持"为所有用户安装"与"仅为我安装"（公司电脑无管理员权限时选后者）。

---

## 3. 目录结构

```
desktop.py            窗口/托盘/自绘标题栏/js_api（唯一入口）
tray.py               原生 Win32 托盘（单击弹出面板）
usage_source.py       本机 JSONL 解析与聚合（只读）
account_service.py    云端账号：扫码登录、四个只读接口、Cookie 持久化
data_source.py        本机数据目录解析 + SQLite 快照读取（见 docs/本地数据源方案.md）
app_paths.py          资源目录 / 可写数据目录分离（安装版写 %APPDATA%）
dpapi.py              Windows DPAPI 加解密（session.bin）
index.html            双形态入口（#main / #popup）
assets/css/tokens.css 设计令牌（明暗两套）
assets/css/app.css    布局与组件
assets/js/charts.js   手写 SVG 图表（柱/环/组合/日历/仪表/迷你线）
assets/js/app.js      渲染、路由、主题、窗口交互
assets/js/holidays.js 法定节假日表（工作日折算用）
tools/live.py         开发期自检：CDP 截图 / 求值 / 切页
build.spec            PyInstaller 配置
installer/            Inno Setup 脚本与产物
docs/                 设计说明 / 架构 / 数据源方案 / 截图
```

---

## 4. 界面与交互

| 区域 | 内容 |
|---|---|
| 顶栏（自绘） | 左：logo + 版本；中：页面标题；右：刷新本地 / 主题 / 头像（账号悬浮栏）、最小化 / 最大化 / 关闭 |
| 总览 | 数据来源提示条 → 4 KPI → 洞察条 → 今日时段（8列）｜额度与消耗（4列）→ 近 30 天趋势（8列）｜今日消耗构成（4列） |
| 用量分析 | 消耗日历（整月对齐、按列推入动画）→ 模型使用量 ｜ 项目消耗排行（同栅格对齐）→ 今日洞察 |
| 积分流水 | 汇总（云端/本机口径随来源）→ 类型与关键词筛选 → 按分钟合并的逐笔（积分列显示该分钟合计） |
| 设置 | 列表分组、竖向排列：外观 / 显示与行为 / 数据源 / 关于 |
| 小窗 | 今日使用积分（大字）+ 可用积分（小字）+ 额度 + 延迟说明 → 消耗构成环图 → 今日时段柱图 |

窗口：无系统边框（避免顶部 8px 黑边），边缘 4px 自绘缩放手柄，顶栏可拖动、双击最大化。

---

## 5. 关键设计取舍

**1）不合并本地明细到云端。**
本地 JSONL 只描述"这台机器"，把它上传/合并需要服务端，且容易重复计费。
跨设备只保证账号级数据一致（余额、日历、模型、流水），项目维度天生是机器级的。

**2）环形图数字放环外。**
曾把数值画在圆心，四位数以上会压到圆环；现在数值固定在环下方，环内只留固定短标签。

**3）一个视图只用一种可视化。**
"今日消耗构成"只保留环图（不再叠加横条与文字列表），"额度与消耗"只保留仪表盘 + 三个数。

**4）刷新按钮只读本地。**
云端请求只在"定时任务"或显式点"立即同步/同步云端"时发生，避免误触高频请求。

**5）只读、不外传。**
云端只调用四个 `POST /billing/meter/*` 只读接口 + `GET /console/accounts`；
登录 Cookie 用 DPAPI 加密存本机 `session.bin`，不打印、不上传。

---

## 6. 排查

- 启动没反应：看 `logs/app.log`（安装版在 `%APPDATA%\WorkBuddyPoints\logs`）；
  最常见是已有实例在跑（托盘图标可能被折叠进通知区）。
- 闪退：看 `logs/crash.log`（faulthandler 记录原生崩溃现场）与 `app.log` 的 `webview.exit` 行。
- 找不到本机数据：设置 → 数据源 → 选择目录，或设环境变量 `WORKBUDDY_HOME`。
- 云端一直"未连接"：点头像 → 连接账号重新扫码；日志见 `logs/api-probe.log`。

更多：`docs/设计说明.md`（界面迭代史）、`docs/架构说明.md`（模块与数据流）、
`docs/本地数据源方案.md`（公司电脑取数方案对比）。
