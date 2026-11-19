#!/usr/bin/env python3
"""
把 mcuboot 签名过的固件推到板子的 :8080/ota

用法：
    python tools/ota_push.py 10.168.1.226 build/esp32s3/s3ota/zephyr/zephyr.signed.bin --version 101

注意：
- 必须传 **zephyr.signed.bin**，不是 zephyr.bin。前者带 mcuboot 的 32 字节头
  和尾部 TLV，mcuboot 靠它判断镜像有效；裸 bin 会被判无效镜像。
- --version 要大于板子当前 OTA_LOCAL_VERSION（当前 100）。设备端自己裁决，
  不因为本脚本说"已比对"就放行。
- nonce 由本脚本持久化递增（tools/.ota_nonce），设备端严格拒绝 <= 上次的 nonce。
  --force 只允许越过"版本不升"，越不过 nonce / 体积 / 完整性。
"""

import argparse
import hashlib
import hmac
import json
import os
import socket
import sys
import time

# 编译期开发默认 PSK（对应 ota.c 的 OTA_PSK_DEV_DEFAULT）。
# 换独立密钥时：设备侧写 NVS s3ota/psk + 这里用 --psk 或环境变量 S3_OTA_PSK，
# 两端必须同时改 —— skill 里"不兼容升级双端同步发布"那条。
DEV_PSK = "s3ota-psk-dev-20261115"

# nonce 账本（本机运行状态，不入 git）：next_nonce / save_nonce 见 tools/ota_nonce.py
from ota_nonce import next_nonce, save_nonce  # noqa: E402

# 块要小：板端 ota_thread 是边收边写 flash，首次写入前还要按扇区擦整个 slot1
# （1344 KB / 4 KB = 336 次擦除，实机约 10 s）。块太大时 TCP 窗口被塞满，
# 客户端 sendall 会一直等到板子擦完才拿到 ACK —— 这就是 10 s 超时打挂的原因。
CHUNK = 2048
# 单次 send 的最长等待必须盖住板子的擦除窗口，不能按"网络 RTT"来估。
SOCK_TIMEOUT = 90


def recv_response(sock, timeout=10.0):
    """读完整响应：头 + 按 Content-Length 读满 body。

    踩过的坑：之前只读到 "\\r\\n\\r\\n" 就返回，而板子是把头和 body 分两次 send 的，
    于是 body 恒为空 -> 明明设备已经 "whole-sha256 MATCH / upgrade flag set"，
    客户端却因为拿不到 {"ok":true} 而报"device rejected"。
    """
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(512)
        if not d:
            break
        buf += d
    head, _, body = buf.partition(b"\r\n\r\n")

    clen = 0
    for line in head.decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                clen = int(line.split(":", 1)[1].strip())
            except ValueError:
                clen = 0
            break

    while len(body) < clen:
        d = sock.recv(clen - len(body))
        if not d:
            break
        body += d

    return head.decode("utf-8", "replace"), body


def parse_status(head):
    """'HTTP/1.1 206 Partial Content' -> 206"""
    parts = head.split("\r\n", 1)[0].split(" ")
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return 0


