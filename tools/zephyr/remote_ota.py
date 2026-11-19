"""纯 HTTP 远程 OTA：只用局域网，不碰串口、不接 USB。

用法：
  python tools/remote_ota.py --ip 10.115.126.239 --key <REDACTED-PSK> \
      --bin build/esp32s3/s3ota/fw_v130.signed.bin --version 130 [--no-reboot]

流程与设备端门禁一一对应：
  1. GET  /api/status        取基线（local_ver / 槽位 / uptime）
  2. POST :8080/ota          带 X-Version / X-Nonce / X-Force / X-Sha256 / X-Key
  3. 轮询 /api/status 的 ota.stage / pct / err
  4. POST :8080/api/ota/reboot 切槽（客户端超时是预期）
  5. 轮询 /api/status 到 local_ver == 目标版本
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

import requests

UA = {"User-Agent": "portal-remote-ota/1.0"}


def status(ip, key, timeout=8):
    url = "http://%s/api/status?k=%s" % (ip, key)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--key", default="<REDACTED-PSK>")
    ap.add_argument("--bin", required=True)
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    path = os.path.abspath(args.bin)
    if not os.path.isfile(path):
        print("FAIL: 找不到 %s" % path)
        return 2
    blob = open(path, "rb").read()
    sha = hashlib.sha256(blob).hexdigest()

    base = status(args.ip, args.key)
    print("baseline: local_ver=%s stage=%s slot0=%s" %
          (base["ota"]["local_ver"], base["ota"]["stage"], base["ota"].get("ver")))
    print("image   : %s %d B sha=%s" % (os.path.basename(path), len(blob), sha[:16] + "..."))

    nonce = int(time.time())
    headers = {"Content-Type": "application/octet-stream",
               "X-Version": str(args.version),
               "X-Nonce": str(nonce),
               "X-Force": "1" if args.force else "0",
               "X-Sha256": sha,
               "X-Key": args.key}
    url = "http://%s:8080/ota" % args.ip

    result = {}

    def upload():
        try:
            r = requests.post(url, data=blob, headers=headers, timeout=180)
            result["status"] = r.status_code
            result["body"] = r.text
        except Exception as exc:
            result["status"] = -1
            result["body"] = "%s: %s" % (type(exc).__name__, exc)

    print("upload: POST :8080/ota  nonce=%d ver=%d" % (nonce, args.version))
    t = threading.Thread(target=upload, daemon=True)
    t.start()

    # 上传期间进度靠 /api/status 的 ota.pct 读，不依赖服务端流式响应
    last = -1
    while t.is_alive():
        try:
            st = status(args.ip, args.key, timeout=3)["ota"]
            if st.get("pct") != last:
                last = st.get("pct")
                print("   %s %s%% (%s/%s B)" % (st.get("stage"), last,
                                               st.get("written"), st.get("total")))
        except Exception:
            pass
        time.sleep(1.5)

    t.join(timeout=180)
    print("reply  : HTTP %s %s" % (result.get("status"), (result.get("body") or "")[:200]))
    if result.get("status") != 200:
        print("RESULT: FAIL (未写入，槽位内容不变)")
        return 1

    st = status(args.ip, args.key)["ota"]
    print("staged : stage=%s pct=%s cand=%s" % (st["stage"], st["pct"], st.get("cand")))

    if args.no_reboot:
        print("RESULT: PASS（已写入并校验，未切槽）")
        return 0

    # 切槽：服务端会立刻复位，客户端拿到超时是预期行为
    try:
        r = requests.post("http://%s:8080/api/ota/reboot" % args.ip,
                          headers={"X-Nonce": str(int(time.time())), "X-Key": args.key},
                          timeout=8)
        print("reboot : HTTP %s" % r.status_code)
    except Exception as exc:
        print("reboot : %s（预期：连接随复位断开）" % type(exc).__name__)

    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(3)
        try:
            st = status(args.ip, args.key, timeout=4)
        except Exception:
            continue
        print("after  : local_ver=%s state=%s ssid=%s boot=%s" %
              (st["ota"]["local_ver"], st["state"], st["ssid"], st["boot"]["n"]))
        if st["ota"]["local_ver"] == args.version:
            print("RESULT: PASS 远程升级成功，无需串口/USB")
            return 0
    print("RESULT: FAIL 未在 90 s 内看到目标版本")
    return 1


if __name__ == "__main__":
    sys.exit(main())
