# -*- coding: utf-8 -*-
"""
用 CDP 精确截图 / 取运行状态，仅用于开发期自检。

用法:
    python tools/shot.py "index.html?static=1&theme=light" out.png 1440 900 [--full]
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(BASE, ".edge-cdp")
PORT = 9333


def launch():
    if not os.path.exists(PROFILE):
        os.makedirs(PROFILE, exist_ok=True)
    args = [
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--force-device-scale-factor=1", "about:blank",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ws_url():
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1))
            for t in data:
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("CDP 未就绪")


class Client:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=20)
        self.i = 0

    def cmd(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})


def main():
    url_arg = sys.argv[1]
    out = os.path.abspath(sys.argv[2])
    w = int(sys.argv[3]) if len(sys.argv) > 3 else 1440
    h = int(sys.argv[4]) if len(sys.argv) > 4 else 900
    full = "--full" in sys.argv

    proc = launch()
    try:
        c = Client(ws_url())
        c.cmd("Page.enable")
        c.cmd("Runtime.enable")
        c.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
        target = "file:///" + os.path.join(BASE, url_arg).replace("\\", "/")
        c.cmd("Page.navigate", url=target)
        time.sleep(2.2)

        info = c.cmd("Runtime.evaluate", expression="""(() => ({
            w: innerWidth, h: innerHeight, dpr: devicePixelRatio,
            scrollH: document.documentElement.scrollHeight,
            cards: document.querySelectorAll('.card').length,
            err: window.__err || null
        }))()""", returnByValue=True)["result"]["value"]
        print("layout:", info)

        errs = c.cmd("Runtime.evaluate", expression="""(() => {
            const out = [];
            document.querySelectorAll('svg').forEach(s => { if(!s.getAttribute('width')) out.push('svg-no-size'); });
            return out.slice(0,5);
        })()""", returnByValue=True)["result"]["value"]
        print("checks:", errs)

        params = {"format": "png"}
        if full:
            m = c.cmd("Page.getLayoutMetrics")
            ch = m["cssContentSize"]["height"]
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": w, "height": ch, "scale": 1}
        data = c.cmd("Page.captureScreenshot", **params)["data"]
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        print("saved:", out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
