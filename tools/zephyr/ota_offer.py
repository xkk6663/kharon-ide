#!/usr/bin/env python3
"""公网 OTA 发布端：把签名镜像送上 VPS 并给设备排 OFFER。

与设备端 ota_cloud.c 的协议逐字对齐（HMAC 规范化串两端必须一致）：
    HMAC-SHA256(PSK, "ver|size|sha256|nonce|force")

流程：
  1. scp 签名镜像到 VPS /opt/hub/fw/fw-<ver>.signed.bin（走本机 SSH key）
  2. GET /v1/nonce/next?dev=<dev>        —— VPS 单调计数器，防重放的 nonce 源
  3. POST /v1/ota/offer  X-Admin-Token   —— OFFER 排队（VPS 只搬运不裁决）
  4. （可选）GET /v1/ota/status?dev=<dev> —— 设备收到后会把结果 report 上来

安全要点：
  · PSK 不出本机、不进 VPS；OFFER 里只有 HMAC 结果
  · admin_token 在 VPS /opt/hub/data/admin_token（600），不落在仓库里
    （读环境变量 S3_HUB_ADMIN 或 --admin-file）
  · 版本门禁在设备端独立裁决：OFFER 的 ver 不大于设备版本照样被拒

用法（在仓库根目录）：
  python tools/zephyr/ota_offer.py <build>/
      其中 <build>/ 是 sysbuild 的 app 域，里面有 zephyr/zephyr.signed.bin
  常用：
      python tools/zephyr/ota_offer.py build/esp32s3/s3_wifi_portal/default/s3_wifi_portal
  可选：
      --ver 152            镜像版本（默认从 tools/img_hdr.py 读，读不到必填）
      --dev s3-0001        设备 ID
      --force              放行"版本不升"（设备端仍会独立判，公网不给口令弱档）
      --reboot             OFFER 里要求设备装好后自动重启（默认等本地触发）
"""
from __future__ import annotations

import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
import ssl
import sys
import time
import urllib.request
from pathlib import Path

# 编译期开发默认 PSK（对应 ota.c 的 OTA_PSK_DEV_DEFAULT，与局域网 ota_push.py 同一把）。
# 只做"开发板开箱能用"，**发布端不默认使用它**：必须用 --psk 或环境变量 S3_OTA_PSK
# 显式给出密钥，否则直接失败 —— 静默默认等于把量产密钥降级成公开值。
DEV_PSK = "s3ota-psk-dev-20261115"

HUB_HOST = "120.76.230.17"
HUB_PORT = 8443
VPS_FW_DIR = "/opt/hub/fw"
VPS_SSH_KEY = os.environ.get("S3_HUB_SSH_KEY", r"C:/Users/xiao1/Downloads/_xkk_3.pem")
VPS_USER = "root"


def tls_ctx() -> ssl.SSLContext:
    """VPS 是自签 CA（scau-hub-ca）。发布端与设备端一样做证书固定：
    CA 从 VPS 拉过一次后缓存在 tools/zephyr/hub_ca.pem（可 --ca 覆盖）。
    缺 CA 就**直接失败**（fail closed）—— 以前这里只是 warn 后关校验，
    等于把"证书固定"这条安全结论悄悄降级成"不校验"。"""
    ca = Path(__file__).parent / "hub_ca.pem"
    if not ca.is_file():
        raise SystemExit(
            f"缺 CA 固定文件 {ca}；从 VPS 取回 hub_ca.pem 后再发布"
            "（不接受跳过校验：公网通道的防中间人全靠它）")
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.check_hostname = True
    return ctx


def hub_req(method: str, path: str, hdr_name: str, token: str,
            body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"https://{HUB_HOST}:{HUB_PORT}{path}", method=method, data=body)
    req.add_header("Host", f"{HUB_HOST}:{HUB_PORT}")
    if token:
        req.add_header(hdr_name, token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=tls_ctx(), timeout=30) as r:
        return r.status, r.read()


def read_image_version(img: Path) -> int:
    """直接解析 mcuboot 镜像头的版本字段（major.minor.revision → 取 major.minor）。
    布局：off20 = major u8, off21 = minor u8, off22 = revision u16。
    "1.152.0+0" → 1 | 152<<8。设备端 OTA_LOCAL_VERSION 是纯整数（152），对齐它。"""
    b = img.read_bytes()
    if len(b) < 28 or int.from_bytes(b[0:4], "little") != 0x96f3b83d:  # MCUBOOT magic
        return 0
    major, minor = b[20], b[21]
    if major != 1:
        return 0
    return minor


