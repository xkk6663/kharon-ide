#!/usr/bin/env python3
"""故意把 OTA 上传打断，用来制造断点记录（配合 ota_push.py --resume 验续传）。

做法：正常的头（带 X-Sha256 / X-Auth / X-Nonce）+ 只发前 N 字节 body，然后直接
关连接。设备端 recv 会拿到 0（peer closed），按"传输中断"处理，段边界上的断点
记录留在 NVS 里。

用法：
    python tools/ota_drop.py 10.168.1.226 build/esp32s3/s3ota/fw_v110.signed.bin \
        --version 110 --drop-bytes 400000

注意：--drop-bytes 建议取 >256KB 且不是段边界倍数（256KB=262144），这样能验证
"段内断掉就回退到上一个段边界"的行为；取 400000 期望的续传偏移是 262144。
"""

import argparse
import hashlib
import hmac
import os
import socket
import sys
import time

# nonce 账本（本机运行状态，不入 git）：next_nonce / save_nonce 见 tools/ota_nonce.py
from ota_nonce import next_nonce, save_nonce

DEV_PSK = "s3ota-psk-dev-20261115"
CHUNK = 2048


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("image")
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--nonce", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--drop-bytes", type=int, default=400000, help="发完这么多 body 字节就断开")
    ap.add_argument("--psk", default=os.environ.get("S3_OTA_PSK", DEV_PSK))
    ap.add_argument("--key", default="")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    with open(args.image, "rb") as f:
        data = f.read()
    size = len(data)
    sha = hashlib.sha256(data).hexdigest()
    nonce = args.nonce or next_nonce()
    canonical = f"{args.version}|{size}|{sha}|{nonce}|{1 if args.force else 0}"

    if args.key:
        auth = f"X-Key: {args.key}\r\n"
    else:
        auth = f"X-Auth: {hmac.new(args.psk.encode(), canonical.encode(), hashlib.sha256).hexdigest()}\r\n"

    head = (
        f"POST /ota HTTP/1.1\r\n"
        f"Host: {args.ip}:{args.port}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {size}\r\n"
        f"X-Version: {args.version}\r\n"
        f"X-Nonce: {nonce}\r\n"
        f"X-Force: {1 if args.force else 0}\r\n"
        f"X-Sha256: {sha}\r\n"
        f"{auth}"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()

    drop = min(args.drop_bytes, size - 1)
    print(f"image {os.path.basename(args.image)} size={size} drop_at={drop} "
          f"(段边界 262144，预期续传偏移 {drop // 262144 * 262144})")

    sock = socket.create_connection((args.ip, args.port), timeout=30)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(head)
    # 头一旦被接受，设备端就已经把这枚 nonce 记进 last_used，必须回写账本；
    # 否则下一个脚本（probe / push --resume）会拿到同值，被判重放。
    save_nonce(nonce)

    t0 = time.time()
    sent = 0
    while sent < drop:
        n = min(CHUNK, drop - sent)
        sock.sendall(data[sent : sent + n])
        sent += n
    dt = time.time() - t0
    print(f"sent {sent} B in {dt:.1f} s ({sent/1024/dt:.0f} KB/s)，现在断开（模拟掉线）")

    # 正常 close（FIN）。设备端 recv 返回 0 -> "peer closed mid-transfer"，
    # 段边界的断点记录保留。这里不用 RST，RST 也能触发，但日志会变成 recv 错误，
    # 不如"对端关闭"这条路径好读。
    sock.close()
    print("done. 下一步：python tools/ota_push.py <ip> <same image> --version "
          f"{args.version} --resume")


if __name__ == "__main__":
    sys.exit(main())
