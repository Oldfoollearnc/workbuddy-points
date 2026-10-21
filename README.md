# WorkBuddy 积分观测台

面向 Windows 的 WorkBuddy 积分观测小组件：常驻托盘，单击弹出右下角快捷面板，查看**本机真实**的积分消耗，需要时展开为完整数据面板。全中文界面，蓝白高级配色 + 可切换曜蓝深色主题，所有图表为手写 SVG，无第三方图表库。

## 数据来源（真实数据）

应用直接读取本机 WorkBuddy 写下的运行记录，不联网、不上传：

```
%USERPROFILE%\.workbuddy\projects\<项目>\<会话>.jsonl
  └─ providerData.rawUsage.credit   ← 每次调用的真实积分
     providerData.model / usage.totalTokens / timestamp / cwd / name
```

`usage_source.py` 负责扫描 → 按 `messageId` 去重 → 按日 / 小时 / 项目 / 调用类型聚合，并缓存到 `.cache/usage.json`（按文件 mtime 失效，刷新通常 < 0.3 秒）。

**关于余额**：本机记录只有消耗、没有账户剩余额度。未配置额度时，KPI 显示"本周期消耗"；到「设置 → 账号与额度」填入总额度与周期起止后，才会计算可用余额与预计耗尽日（保存在 `points_config.json`）。

**关于准确性**：积分、模型、项目、时段、Token 都是逐次调用的原始值，可精确对上；只有"命令执行/文件写入"这类动作标签是启发式的，所以构成图用模型而不是动作。

**拷贝给别人**：可以直接拷。程序不含账号信息，谁运行就读谁的 `~/.workbuddy`，同事看到的是他自己的数据；没有使用记录时显示"今日暂无消耗"，不会造假。

## 账号云同步（可选）

设置 → 账号与云同步 → 「连接账号」→ 微信扫码。登录后自动读取账号级余额、月周期与有效期，与本地逐次记录合并展示（余额取云端，明细取本地）。

- 登录态只存在应用自己的 WebView2 登录窗内，不读取 WorkBuddy 客户端的 Cookies；
- 只调用只读接口（`/v2/accounts`、`/v2/user/cloudagent/quota`，未登录时返回 401，已确认存在）；
- **扫一次码，以后免登录**：登录成功后 Cookie 经 DPAPI 加密存到 `session.bin`，下次启动自动回填（实测重启后无需扫码，直接同步成功）；
- 默认 60 分钟自动同步，可改为 15/30 分钟或仅手动；主界面「刷新」按钮会一并同步；
- 未连接时完整功能仍可用，只是余额显示为「未设置额度」。

## 运行

双击 `启动应用.bat` —— 启动后**只出现托盘图标**（右下角通知区，可能被折叠进 `^` 箭头里），单击图标即弹出快捷面板。

```bash
python desktop.py            # 托盘常驻（默认）
python desktop.py --show     # 启动即弹出小窗
python desktop.py --main     # 启动即打开主界面
```

无 Python 环境时可双击 `以浏览器模式预览.bat`（Edge app 模式，演示数据）。

首次运行如缺少依赖：

```bash
python -m venv .venv
.venv\Scripts\pip install pywebview pillow
```

## 启动不了？先看这两处

1. **`logs/app.log`** —— 每次启动都会写日志（窗口创建、托盘注册、对齐结果、异常堆栈）。pythonw 没有控制台，任何错误都会记在这里，并弹对话框提示。
2. **是否已有实例在运行** —— 程序单实例运行。重复双击会提示"已在运行"，此时去通知区找托盘图标（蓝色柱状图标），可能在 `^` 折叠区里。

## 交互

```
托盘图标 ──单击──> 快捷面板（424×656，贴右下角 8px）
                      └─「展开完整面板」──> 主界面（1408×916）
                                                 └─「收起为小窗」──> 快捷面板
关闭任一窗口 = 收起到托盘；托盘右键菜单「退出」= 结束进程
```

`Esc` 收起小窗 · `Ctrl+R` 重新读取本机记录 · `Alt+1..4` 切换主界面页面。

## 目录

| 路径 | 说明 |
|---|---|
| `index.html` | 双形态入口（`#popup` / `#main`） |
| `desktop.py` | 桌面启动器：双窗口、右下角对齐、单实例、日志、js_api |
| `tray.py` | 原生 Win32 托盘（单击响应、右键菜单、气泡提示） |
| `usage_source.py` | 真实用量读取与聚合（唯一数据源） |
| `assets/js/charts.js` | 图表引擎：柱状 / 堆叠 / 组合 / 环形 / 仪表 / 热力 / 排行 / 迷你趋势 |
| `assets/js/app.js` | 数据接入、路由、主题、渲染、桌面桥接 |
| `assets/js/data.js` | 浏览器预览用的模拟数据（桌面端不会用到） |
| `assets/css/tokens.css` | 明/暗两套设计令牌 |
| `docs/设计说明.md` | 页面构成、图表维度、主题与动效的完整说明 |
| `docs/screenshots/` | 真实数据下的界面截图 |
| `tools/make_icon.py` | 生成 `assets/icon.ico` |

## 环境

Windows 10/11 · WebView2 Runtime（随 Edge 预装）· Python 3.9+ · pywebview 6 · Pillow（仅生成图标时需要）