def scp_up(img: Path, remote: str) -> None:
    cmd = ["scp", "-i", VPS_SSH_KEY, "-o", "StrictHostKeyChecking=accept-new",
           str(img), f"{VPS_USER}@{HUB_HOST}:{remote}"]
    print("scp:", " ".join(cmd[:6]), "...")
    subprocess.run(cmd, check=True, timeout=600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("img_dir", help="sysbuild app 域目录（内含 zephyr/zephyr.signed.bin）")
    ap.add_argument("--ver", type=int, default=0, help="镜像版本（默认从镜像头读）")
    ap.add_argument("--dev", default=os.environ.get("S3_HUB_DEV", "s3-0001"))
    ap.add_argument("--proto", type=int, default=3,
                    help="OFFER 协议版本（默认 3）。老固件（proto 2）必须显式传 2，"
                         "否则设备端 proto 校验直接拒——用于 2→3 的过渡升级。")
    ap.add_argument("--psk", default=os.environ.get("S3_OTA_PSK", ""),
                    help="HMAC 密钥（必填：--psk 或环境变量 S3_OTA_PSK；无默认值）")
    ap.add_argument("--admin", default=os.environ.get("S3_HUB_ADMIN", ""),
                    help="VPS admin token；默认环境变量 S3_HUB_ADMIN 或 --admin-file")
    ap.add_argument("--admin-file", default="/dev/null", help="admin token 文件路径")
    ap.add_argument("--force", action="store_true", help="放行版本不升（设备端独立裁决）")
    ap.add_argument("--reboot", action="store_true",
                    help="OFFER 要求设备装好后自动重启（默认等本地）")
    ap.add_argument("--ssh-key", default=VPS_SSH_KEY)
    ap.add_argument("--wait-report", type=int, default=0,
                    help="排完 OFFER 后等设备 report 的秒数（0=不等）")
    a = ap.parse_args()

    img = Path(a.img_dir) / "zephyr" / "zephyr.signed.bin"
    if not img.is_file():
        print(f"镜像不存在：{img}", file=sys.stderr)
        return 1

    ver = a.ver or read_image_version(img)
    if not ver:
        print("读不出镜像版本，用 --ver 指定", file=sys.stderr)
        return 1

    data = img.read_bytes()
    size = len(data)
    sha = hashlib.sha256(data).hexdigest()
    print(f"image  : {img}  ({size} B)")
    print(f"sha256 : {sha}")

    if not a.psk:
        print("缺 PSK（--psk 或环境变量 S3_OTA_PSK）；发布端刻意不默认使用开发板密钥。"
              f"开发板验证请显式传 --psk {DEV_PSK}", file=sys.stderr)
        return 1

    # ---- admin token
    admin = a.admin
    if not admin and a.admin_file != "/dev/null":
        admin = Path(a.admin_file).read_text(encoding="utf-8").strip()
    if not admin:
        print("缺 admin token（--admin 或环境变量 S3_HUB_ADMIN）", file=sys.stderr)
        return 1

    # ---- 1. scp 固件上 VPS
    remote = f"{VPS_FW_DIR}/fw-{ver}.signed.bin"
    scp_up(img, remote)

    # ---- 2. nonce：VPS 单调计数器（cloudhub.py 只把 /v1/nonce/next 挂在 do_POST）
    st, body = hub_req("POST", f"/v1/nonce/next?dev={a.dev}", "X-Admin-Token", admin)
    if st != 200:
        print(f"nonce 失败 HTTP {st}: {body[:120]!r}", file=sys.stderr)
        return 1
    nonce = json.loads(body)["nonce"]
    print(f"nonce  : {nonce}  (VPS 单调计数器)")

    # ---- 3. OFFER：HMAC 规范化串与设备端逐字一致 ver|size|sha256|nonce|force
    force = 1 if a.force else 0
    canonical = f"{ver}|{size}|{sha}|{nonce}|{force}"
    mac = hmac_mod.new(a.psk.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    offer = {
        "proto": a.proto,   # 默认 3；给 proto 2 的老固件发 160 时必须 --proto 2
        "dev": a.dev,
        "ver": ver,
        "file": Path(remote).name,
        "size": size,
        "sha256": sha,
        "nonce": nonce,
        "force": force,
        "auth": mac,
        "reboot": 1 if a.reboot else 0,
    }
    st, body = hub_req("POST", "/v1/ota/offer", "X-Admin-Token", admin,
                       json.dumps(offer).encode())
    if st != 200:
        print(f"offer 失败 HTTP {st}: {body[:200]!r}", file=sys.stderr)
        return 1
    print(f"offer  : queued for {a.dev}  ver={ver} size={size}")

    # ---- 4. 等设备 report
    if a.wait_report:
        print(f"等待设备 report（最多 {a.wait_report}s）...")
        deadline = time.time() + a.wait_report
        last = None
        while time.time() < deadline:
            time.sleep(5)
            # /v1/ota/status 在 cloudhub.py 里只认 X-Admin-Token（设备 token 必 401），
            # 所以这里不做"退而求其次"的 dev-token 分支。
            st, body = hub_req("GET", f"/v1/ota/status?dev={a.dev}",
                                "X-Admin-Token", admin)
            if st == 200:
                rep = json.loads(body)["report"]
                cur = rep.get("result"), rep.get("ver")
                if cur != last:
                    print(f"  report: {rep}")
                    last = cur
                if rep.get("result") in ("ok", "fail"):
                    return 0 if rep.get("result") == "ok" else 2
        print("report 等待超时（设备可能还没醒来 check）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
