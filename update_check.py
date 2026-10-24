# -*- coding: utf-8 -*-
"""版本更新检查：查 GitHub 最新 Release，和自己的 version.json 比一下。

只查不下载、不自动更新 —— 用户没要求的自动升级是负担。
结果缓存一天，失败一律静默（查不到新版本不该影响任何人）。
"""
import json
import time
import urllib.request

API = "https://api.github.com/repos/Oldfoollearnc/workbuddy-points/releases/latest"


def _parse(v):
    """'1.8.0' / 'v1.8.0' → (1, 8, 0)，解析不了的段按 0 算。"""
    parts = []
    for seg in str(v or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check(current, timeout=8.0):
    """返回 {ok, current, latest, hasUpdate, url, error}。"""
    out = {"ok": False, "current": current, "latest": "", "hasUpdate": False,
           "url": "https://github.com/Oldfoollearnc/workbuddy-points/releases",
           "error": None}
    try:
        req = urllib.request.Request(API, headers={
            "User-Agent": "WorkBuddyPoints/%s" % current,
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = str(data.get("tag_name") or "").lstrip("vV")
        if not latest:
            out["error"] = "没有取到版本号"
            return out
        out.update({
            "ok": True,
            "latest": latest,
            "hasUpdate": _parse(latest) > _parse(current),
            "url": data.get("html_url") or out["url"],
        })
        return out
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
