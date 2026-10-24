# -*- coding: utf-8 -*-
"""诊断信息导出：把排障需要的东西打成一个 zip，用户发过来就能定位问题。

**刻意不放的东西**：account.json（含手机号）、session.bin、.webview-profile（登录 cookie）。
这些对排障没有帮助，泄露风险却是实打实的。
"""
import datetime as dt
import json
import os
import platform
import sys
import zipfile

MAX_LOG_BYTES = 400_000      # 只取日志尾部，整份可能几 MB


def _log_tail(path, max_bytes=MAX_LOG_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read()
        text = raw.decode("utf-8", "replace")
        return text if size <= max_bytes else "(仅保留日志尾部)\n" + text
    except Exception as e:
        return "(读取失败: %s)" % e


def _safe_json(path, drop=()):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"_error": str(e)}
    if isinstance(data, dict):
        for k in drop:
            data.pop(k, None)
    return data


def build(dest_dir, *, version, log_path, data_dir, app_state, extra_files=()):
    """生成诊断 zip，返回绝对路径。

    app_state：调用方拼好的运行时快照（账号状态、数据源、最近同步结果等）。
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(dest_dir, "WorkBuddyPoints-诊断-%s.zip" % stamp)

    info = {
        "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "程序版本": version,
        "Python": sys.version.split()[0],
        "系统": "%s %s" % (platform.system(), platform.release()),
        "架构": platform.machine(),
        "打包运行": bool(getattr(sys, "frozen", False)),
        "数据目录": data_dir,
        "运行状态": app_state,
    }

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("info.txt", _render_text(info))
        if log_path and os.path.exists(log_path):
            z.writestr("app.log", _log_tail(log_path))
        ck = os.path.join(data_dir, "checkin.json")
        if os.path.exists(ck):
            z.writestr("checkin.json", json.dumps(_safe_json(ck), ensure_ascii=False, indent=2))
        for name, path in extra_files:
            if os.path.exists(path):
                try:
                    z.write(path, name)
                except Exception:
                    pass
    return out


def _render_text(info):
    lines = ["WorkBuddy 积分观测台 · 诊断信息", ""]
    for k, v in info.items():
        if k == "运行状态":
            continue
        lines.append("%-10s %s" % (k + "：", v))
    lines.append("")
    lines.append("—— 运行状态 ——")
    st = info.get("运行状态") or {}
    for k, v in st.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append("%-16s %s" % (k + "：", v))
    lines.append("")
    lines.append("（本文件不含手机号、cookie、登录凭证）")
    return "\n".join(lines)
