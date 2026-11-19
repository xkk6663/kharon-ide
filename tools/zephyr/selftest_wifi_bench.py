"""wifi_bench.py 的离线自测：起一个本地假 HTTP 服务模拟板子的三个端点，
再让 wifi_bench.py 去打它，确认脚本本身能跑、能算数、能打印。

这样在板子还没烧好固件时，也能确定"工具本身没问题"，下一轮只剩真实数据。

用法：
    python tools\\selftest_wifi_bench.py
"""
import http.server
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 18080

STATUS_BODY = b'{"state":"connected","ssid":"fake","ip":"127.0.0.1"}'
PAGE_BODY = b"<html><body>" + (b"0123456789abcdef" * 900) + b"</body></html>"   # ≈14 KB
SCAN_BODY = b'{"scanning":false,"aps":[' + (b'{"ssid":"ap","rssi":-60,"lock":1},' * 60) \
    + b']}'                                                                     # ≈2.8 KB


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                      # noqa: N802
        body = None
        if self.path == "/api/status":
            body = STATUS_BODY
        elif self.path in ("/", "/index.html"):
            body = PAGE_BODY
        elif self.path == "/api/scan":
            body = SCAN_BODY

        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):                     # 静音
        pass


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex((HOST, port)) != 0


def main():
    if not port_free(PORT):
        print(f"端口 {PORT} 被占用，先释放再跑")
        return 1

    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.3)
    print(f"假服务已起: http://{HOST}:{PORT}  "
          f"(status {len(STATUS_BODY)} B / page {len(PAGE_BODY)} B / scan {len(SCAN_BODY)} B)\n")

    here = Path(__file__).resolve().parent
    cmd = [sys.executable, str(here / "wifi_bench.py"), HOST,
           "--port", str(PORT), "--rounds", "5"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
        srv.server_close()

    print(out.stdout)
    if out.stderr.strip():
        print("STDERR:", out.stderr.strip())

    ok = out.returncode == 0 and "kbit/s" in out.stdout and "请求失败" not in out.stdout
    print("自测结果:", "通过" if ok else "失败")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
