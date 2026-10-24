# -*- coding: utf-8 -*-
"""
WorkBuddy 真实用量数据源

数据来源（本机，只读）
----------------------
1. %USERPROFILE%\\.workbuddy\\projects\\<project>\\<session>.jsonl
   每次模型调用的 providerData.rawUsage.credit = 该次真实积分消耗，
   同时带 model / timestamp / cwd / usage(totalTokens) / 工具名。
2. %USERPROFILE%\\.workbuddy\\workbuddy.db  session_usage 表
   credit_json: {conversationRequestId: credit}，用于校验与补全。

余额说明
--------
本机记录只有"消耗"，没有账号额度余额。因此额度由使用者在设置里填写
（总额度 + 周期起止），可用余额 = 总额度 - 周期内累计消耗；未配置时
界面显示"本周期消耗"而不是编造一个余额。
"""
import datetime as dt
import json
import os
import threading
import time
from collections import defaultdict

import app_paths
import fsutil
import data_source
import logutil

HOME = os.path.expanduser("~")
# 数据目录不再写死：由 data_source 解析（手动指定 > 环境变量 > 自动探测）
# 这样在公司电脑上把数据放在 D 盘或网络盘时也能读到，见 docs/本地数据源方案.md
CONFIG_PATH = app_paths.data_path("points_config.json")
CACHE_PATH = app_paths.data_path(".cache", "usage.json")
LOG_PATH = app_paths.data_path("logs", "app.log")
# v5：缓存从"聚合结果快照"改成"按文件的逐笔记录 + 已读字节偏移"。
# 这样 refresh 只需要从上次读到的位置往后读新增字节，不用每次重扫全部会话文件。
CACHE_VER = 5


def log(msg):
    logutil.log(LOG_PATH, msg, "usage")

MODEL_NAMES = {
    "deepseek-v4.1-flash": "DeepSeek-V4.1-Flash",
    "deepseek-v3.2": "DeepSeek-V3.2",
    "hy3": "混元 Hy3",
    "hunyuan": "混元",
    "claude-sonnet-4.5": "Claude Sonnet 4.5",
    "claude-opus-4.1": "Claude Opus 4.1",
    "gpt-5": "GPT-5",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
}

TOOL_GROUPS = {
    "Read": "文件读取", "Write": "文件写入", "Edit": "文件编辑", "MultiEdit": "文件编辑",
    "Bash": "命令执行", "PowerShell": "命令执行", "Glob": "文件检索", "Grep": "内容检索",
    "WebSearch": "联网检索", "WebFetch": "网页抓取",
    "ImageGen": "图像生成", "VideoGen": "视频生成", "present_files": "文件交付",
    "Agent": "子任务编排", "TaskCreate": "任务管理", "Skill": "技能调用",
    "__default__": "工具调用",
}

TONE_ORDER = ["c1", "c2", "c3", "c4", "c5", "c6"]


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "total": float(cfg.get("total") or 0),
            "cycle_start": cfg.get("cycle_start") or "",
            "cycle_end": cfg.get("cycle_end") or "",
            "cover_days": int(cfg.get("cover_days") or 0),
        }
    except Exception:
        return {"total": 0, "cycle_start": "", "cycle_end": "", "cover_days": 0}


def save_config(total, cycle_start, cycle_end):
    keep = _load_config()
    cfg = {"total": float(total or 0), "cycle_start": cycle_start or "",
           "cycle_end": cycle_end or "", "cover_days": keep.get("cover_days") or 0}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def save_cover_days(days):
    """统计覆盖天数：0 = 全部记录；N = 只聚合最近 N 天（额度周期不受影响）。"""
    cfg = _load_config()
    try:
        days = max(0, int(days or 0))
    except (TypeError, ValueError):
        days = 0
    out = {"total": cfg["total"], "cycle_start": cfg["cycle_start"],
           "cycle_end": cfg["cycle_end"], "cover_days": days}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def _friendly_model(raw):
    if not raw:
        return "未知模型"
    key = raw.lower()
    if key in MODEL_NAMES:
        return MODEL_NAMES[key]
    return raw[0].upper() + raw[1:]


def _tool_group(name, typ, role):
    if not name:
        return "对话生成" if role == "assistant" else "其他调用"
    return TOOL_GROUPS.get(name, TOOL_GROUPS["__default__"])


