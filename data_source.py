# -*- coding: utf-8 -*-
"""
本机 WorkBuddy 数据目录解析（#7：在公司电脑上如何找到本地数据库）

问题
----
开发机上数据固定在 `%USERPROFILE%\\.workbuddy`，但公司电脑常见三种偏差：

1. 域账号的 `%USERPROFILE%` 被重定向到网络盘，或客户端被 IT 统一改到别的盘；
2. 客户端以"多用户共用一台机器"的方式安装，数据落在 `%APPDATA%` / `%LOCALAPPDATA%`；
3. `workbuddy.db` 被客户端进程独占（WAL 模式），直接 `sqlite3.connect` 会 `database is locked`。

方案（优先级从高到低）
----------------------
1. **手动指定**：设置页「选择目录」写入 `datasource.json`，一人一次，最稳；
2. **环境变量** `WORKBUDDY_HOME`：IT 可用组策略统一注入，无需用户操作；
3. **自动探测**：`~/.workbuddy` → `%APPDATA%/WorkBuddy` → `%LOCALAPPDATA%/WorkBuddy`
   → `~/.codebuddy` → `D:/WorkBuddyData`，逐个校验结构（有 projects/ 或 workbuddy.db）；
4. 都不命中 → 返回默认路径并标记 invalid，界面显示"未找到本机记录"并可手动选择。

取舍见 docs/本地数据源方案.md：手动优先保证确定性，环境变量便于批量部署，
自动探测兜底零配置；三者叠加而不是二选一。

数据库读取
----------
一律 **先快照再读**：把 `workbuddy.db` 连同 `-wal/-shm` 复制到临时目录后只读打开。
这样既不锁客户端、也不会因为 WAL 未 checkpoint 而读到旧数据；代价是多一次几百 KB 的拷贝。
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile

import app_paths
import fsutil

CONFIG = "datasource.json"
DB_NAME = "workbuddy.db"
PROJECTS = "projects"
_SQL_HINT = "若公司电脑数据不在默认位置：设置 → 数据源 → 选择目录"


def _home():
    return os.path.expanduser("~")


def candidates():
    """按优先级列出候选目录（含来源标签，便于界面解释"为什么是这个路径"）。"""
    env = (os.environ.get("WORKBUDDY_HOME") or "").strip()
    appdata = os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")
    out = []
    if env:
        out.append((env, "环境变量 WORKBUDDY_HOME"))
    out += [
        (os.path.join(_home(), ".workbuddy"), "默认位置 %USERPROFILE%\\.workbuddy"),
        (os.path.join(appdata, "WorkBuddy"), "漫游目录 %APPDATA%\\WorkBuddy"),
        (os.path.join(local, "WorkBuddy"), "本地目录 %LOCALAPPDATA%\\WorkBuddy"),
        (os.path.join(_home(), ".codebuddy"), "同族客户端 ~/.codebuddy"),
        (os.path.join("D:" + os.sep, "WorkBuddyData"), "固定数据盘 D:\\WorkBuddyData"),
    ]
    return out


def _structure(home):
    """返回该目录的可用结构信息；不可用返回 None。"""
    if not home or not os.path.isdir(home):
        return None
    projects = os.path.join(home, PROJECTS)
    db = os.path.join(home, DB_NAME)
    has_projects = os.path.isdir(projects)
    has_db = os.path.isfile(db)
    if not (has_projects or has_db):
        return None
    return {
        "home": os.path.abspath(home),
        "projects": projects if has_projects else "",
        "db": db if has_db else "",
        "hasProjects": has_projects,
        "hasDb": has_db,
    }


def read_config():
    try:
        with open(os.path.join(app_paths.data_dir(), CONFIG), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_config(cfg):
    # 原子写：这份配置每次聚合都会 read_config()，直接 open(w) 会先截断，
    # 读端正好撞上就会拿到 {}，于是 resolve() 回落成自动探测 —— 数据目录会"自己跳走"。
    path = os.path.join(app_paths.data_dir(), CONFIG)
    fsutil.save_json(path, cfg, indent=2)
    return path


def set_home(path):
    """用户手动选择目录。目录不可用时返回 ok=False，但依然保存（便于日后目录挂载后生效）。"""
    path = os.path.abspath(os.path.expanduser(path or ""))
    cfg = read_config()
    cfg["home"] = path
    cfg["pickedAt"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_config(cfg)
    st = resolve(force=True)
    return {"ok": bool(st["valid"]), "state": st}


def clear_home():
    cfg = read_config()
    cfg.pop("home", None)
    write_config(cfg)
    return resolve(force=True)


def resolve(force=False):
    """解析当前应使用的数据目录。返回结构化的状态，供设置页与日志使用。"""
    cfg = read_config()
    manual = (cfg.get("home") or "").strip()
    tried = []

    if manual:
        st = _structure(manual)
        tried.append({"path": manual, "label": "手动指定", "ok": bool(st)})
        if st:
            st.update({"source": "manual", "valid": True, "note": "", "tried": tried})
            return st

    for path, label in candidates():
        if manual and os.path.abspath(path) == os.path.abspath(manual):
            continue
        st = _structure(path)
        tried.append({"path": path, "label": label, "ok": bool(st)})
        if st:
            st.update({"source": "auto", "valid": True, "note": label, "tried": tried})
            return st

    fallback = os.path.join(_home(), ".workbuddy")
    return {
        "home": manual or fallback,
        "projects": os.path.join(manual or fallback, PROJECTS),
        "db": os.path.join(manual or fallback, DB_NAME),
        "hasProjects": False,
        "hasDb": False,
        "source": "none",
        "valid": False,
        "note": "未找到本机 WorkBuddy 记录；" + _SQL_HINT,
        "tried": tried,
    }


# ---------------- 数据库：先快照再读 ----------------

def _snapshot_db(db_path):
    """把 SQLite（含 WAL/SHM）复制到临时目录，避免与客户端抢锁。"""
    tmpdir = tempfile.mkdtemp(prefix="wbpts-db-")
    dst = os.path.join(tmpdir, DB_NAME)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst + suffix)
                copied.append(suffix or "main")
            except Exception:
                pass
    return (dst if copied else ""), tmpdir


def db_summary(state=None, keep_tmp=False):
    """读取 session_usage，作为 JSONL 缺失时的兜底与完整性校验。

    返回 {ok, sessions, total, daily:[{date, points, calls}], error}
    - credit_json 形如 {"<requestId>": 0.59, ...}，累加即该会话总积分
    - 只有 updated_at（会话最后更新时间），没有逐笔时间 → 按"会话最后更新日"归档，
      口径标注为"数据库估算"，用于 JSONL 完全缺失时仍能看到大致用量。
    """
    state = state or resolve()
    db = state.get("db") or ""
    out = {"ok": False, "sessions": 0, "total": 0.0, "daily": [], "error": "", "source": "db"}
    if not db or not os.path.isfile(db):
        out["error"] = "未找到 workbuddy.db"
        return out

    snap, tmpdir = _snapshot_db(db)
    if not snap:
        out["error"] = "数据库被占用且无法复制"
        return out

    by_day = {}
    total = 0.0
    sessions = 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % snap.replace("\\", "/"), uri=True)
        try:
            rows = con.execute("SELECT session_id, credit_json, updated_at FROM session_usage").fetchall()
        finally:
            con.close()
        for _sid, raw, updated in rows:
            try:
                credits = json.loads(raw or "{}")
                val = sum(float(v) for v in credits.values())
            except Exception:
                continue
            if val <= 0:
                continue
            sessions += 1
            total += val
            try:
                day = dt.datetime.fromtimestamp(float(updated) / 1000).strftime("%Y-%m-%d")
            except Exception:
                continue
            rec = by_day.setdefault(day, {"date": day, "points": 0.0, "calls": 0})
            rec["points"] = round(rec["points"] + val, 2)
            rec["calls"] += len(credits)
        out.update({
            "ok": True,
            "sessions": sessions,
            "total": round(total, 2),
            "daily": [by_day[k] for k in sorted(by_day)],
        })
    except Exception as e:
        out["error"] = str(e)[:160]
    finally:
        if not keep_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out
