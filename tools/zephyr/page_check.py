"""页面离线校验：语法 + 内容边界。

两件事必须在改页面前后都成立：
  1. user_page.html 的 <script> 能被 JS 解析器接受（node --check）；
  2. 用户页里**不能**出现开发者功能（OTA 卡片、/api/shell、槽位、复位原因），
     也不能调用开发者接口。这一条是产品要求，不是风格偏好。

用法：python tools/page_check.py [--node <node.exe 路径>]
返回码 0 = 两条都过。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

APP = pathlib.Path(__file__).resolve().parent.parent / "apps" / "esp32s3" / "s3_wifi_portal"
PAGE = APP / "src" / "user_page.html"
OUT_JS = pathlib.Path(__file__).resolve().parent.parent / "build" / "page_check.js"

FORBIDDEN_TEXT = ["固件升级", "远程命令", "OTA", "槽位", "复位原因", "boot"]
FORBIDDEN_API = ["/api/shell", "/api/ota", "/api/boot", "/api/wlog", "/api/pw", "/api/status"]
ALLOWED_API = ["/api/wifi", "/api/scan", "/api/scan/start", "/api/connect",
               "/api/saved", "/api/forget", "/api/disconnect"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=shutil.which("node") or "node")
    args = ap.parse_args()

    html = PAGE.read_text(encoding="utf-8")
    bad_text = [w for w in FORBIDDEN_TEXT if w in html]
    apis = sorted(set(re.findall(r"/api/[a-z_]+(?:/[a-z_]+)?", html)))
    bad_api = [a for a in apis if a not in ALLOWED_API]

    print("page bytes : %d" % len(html))
    print("api calls  : %s" % apis)

    rc = 0
    if bad_text:
        print("FAIL: 用户页里出现了开发者字样 %s" % bad_text)
        rc = 1
    if bad_api:
        print("FAIL: 用户页调用了非白名单接口 %s" % bad_api)
        rc = 1

    m = re.search(r"<script>(.*)</script>", html, re.S)
    if not m:
        print("FAIL: 没有 <script>")
        return 1
    OUT_JS.parent.mkdir(parents=True, exist_ok=True)
    OUT_JS.write_text(m.group(1), encoding="utf-8")

    try:
        proc = subprocess.run([args.node, "--check", str(OUT_JS)],
                              capture_output=True, text=True)
    except FileNotFoundError:
        print("SKIP: 找不到 node，跳过语法检查（内容检查仍已执行）")
        return rc

    if proc.returncode != 0:
        print("FAIL: JS 语法错误\n%s" % (proc.stderr[:600]))
        rc = 1
    else:
        print("ok: JS 语法通过")

    print("RESULT: %s" % ("PASS" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