def parse_header(head, name):
    for line in head.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", help="板子 STA 侧 IP，例如 10.168.1.226")
    ap.add_argument("image", help="zephyr.signed.bin 的路径")
    ap.add_argument("--version", type=int, required=True, help="本次固件的版本号（整数，需大于设备当前值）")
    ap.add_argument("--nonce", type=int, default=0, help="指定 nonce；不填则用本地持久化值 +1")
    ap.add_argument("--force", action="store_true", help="允许同版本/降级（设备端仍会自己裁决其它条件）")
    ap.add_argument(
        "--psk",
        default=os.environ.get("S3_OTA_PSK", DEV_PSK),
        help="HMAC 密钥；默认取环境变量 S3_OTA_PSK，再退回编译期开发默认值",
    )
    ap.add_argument(
        "--key",
        default="",
        help="改用配网页口令（X-Key，弱一档，页面走的就是这条）；给了它就不发 X-Auth",
    )
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--resume",
        action="store_true",
        help="断点续传：先只发头，等设备回 206 + X-Resume-Offset，再发剩下的字节；没有断点则自动全量",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print(f"image not found: {args.image}", file=sys.stderr)
        return 2

    size = os.path.getsize(args.image)
    if size < 1024:
        print(f"image too small ({size} B) — 大概传错文件了", file=sys.stderr)
        return 2

    with open(args.image, "rb") as f:
        data = f.read()

    sha = hashlib.sha256(data).hexdigest()

    if args.nonce:
        nonce = args.nonce
    else:
        nonce = next_nonce()

    # 规范化串必须跟设备端逐字一致：ver|size|sha256|nonce|force
    # 字段顺序固定、| 分隔、十进制无符号、force 归一成 0/1、不做 trim/大小写归一。
    canonical = f"{args.version}|{size}|{sha}|{nonce}|{1 if args.force else 0}"

    if args.key:
        auth_line = f"X-Key: {args.key}\r\n"
        auth_note = "X-Key(portal pw)"
    else:
        mac = hmac.new(args.psk.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        auth_line = f"X-Auth: {mac}\r\n"
        auth_note = f"HMAC-SHA256(psk={len(args.psk)}B)"

    head = (
        f"POST /ota HTTP/1.1\r\n"
        f"Host: {args.ip}:{args.port}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {size}\r\n"
        f"X-Version: {args.version}\r\n"
        f"X-Nonce: {nonce}\r\n"
        f"X-Force: {1 if args.force else 0}\r\n"
        f"X-Sha256: {sha}\r\n"
        f"{'X-Resume: 1\r\n' if args.resume else ''}"
        f"{auth_line}"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()

    print(f"image   : {os.path.basename(args.image)}")
    print(f"size    : {size} B ({size/1024:.1f} KB)")
    print(f"sha256  : {sha}")
    print(f"version : {args.version}   nonce: {nonce}   force: {bool(args.force)}")
    print(f"auth    : {auth_note}")
    print(f"target  : {args.ip}:{args.port}")

    t0 = time.time()
    sock = socket.create_connection((args.ip, args.port), timeout=SOCK_TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(head)

    # 续传握手：只发头，等设备裁决从哪个偏移开始
    start = 0
    if args.resume:
        hs_head, hs_body = recv_response(sock)
        code = parse_status(hs_head)
        if code == 206:
            start = int(parse_header(hs_head, "X-Resume-Offset") or 0)
            print(f"resume  : 设备接受续传，从 {start} / {size} ({start*100//size}%) 开始")
        elif code == 200:
            print("resume  : 设备无可用断点（或记录已失效），按全量重传")
        else:
            print(f"resume  : 握手被拒 {code}\n{hs_head.strip()}\n{hs_body.decode('utf-8','replace')}")
            sock.close()
            return 1
        t0 = time.time()   # 吞吐只算真正传字节的这段时间

    sent = start
    last_print = 0.0
    for i in range(start, size, CHUNK):
        try:
            sock.sendall(data[i : i + CHUNK])
        except (ConnectionError, OSError) as e:
            # 设备端在读完请求头就拒了（401 badauth / 403 replay / 409 version / 413 too big），
            # 会立刻 close，客户端这边表现为"发送时连接被中止"。
            # 原来这里直接把异常抛出去，只看到 WinError 10053，完全不知道拒绝原因。
            try:
                raw = sock.recv(256).decode("utf-8", "replace")
            except Exception:
                raw = ""
            sock.close()
            print(f"\n发送中断：{e}")
            if raw:
                print("设备端回应:", raw.split("\r\n")[0])
                if "\r\n\r\n" in raw:
                    print("body      :", raw.split("\r\n\r\n", 1)[1].strip())
            print("对照：401 badauth=口令/HMAC 不对；403 replay=nonce 没递增；"
                  "409 version=版本不够且没 --force；413 too big=超槽；400 missing sha=没带 X-Sha256")
            return 1
        sent += min(CHUNK, size - i)
        now = time.time()
        if now - last_print > 1.0:
            pct = sent * 100 // size
            print(f"  sent {sent}/{size} ({pct}%)", end="\r", flush=True)
            last_print = now

    print(f"  sent {size}/{size} (100%)")
    resp_head, resp_body = recv_response(sock)
    sock.close()
    dt = time.time() - t0

    print("--- response ---")
    print(resp_head.strip())
    print(resp_body.decode("utf-8", "replace"))
    print(f"--- client side: {dt:.2f} s, {max(1,size-start)/(1024*dt):.0f} KB/s ---")

    if '"ok":true' in resp_body.decode("utf-8", "replace"):
        save_nonce(nonce)
        print("device accepted: image written + sha256 verified + upgrade flag set")
        print("next: POST /api/ota/reboot (tools/ota_reboot.py) to boot into it")
        return 0

    print("device rejected. 对照拒绝码：", file=sys.stderr)
    print("  400 missing header / short body", file=sys.stderr)
    print("  409 version older  → 加 --force 或把 --version 调大", file=sys.stderr)
    print("  403 replay         → nonce 必须严格递增", file=sys.stderr)
    print("  413 too big        → 超过槽容量", file=sys.stderr)
    print("  500 sha mismatch   → 传输被打断或镜像本身不完整", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