def _mix(points_map, total):
    """按消耗排序并补充占比"""
    items = sorted(points_map.items(), key=lambda kv: -kv[1])
    out = []
    for i, (name, val) in enumerate(items):
        out.append({
            "name": name,
            "points": round(val, 2),
            "share": (val / total) if total else 0,
            "tone": TONE_ORDER[i % len(TONE_ORDER)],
        })
    return out


def _scan_file(path, offset=0):
    """从 offset 起增量解析一个 jsonl，返回 (records, 新偏移)。

    只消费到最后一个换行符为止：会话正在写入时最后一行可能只写了一半，
    留在下一轮再读，避免把半截 JSON 当损坏行永久跳过。
    """
    base = max(0, int(offset or 0))
    try:
        with open(path, "rb") as f:
            f.seek(base)
            buf = f.read()
    except OSError as e:
        log(f"read {path} failed: {e}")
        return [], base
    if not buf:
        return [], base
    end = buf.rfind(b"\n")
    if end < 0:
        return [], base                     # 只有半行，等下次

    out = []
    for raw in buf[:end + 1].split(b"\n"):
        if not raw or b'"credit"' not in raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            continue
        pd = obj.get("providerData") or {}
        ru = pd.get("rawUsage") or {}
        if "credit" not in ru:
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        try:
            credit = float(ru.get("credit") or 0)
        except (TypeError, ValueError):
            credit = 0.0
        usage = pd.get("usage") or {}
        tokens = usage.get("totalTokens") or ru.get("total_tokens") or 0
        model = pd.get("model") or ""
        tool = obj.get("name") or ""
        # 去重键：优先 messageId / id，缺失时退回"内容指纹"。
        # 旧实现 mid 为 None 时，第一条之后的所有无 id 记录都会被判成重复丢掉。
        mid = pd.get("messageId") or obj.get("id") or "%s|%s|%s|%s" % (ts, model, credit, tool)
        out.append({
            "mid": str(mid),
            "ts": int(ts),
            "credit": credit,
            "model": model,
            "tokens": int(tokens or 0),
            "cwd": obj.get("cwd") or "",
            "tool": tool,
            "type": obj.get("type") or "",
            "role": obj.get("role") or "",
        })
    return out, base + end + 1


def _monthday_to_date(md, today):
    """把 "MM-DD" 还原成日期：跨年时按"不晚于今天"推断年份。"""
    try:
        m, d = (int(x) for x in str(md).split("-"))
    except (TypeError, ValueError):
        return None
    for y in (today.year, today.year - 1):
        try:
            cand = dt.date(y, m, d)
        except ValueError:
            return None
        if cand <= today:
            return cand
    return None


