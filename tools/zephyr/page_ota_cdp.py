"""浏览器页面 OTA 的 CDP 版验收（不是仿真：真 Chrome + DevTools Protocol）。

相比用 GUI 点：文件选择、对话框、进度读取、截图全部走协议，可重复、可断言、无坐标。

用法：
  python tools/page_ota_cdp.py --ip 10.115.126.239 --key <REDACTED-PSK> \
      --bin build/esp32s3/s3ota/fw_v129.signed.bin --version 129 \
      --shot docs/教程/17-Zephyr无线能力/img-ota-cdp-129.png
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket  # websocket-client

CHROME = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9333


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, max_size=64 * 1024 * 1024)
        self.n = 0

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        return self.n

    def wait(self, msg_id, on_event=None, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (msg["error"], msg["error"].get("message")))
                return msg.get("result", {})
            if on_event and "method" in msg:
                on_event(msg)
        raise TimeoutError("CDP #%d timeout" % msg_id)

    def call(self, method, params=None, on_event=None, timeout=90):
        return self.wait(self.send(method, params), on_event=on_event, timeout=timeout)

    def eval(self, expr, on_event=None, timeout=90):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "awaitPromise": True, "returnByValue": True},
                      on_event=on_event, timeout=timeout)
        return r.get("result", {}).get("value")


def ws_target():
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % DEBUG_PORT, timeout=2) as r:
                tabs = json.load(r)
            pages = [t for t in tabs if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("chrome 没有起来（端口 %d）" % DEBUG_PORT)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--key", default="<REDACTED-PSK>")
    ap.add_argument("--bin", required=True)
    ap.add_argument("--version", required=True, type=int)
    ap.add_argument("--shot", required=True)
    ap.add_argument("--no-reboot", action="store_true", help="只上传，不点重启")
    args = ap.parse_args(argv)

    binary = os.path.abspath(args.bin)
    if not os.path.isfile(binary):
        print("FAIL: 找不到 %s" % binary)
        return 2
    url = "http://%s/?k=%s" % (args.ip, args.key)

    profile = tempfile.mkdtemp(prefix="portal-cdp-")
    # 关键：Chrome 的 Private Network Access 会把"http 页面 → 私网地址"的
    # POST 拦成 TypeError: Failed to fetch（浏览器层就掐了，请求根本没出门）。
    # 板子 :8080 已经回了 Access-Control-Allow-*，所以这里只需要关掉 PNA 预检。
    proc = subprocess.Popen([CHROME, "--remote-debugging-port=%d" % DEBUG_PORT,
                             "--user-data-dir=" + profile, "--no-first-run",
                             "--disable-features=LocalNetworkAccessChecks,"
                             "PrivateNetworkAccessSendPreflights,"
                             "PrivateNetworkAccessRespectPreflightResults",
                             "--no-default-browser-check", "--disable-extensions",
                             "--window-size=1600,1000", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        cdp = CDP(ws_target())
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.navigate", {"url": url})

        for _ in range(40):
            if cdp.eval("document.readyState") == "complete":
                break
            time.sleep(0.5)
        print("page:", cdp.eval("document.title"))
        scope = cdp.eval("document.querySelector('.card') ? 'dev-page' : 'unknown'")
        print("scope hint:", scope)

        # 文件选择：协议层直接把路径塞给 <input type=file>，不弹原生对话框
        obj = cdp.call("Runtime.evaluate",
                       {"expression": "document.getElementById('fup')", "returnByValue": False})
        cdp.call("DOM.setFileInputFiles", {"files": [binary],
                                           "objectId": obj["result"]["objectId"]})
        print("file set:", cdp.eval("document.getElementById('fup').files[0].name"),
              cdp.eval("document.getElementById('fup').files[0].size"), "B")

        dialogs = []

        def on_event(msg):
            if msg.get("method") == "Page.javascriptDialogOpening":
                kind = msg["params"]["type"]
                dialogs.append((kind, msg["params"].get("message")))
                text = None
                if kind == "prompt":
                    text = str(args.version)
                cdp.send("Page.handleJavaScriptDialog",
                         {"accept": True, **({"promptText": text} if text else {})})
                print("dialog:", kind, msg["params"].get("message"), "->", "accept", text or "")

        cdp.eval("document.getElementById('upbtn').click()", on_event=on_event, timeout=20)

        deadline = time.time() + 180
        last = ""
        while time.time() < deadline:
            fb = cdp.eval("document.getElementById('otafb').textContent || ''", on_event=on_event)
            prog = cdp.eval("document.getElementById('otaprog').textContent || ''")
            line = "%s | %s" % (fb, prog)
            if line != last:
                print("  ", line)
                last = line
            if "已写入" in fb or "失败" in fb or "被拒绝" in fb:
                break
            time.sleep(2)

        # 截图前把 OTA 卡片滚进视口（整页截图里表单才是主角）
        cdp.eval("document.getElementById('fup').scrollIntoView({block: 'center'})")
        time.sleep(0.6)
        shot = cdp.call("Page.captureScreenshot", {"format": "png"})
        import base64
        with open(args.shot, "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
        print("screenshot:", args.shot, os.path.getsize(args.shot), "B")

        ok = "已写入" in (last or "")
        print("RESULT:", "PASS" if ok else "FAIL")
        if ok and not args.no_reboot:
            time.sleep(1)
            cdp.eval("document.getElementById('upbtn').click()", on_event=on_event, timeout=5)
            for _ in range(20):
                if any(k == "confirm" for k, _ in dialogs):
                    break
                time.sleep(0.5)
            print("dialogs seen:", dialogs)
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(1)
        try:
            proc.kill()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
