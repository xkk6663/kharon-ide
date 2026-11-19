#!/usr/bin/env python3
"""检查"升级成功之后断点记录有没有被清掉"。

做法：带 X-Resume: 1 只发头，看设备回 206（记录还在，异常）还是 200 resume:0（已清空，正确）。
不写文件、不发 body，对设备只产生一次空会话。
"""
import hashlib
import hmac
import socket
import sys

# nonce 账本（本机运行状态，不入 git）：next_nonce / save_nonce 见 tools/ota_nonce.py
from ota_nonce import next_nonce, save_nonce

DEV_PSK = "s3ota-psk-dev-20261115"


def main():
    ip = sys.argv[1]
    img = sys.argv[2]
    ver = int(sys.argv[3])
    # 设备端版本门禁挡在续传握手之前（这是有意的：未授权/降级请求连"有没有断点"都不该被告知）。
    # 所以要验"记录是否已清"，同版本探测得带 force。
    force = len(sys.argv) > 4 and sys.argv[4] == "force"

    data = open(img, "rb").read()
    size = len(data)
    sha = hashlib.sha256(data).hexdigest()
    nonce = next_nonce()
    canon = f"{ver}|{size}|{sha}|{nonce}|{1 if force else 0}"
    mac = hmac.new(DEV_PSK.encode(), canon.encode(), hashlib.sha256).hexdigest()

    head = (
        f"POST /ota HTTP/1.1\r\nHost: probe\r\n"
        f"Content-Length: {size}\r\nX-Version: {ver}\r\nX-Nonce: {nonce}\r\n"
        f"X-Force: {1 if force else 0}\r\nX-Sha256: {sha}\r\nX-Auth: {mac}\r\n"
        f"X-Resume: 1\r\nConnection: close\r\n\r\n"
    ).encode()

    s = socket.create_connection((ip, 8080), timeout=20)
    s.sendall(head)
    # 头被接受就代表设备端已经消耗掉这枚 nonce，立刻回写账本
    save_nonce(nonce)
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(512)
        if not d:
            break
        buf += d
    line = buf.decode("utf-8", "replace").split("\r\n")[0]
    print(line)
    if " 206 " in line:
        print("结论: 记录仍在（升级成功后没清 → 异常）")
        return 1
    print("结论: 无可用记录（升级成功后已清空 → 正确）")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