class UsageService:
    def __init__(self):
        self._data = None
        self._lock = threading.Lock()
        self._thread = None
        self._started_at = 0
        self.source = data_source.resolve()   # 最近一次解析到的数据目录

    # ---------- 对外 ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._build_safe, name="usage", daemon=True)
        self._thread.start()

    def get(self, wait=12):
        deadline = time.time() + wait
        while time.time() < deadline:
            with self._lock:
                if self._data is not None:
                    return self._data
            time.sleep(0.05)
        # 超时：返回空壳，避免界面卡死
        with self._lock:
            return self._data or self._empty()

    def refresh(self):
        # 不再 force：_build 内部按文件 (mtime,size) 增量读，只有变动的文件才重新解析。
        # 以前这里强制全量重扫，127MB 会话目录每次约 0.6s，且随历史线性变慢。
        self._build_safe()
        return self.get(3)

    # ---------- 构建 ----------

    def _build_safe(self, force=False):
        try:
            data = self._build(force)
        except Exception as e:
            data = self._empty()
            data["meta"]["error"] = str(e)
        with self._lock:
            self._data = data

    def _empty(self):
        now = dt.datetime.now()
        daily = []
        for i in range(29, -1, -1):
            d = now.date() - dt.timedelta(days=i)
            daily.append({"date": d.strftime("%m-%d"),
                          "weekday": "日一二三四五六"[d.isoweekday() % 7],
                          "consumed": 0, "earned": 0, "balance": 0, "calls": 0, "tokens": 0})
        src = getattr(self, "source", None) or {}
        return {
            "meta": {"now": now.isoformat(), "updated": now.strftime("%H:%M"),
                     "updatedFull": now.strftime("%m-%d %H:%M"), "nowHour": now.hour,
                     "source": "local", "records": 0, "days": 0, "error": None,
                     "coverDays": 0, "recordsTotal": 0, "daysTotal": 0,
                     "sourcePath": src.get("home") or "", "sourceKind": "none",
                     "sourceNote": src.get("note") or ""},
            "account": {"total": 0, "used": 0, "available": 0, "usedPct": 0,
                        "configured": False, "plan": "本机用量", "id": "本机记录",
                        "cycleEnd": now.isoformat()},
            "today": {"consumed": 0, "earned": 0, "requests": 0, "tokens": 0,
                      "avgPerRequest": 0, "deltaPct": 0},
            "hourly": [{"hour": h, "label": f"{h:02d}:00", "points": 0, "requests": 0,
                        "tokens": 0, "isFuture": h > now.hour, "isNow": h == now.hour} for h in range(24)],
            "models": [], "models30": [], "tasks": [], "projects": [], "projects7": [],
            "daily": daily,
            "week": [{"date": "", "weekday": "", "total": 0, "values": [0, 0, 0, 0, 0], "isToday": i == 6} for i in range(7)],
            "heat": [], "heatMax": 1, "ledger": [], "recent": [], "insights": [],
            "avg7": 0, "daysLeft": 0, "peakHour": 0, "peakPoints": 0,
            "modelDefs": [], "taskDefs": [], "runOutDate": now.isoformat(),
            "ledgerTodayTotal": 0, "pctOfQuota": 0,
        }

    def _build(self, force=False):
        # 每次构建都重新解析数据目录：用户可能在设置里刚换了目录
        self.source = data_source.resolve()
        projects = self.source.get("projects") or ""
        files = []
        if projects and os.path.isdir(projects):
            for root, _dirs, names in os.walk(projects):
                for n in names:
                    if n.endswith(".jsonl"):
                        p = os.path.join(root, n)
                        try:
                            st = os.stat(p)
                        except OSError as e:
                            log(f"stat {p} failed: {e}")
                            continue
                        files.append((p, st.st_mtime, st.st_size))
        files.sort()

        cache = None if force else self._load_cache()
        if cache is not None and cache.get("home") != self.source["home"]:
            cache = None                     # 换过数据目录，旧缓存作废
        prev = (cache or {}).get("files") or {}
        stats, merged = {}, {}
        dirty = cache is None
        tail_reads = 0

        for path, mtime, size in files:
            ent = prev.get(path)
            if ent and ent.get("size") == size and abs(float(ent.get("mtime") or 0) - mtime) < 1e-6:
                # 文件没动过：直接复用上次的逐笔
                recs, offset = ent.get("records") or [], int(ent.get("offset") or 0)
            elif (ent and size >= int(ent.get("size") or 0)
                  and int(ent.get("offset") or 0) <= size
                  and mtime >= float(ent.get("mtime") or 0)):
                # 常见路径：会话文件只在末尾追加 → 只读新增的那一段
                tail, offset = _scan_file(path, int(ent.get("offset") or 0))
                recs = (ent.get("records") or []) + tail
                dirty = True
                tail_reads += 1
            else:
                # 被截断 / 被重写 / 首次见：整文件重扫
                recs, offset = _scan_file(path, 0)
                dirty = True
            stats[path] = {"mtime": mtime, "size": size, "offset": offset, "records": recs}
            for r in recs:
                merged.setdefault(r["mid"], r)      # 同一 message 可能跨文件重复
        if set(stats) != set(prev):
            dirty = True

        records = sorted(merged.values(), key=lambda r: r["ts"])
        if not records and self.source.get("hasDb"):
            # JSONL 完全没有（公司策略清理 / 换机）时，用 workbuddy.db 的会话汇总兜底
            data = self._from_db(data_source.db_summary(self.source))
        else:
            data = self._aggregate(records)
        data = self._ensure_shape(data)
        data["meta"]["cached"] = not dirty
        if dirty:
            try:
                fsutil.save_json(CACHE_PATH,
                                 {"ver": CACHE_VER, "home": self.source["home"], "files": stats})
            except Exception as e:
                log(f"cache write error: {e}")
        if tail_reads:
            log(f"incremental scan: {tail_reads} 个文件追加读取，共 {len(records)} 条记录")
        return data

    def _load_cache(self):
        """读缓存；结构/版本不符直接丢弃（返回 None）。损坏的缓存不该让程序起不来。"""
        cache = None
        for attempt in range(3):
            try:
                with open(CACHE_PATH, encoding="utf-8") as f:
                    cache = json.load(f)
                break
            except FileNotFoundError:
                return None
            except Exception as e:
                # 撞上写端正在替换文件时会读到半截，隔一会儿重读通常就正常了
                if attempt == 2:
                    log(f"cache read error: {e}")
                    return None
                time.sleep(0.05)
        if not isinstance(cache, dict) or cache.get("ver") != CACHE_VER:
            return None
        if not isinstance(cache.get("files"), dict):
            return None
        return cache

    def _ensure_shape(self, data):
        """把返回值补齐到和 `_empty()` 同一套字段。

        以前 `_empty()` 与 `_aggregate()` 是两份各自维护的结构，漏一个键，
        前端那个数字就变 undefined。这里以 `_empty()` 当唯一模板补缺，
        以后新增字段只改一处。
        """
        blank = self._empty()
        for k, v in blank.items():
            if k == "meta":
                data.setdefault("meta", {})
                for mk, mv in v.items():
                    data["meta"].setdefault(mk, mv)
            else:
                data.setdefault(k, v)
        return data

    @staticmethod
    def _scan(files):
        """全量扫描（测试与兜底用；生产路径走 _scan_file 增量读）。"""
        out = []
        for path, _mtime, _size in files:
            recs, _off = _scan_file(path, 0)
            out.extend(recs)
        out.sort(key=lambda r: r["ts"])
        return out

    # ---------- 聚合 ----------

    def _aggregate(self, records):
        now = dt.datetime.now()
        today = now.date()
        cfg = _load_config()

        def day_of(r):
            return dt.datetime.fromtimestamp(r["ts"] / 1000).date()

        # 统计窗口（设置 → 数据源 → 覆盖天数）：只聚合最近 N 天，
        # 但"周期已用额度"必须按全部记录算，否则窗口一变可用余额就失真。
        all_records = records
        cover_days = int(cfg.get("cover_days") or 0)
        if cover_days > 0:
            cutoff = today - dt.timedelta(days=cover_days - 1)
            records = [r for r in records if day_of(r) >= cutoff]
        all_days = {day_of(r) for r in all_records}

        by_day = defaultdict(float)
        by_day_req = defaultdict(int)
        by_day_tok = defaultdict(int)
        for r in records:
            d = day_of(r)
            by_day[d] += r["credit"]
            by_day_req[d] += 1
            by_day_tok[d] += r["tokens"]

        today_recs = [r for r in records if day_of(r) == today]
        yest_recs = [r for r in records if day_of(r) == today - dt.timedelta(days=1)]

        today_credit = sum(r["credit"] for r in today_recs)
        yest_credit = sum(r["credit"] for r in yest_recs)
        today_tokens = sum(r["tokens"] for r in today_recs)
        today_reqs = len(today_recs)

        hourly = []
        for h in range(24):
            rs = [r for r in today_recs if dt.datetime.fromtimestamp(r["ts"] / 1000).hour == h]
            hourly.append({
                "hour": h,
                "label": f"{h:02d}:00",
                "points": round(sum(x["credit"] for x in rs), 2),
                "requests": len(rs),
                "tokens": sum(x["tokens"] for x in rs),
                "isFuture": h > now.hour,
                "isNow": h == now.hour,
            })

        peak = max(hourly, key=lambda x: x["points"]) if hourly else hourly[0]

        by_model = defaultdict(float)
        by_model_calls = defaultdict(int)
        for r in today_recs:
            name = _friendly_model(r["model"])
            by_model[name] += r["credit"]
            by_model_calls[name] += 1
        models = _mix(by_model, today_credit)
        # 调用次数：今日消耗构成按"次数"拆环图，积分占比只作参考
        for m in models:
            m["calls"] = by_model_calls.get(m["name"], 0)

        by_tool = defaultdict(float)
        for r in today_recs:
            by_tool[_tool_group(r["tool"], r["type"], r["role"])] += r["credit"]

        by_proj = defaultdict(float)
        for r in today_recs:
            cwd = r["cwd"].rstrip("\\/")
            by_proj[os.path.basename(cwd) or "未归类"] += r["credit"]
        projects = _mix(by_proj, today_credit)

        # 近 7 天调用类型（取总量前 5，其余合并为「其他」），统一用于环形图 / 堆叠图 / 热力图
        week_tools = defaultdict(float)
        for r in records:
            d = dt.datetime.fromtimestamp(r["ts"] / 1000).date()
            if (today - d).days < 7:
                week_tools[_tool_group(r["tool"], r["type"], r["role"])] += r["credit"]
        ranked = sorted(week_tools.items(), key=lambda kv: -kv[1])
        top_tools = [k for k, _v in ranked[:5]]
        task_defs = [{"key": k, "name": k, "tone": TONE_ORDER[i % len(TONE_ORDER)]}
                     for i, k in enumerate(top_tools + (["其他"] if len(ranked) > 5 else []))]

        def group_of(name):
            return name if name in top_tools else "其他"

        grouped_today = defaultdict(float)
        for name, v in by_tool.items():
            grouped_today[group_of(name)] += v
        tasks = _mix(grouped_today, today_credit)

        # 近 30 天
        daily = []
        for i in range(29, -1, -1):
            d = today - dt.timedelta(days=i)
            daily.append({
                "date": d.strftime("%m-%d"),
                "weekday": "日一二三四五六"[d.isoweekday() % 7],
                "consumed": round(by_day.get(d, 0), 2),
                "earned": 0,
                "calls": by_day_req.get(d, 0),
                "tokens": by_day_tok.get(d, 0),
            })

        # 近 30 天 × 模型（模型使用量卡片的本机兜底口径，含调用次数与 Token）
        by_model30 = defaultdict(lambda: {"points": 0.0, "calls": 0, "tokens": 0})
        for r in records:
            if (today - day_of(r)).days < 30:
                it = by_model30[_friendly_model(r["model"])]
                it["points"] += r["credit"]
                it["calls"] += 1
                it["tokens"] += r["tokens"]
        models30 = [{"name": k, "points": round(v["points"], 2), "calls": v["calls"], "tokens": v["tokens"]}
                    for k, v in sorted(by_model30.items(), key=lambda kv: -kv[1]["points"])]

        # 余额：以配置额度为准
        configured = cfg["total"] > 0
        cycle_start = cfg["cycle_start"]
        cycle_end = cfg["cycle_end"]
        used_in_cycle = 0.0
        if cycle_start:
            try:
                sd = dt.datetime.strptime(cycle_start, "%Y-%m-%d").date()
                used_in_cycle = sum(r["credit"] for r in all_records if sd <= day_of(r) <= today)
            except ValueError:
                used_in_cycle = sum(r["credit"] for r in all_records)
        else:
            used_in_cycle = sum(r["credit"] for r in all_records)
        available = max(0.0, cfg["total"] - used_in_cycle) if configured else 0.0
        used_pct = (used_in_cycle / cfg["total"]) if configured and cfg["total"] else 0.0

        running = available
        for i in range(len(daily) - 1, -1, -1):
            daily[i]["balance"] = round(running, 2)
            running += daily[i]["consumed"]

        # 近 7 日（不含今日）均值
        prev7 = [by_day.get(today - dt.timedelta(days=i), 0) for i in range(1, 8)]
        avg7 = sum(prev7) / 7 if prev7 else 0
        days_left = (available / avg7) if (configured and avg7 > 0) else 0
        run_out = now + dt.timedelta(days=days_left) if days_left else now

        # 近 7 天项目构成（用于环形图与排行）
        by_proj7 = defaultdict(float)
        for r in records:
            d = dt.datetime.fromtimestamp(r["ts"] / 1000).date()
            if (today - d).days < 7:
                cwd = r["cwd"].rstrip("\\/")
                by_proj7[os.path.basename(cwd) or "未归类"] += r["credit"]
        top_proj = sorted(by_proj7.items(), key=lambda kv: -kv[1])[:5]
        folded = defaultdict(float)
        for name, v in by_proj7.items():
            folded[name if name in dict(top_proj) else "其他项目"] += v
        projects7 = _mix(folded, sum(folded.values()))

        # 近 7 天模型序列（模型是计费主体，归因 100% 准确）
        week_models = defaultdict(float)
        for r in records:
            d = dt.datetime.fromtimestamp(r["ts"] / 1000).date()
            if (today - d).days < 7:
                week_models[_friendly_model(r["model"])] += r["credit"]
        top_models = [k for k, v in sorted(week_models.items(), key=lambda kv: -kv[1]) if v > 0][:5]
        model_defs = [{"key": k, "name": k, "tone": TONE_ORDER[i % len(TONE_ORDER)]}
                      for i, k in enumerate(top_models)]

        def m_of(name):
            return name if name in top_models else "其他"

        week = []
        for i in range(6, -1, -1):
            d = today - dt.timedelta(days=i)
            vals = defaultdict(float)
            for r in records:
                if dt.datetime.fromtimestamp(r["ts"] / 1000).date() == d:
                    vals[m_of(_friendly_model(r["model"]))] += r["credit"]
            total_d = sum(vals.values())
            week.append({
                "date": d.strftime("%m-%d"),
                "weekday": "日一二三四五六"[d.isoweekday() % 7],
                "total": round(total_d, 2),
                "values": [round(vals.get(k, 0), 2) for k in top_models],
                "isToday": i == 0,
            })

        # 热力：项目 × 小时（项目取 cwd，归因同样精确）
        heat_projects = [n for n, v in sorted(by_proj.items(), key=lambda kv: -kv[1]) if v > 0][:5]
        heat = []
        for name in heat_projects:
            row = []
            for h in range(24):
                v = sum(r["credit"] for r in today_recs
                        if dt.datetime.fromtimestamp(r["ts"] / 1000).hour == h
                        and (os.path.basename(r["cwd"].rstrip("\\/")) or "未归类") == name)
                row.append(round(v, 2))
            heat.append({"name": name, "tone": "c1", "values": row, "points": round(sum(row), 2)})
        for i, row in enumerate(heat):
            row["tone"] = TONE_ORDER[i % len(TONE_ORDER)]
        heat_max = max([max(r["values"] or [0]) for r in heat] or [1]) or 1

        # 流水（最近 240 条）
        # 为什么要这么多：云端一小时才同步一次，界面上的"实时"靠"最后 N 条本机逐笔"补足最近这一段，
        # 条数太少会在高强度使用时补不全（一次对话可能产生几十条模型调用）。
        ledger = []
        for r in sorted(records, key=lambda x: -x["ts"])[:240]:
            t = dt.datetime.fromtimestamp(r["ts"] / 1000)
            cwd = r["cwd"].rstrip("\\/")
            ledger.append({
                "id": str(r["ts"]) + r["model"],
                "time": t.strftime("%Y-%m-%dT%H:%M:%S"),
                "type": "consume",
                "points": round(r["credit"], 2),
                "model": _friendly_model(r["model"]),
                "task": _tool_group(r["tool"], r["type"], r["role"]),
                "project": os.path.basename(cwd) or "未归类",
                "tokens": r["tokens"],
                "requests": 1,
                "tone": "c1",
            })

        data_start = min(by_day.keys()).strftime("%Y-%m-%d") if by_day else today.strftime("%Y-%m-%d")

        # 最近 72 小时逐笔时间线：[毫秒时间戳, 积分]
        # 用途：云端余额是"上次同步那一刻"的快照，减去这段时间本地又花掉的量，
        # 才是此刻真实的可用积分（云端入账有延迟，单靠余额会偏高）。
        recent_cut = int(time.time() - 72 * 3600) * 1000
        recent = [[int(r["ts"]), round(r["credit"], 4)] for r in all_records if r["ts"] >= recent_cut]
        recent.sort(key=lambda x: x[0])

        insights = []
        if today_credit > 0:
            top = models[0] if models else None
            insights.append({
                "icon": "i-bolt",
                "title": "今日消耗已发生",
                "text": f"今日累计 <b>{today_credit:.2f} 分</b>，共 <b>{today_reqs}</b> 次调用；"
                        f"峰值时段 <b>{peak['hour']:02d}:00</b>，占今日 <b>{(peak['points'] / today_credit * 100):.1f}%</b>。",
            })
            if top:
                insights.append({
                    "icon": "i-chart",
                    "title": "模型消耗结构",
                    "text": f"<b>{top['name']}</b> 占今日 <b>{top['share'] * 100:.1f}%</b>（{top['points']:.2f} 分），"
                            f"今日共涉及 <b>{len(tasks)}</b> 类调用。",
                })
        else:
            insights.append({"icon": "i-clock", "title": "今日暂无消耗",
                             "text": "本机记录中今天还没有积分消耗，发起一次对话后即可看到实时用量。"})
        if configured:
            insights.append({
                "icon": "i-clock",
                "title": "额度可用性",
                "text": f"周期内已用 <b>{used_in_cycle:.2f} / {cfg['total']:.0f} 分</b>，"
                        f"按近 7 日均值 <b>{avg7:.2f} 分/天</b> 估算，约可再用 <b>{days_left:.1f} 天</b>。",
            })
        else:
            insights.append({
                "icon": "i-info",
                "title": "额度来自云端",
                "text": "本机只记录消耗、不含账户余额。到「设置 → 账号额度」填写总额度与周期，即可显示可用积分与预估天数。",
            })

        return {
            "meta": {
                "now": now.isoformat(),
                "updated": now.strftime("%H:%M"),
                "updatedFull": now.strftime("%m-%d %H:%M"),
                "nowHour": now.hour,
                "source": "local",
                "records": len(records),
                "days": len(by_day),
                "coverDays": cover_days,
                "recordsTotal": len(all_records),
                "daysTotal": len(all_days),
                "firstDay": data_start,
                "error": None,
                "sourcePath": getattr(self, "source", {}).get("home") or "",
                "sourceKind": "jsonl",
                "sourceNote": getattr(self, "source", {}).get("note") or "",
            },
            "account": {
                "total": round(cfg["total"], 2),
                "used": round(used_in_cycle, 2),
                "available": round(available, 2),
                "usedPct": round(used_pct, 4),
                "configured": configured,
                "plan": "本机用量",
                "id": "本机记录",
                "cycleEnd": cycle_end or "",
                "cycleStart": cycle_start or "",
            },
            "today": {
                "consumed": round(today_credit, 2),
                "earned": 0,
                "requests": today_reqs,
                "tokens": today_tokens,
                "avgPerRequest": round(today_credit / today_reqs, 2) if today_reqs else 0,
                "deltaPct": round((today_credit / yest_credit - 1) * 100, 1) if yest_credit else 0,
            },
            "hourly": hourly,
            "peakHour": peak["hour"],
            "peakPoints": peak["points"],
            "models": models,
            "models30": models30,
            "modelDefs": model_defs,
            "tasks": tasks,
            "taskDefs": task_defs,
            "projects": projects,
            "projects7": projects7,
            "daily": daily,
            "week": week,
            "heat": heat,
            "heatMax": heat_max,
            "ledger": ledger,
            "recent": recent,
            "ledgerTodayTotal": round(today_credit, 2),
            "avg7": round(avg7, 2),
            "daysLeft": round(days_left, 1),
            "runOutDate": run_out.strftime("%Y-%m-%d"),
            "insights": insights,
            "pctOfQuota": round((today_credit / cfg["total"]), 4) if configured and cfg["total"] else 0,
        }

    def _from_db(self, summary):
        """JSONL 缺失时的兜底：按 workbuddy.db 的会话汇总折算每日用量。

        口径：数据库只有"会话最后更新时间"，没有逐笔时间，只能按最后更新日归档。
        因此结果必须标注为「数据库估算」，不能当作精确流水展示。
        """
        data = self._empty()
        by_day = {}
        for rec in (summary.get("daily") or []):
            d = _monthday_to_date(rec.get("date"), dt.date.today()) if rec.get("date") else None
            if d:
                by_day[d] = rec
        today = dt.date.today()
        today_pts, today_calls = 0.0, 0
        for item in data["daily"]:
            d = _monthday_to_date(item["date"], today)
            rec = by_day.get(d) if d else None
            if not rec:
                continue
            item["consumed"] = rec["points"]
            item["calls"] = rec["calls"]
            item["tokens"] = 0
            if d == today:
                today_pts, today_calls = rec["points"], rec["calls"]
        data["today"].update({
            "consumed": round(today_pts, 2),
            "requests": today_calls,
            "avgPerRequest": round(today_pts / today_calls, 2) if today_calls else 0,
        })
        data["meta"].update({
            "source": "db",
            "sourceKind": "db",
            "sourcePath": self.source.get("db") or "",
            "sourceNote": "未找到 JSONL 明细，已按 workbuddy.db 的会话汇总估算（仅日级）",
            "records": 0,
            "days": len(by_day),
        })
        data["insights"] = [{
            "icon": "i-info",
            "title": "正在使用数据库估算",
            "text": "没有找到会话 JSONL 明细，已改用 <b>workbuddy.db</b> 的会话汇总："
                    "只能给出<b>按天</b>的量，无法拆到模型与项目。"
                    "若数据在别的目录，请到「设置 → 数据源」重新选择。",
        }]
        return data
