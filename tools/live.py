# -*- coding: utf-8 -*-
"""
连接正在运行的桌面程序（CDP 127.0.0.1:9333）做自检：截图 / 求值 / 切页 / 点按。

用法：
    python tools/live.py pages
    python tools/live.py shot main out.png [--w 1408] [--h 916] [--full]
    python tools/live.py eval main "document.documentElement.dataset.theme"
    python tools/live.py view main analytics
    python tools/live.py click main "#themeBtn"
    python tools/live.py theme main dark
"""
import base64
import json
import os
import sys
import time
import urllib.request

import websocket

PORT = 9333
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机调试端口不走代理


def targets():
    with _OPENER.open("http://127.0.0.1:%d/json/list" % PORT, timeout=3) as r:
        return json.load(r)


def pick(name):
    data = targets()
    for t in data:
        if t.get("type") != "page":
            continue
        url = t.get("url", "")
        if url.endswith("#" + name) or ("#" + name) in url:
            return t
    raise RuntimeError("找不到页面 %s，现有：%s" % (name, [t.get("url", "")[-24:] for t in data]))


class Client:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=30)
        self.i = 0

    def cmd(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            return {"__error": str(r["exceptionDetails"])[:400]}
        return r.get("result", {}).get("value")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    op = sys.argv[1]

    if op == "pages":
        for t in targets():
            print(t.get("type"), "|", t.get("title"), "|", t.get("url", "")[-40:])
        return

    page = sys.argv[2]
    c = Client(pick(page)["webSocketDebuggerUrl"])
    c.cmd("Runtime.enable")
    c.cmd("Page.enable")

    if op == "shot":
        out = os.path.abspath(sys.argv[3])
        w = h = None
        if "--w" in sys.argv:
            w = int(sys.argv[sys.argv.index("--w") + 1])
        if "--h" in sys.argv:
            h = int(sys.argv[sys.argv.index("--h") + 1])
        if w and h:
            c.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                  deviceScaleFactor=1, mobile=False)
            time.sleep(0.5)
        params = {"format": "png"}
        if "--full" in sys.argv:
            m = c.cmd("Page.getLayoutMetrics")
            cw = m["cssContentSize"]
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": cw["width"],
                             "height": cw["height"], "scale": 1}
        data = c.cmd("Page.captureScreenshot", **params)["data"]
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        print("saved", out)
        if w and h:
            c.cmd("Emulation.clearDeviceMetricsOverride")
    elif op == "eval":
        print(json.dumps(c.eval(sys.argv[3]), ensure_ascii=False, indent=1))
    elif op == "view":
        c.eval("window.App && App.go('%s')" % sys.argv[3])
        time.sleep(0.8)
        print(c.eval("document.querySelector('.view.is-active').dataset.view"))
    elif op == "click":
        print(c.eval("""(() => { const n = document.querySelector(%s);
            if (!n) return 'no-node'; n.click(); return 'clicked'; })()""" % json.dumps(sys.argv[3])))
    elif op == "theme":
        want = sys.argv[3]
        print(c.eval("""(() => {
            const before = document.documentElement.dataset.theme;
            const cards = document.querySelectorAll('#themeCards .theme-card');
            let hit = null;
            cards.forEach(c => { if (c.dataset.themeChoice === '%s') hit = c; });
            if (hit) hit.click(); else document.querySelector('#themeBtn').click();
            return { before, pref: (JSON.parse(localStorage.getItem('pi.prefs.v1')||'{}').theme) };
        })()""" % want))
        time.sleep(0.45)
        print(c.eval("""(() => ({
            theme: document.documentElement.dataset.theme,
            btnIcon: (document.querySelector('#themeBtn use')||{}).getAttribute && document.querySelector('#themeBtn use').getAttribute('href'),
            activeCard: (document.querySelector('#themeCards .theme-card.is-active')||{}).dataset
        }))()"""))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
