"""POST /api/ota/reboot to the board's :8080 OTA server.

协议 v2 开始重启也要口令：默认送 X-Key（配网页口令），
也可以用 --psk 走 HMAC（规范化串 reboot|<nonce>）。
"""
import argparse
import hashlib
import hmac
import socket

DEV_PSK = "s3ota-psk-dev-20261115"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", nargs="?", default="10.168.1.226")
    ap.add_argument("port", nargs="?", type=int, default=8080)
    ap.add_argument("--key", default="<REDACTED-PSK>", help="配网页口令（X-Key 路径）")
    ap.add_argument("--psk", default="", help="给了就走 HMAC（X-Auth 路径）")
    ap.add_argument("--nonce", type=int, default=0, help="不填用当前 epoch 秒")
    args = ap.parse_args()

    nonce = args.nonce or int(__import__("time").time())

    if args.psk:
        mac = hmac.new(args.psk.encode(), f"reboot|{nonce}".encode(), hashlib.sha256).hexdigest()
        auth = f"X-Auth: {mac}\r\n"
    else:
        auth = f"X-Key: {args.key}\r\n"

    s = socket.create_connection((args.ip, args.port), timeout=10)
    s.sendall(
        (
            "POST /api/ota/reboot HTTP/1.1\r\n"
            f"Host: {args.ip}:{args.port}\r\n"
            f"X-Nonce: {nonce}\r\n"
            f"{auth}"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
    )
    print("reboot response:", s.recv(256).decode("utf-8", "replace").split("\r\n")[0])
    s.close()


if __name__ == "__main__":
    main()
