"""Wi-Fi 应用层吞吐/延迟基线测量（PC 侧，无第三方依赖）。

用法（板子通过 STA 连到同一 LAN 后）：
    python tools\\wifi_bench.py 10.168.1.226
    python tools\\wifi_bench.py s3portal.local --rounds 50

测什么：
  1. TCP 建连耗时（3 次握手 + HTTP 请求往返）
  2. `/`（整页 HTML，十几 KB）的字节/秒 —— 注意必须是根路径：
     `/index.html` 会被 captive portal 的通配规则 302 掉，测出来的是跳转响应
  3. /api/status（小 JSON，几百字节）的请求频率
  4. /api/scan（大 JSON，扫描列表）的字节/秒

为什么这样测：Zephyr 的 HTTP server 一次响应就把 body 全发完，不需要分块，
所以"整页 HTML 的字节/秒"就是这片 Wi-Fi 上应用层能拿到的下行带宽下限。
真要测链路峰值应该上 zperf（UDP/TCP 灌包），那是 OTA 大文件传输前的专项。
"""
import http.client
import statistics
import sys
import time

PATH_STATIC = "/"
PATH_SMALL = "/api/status"
PATH_LARGE = "/api/scan"


def one_request(host, path, port=80, timeout=5):
    """返回 (字节数, 耗时秒)。一次全新的 TCP 连接。"""
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        body = resp.read()
        dt = time.perf_counter() - t0
        return len(body), dt, resp.status
    finally:
        conn.close()


def bench(host, path, rounds, label, port=80):
    sizes, times, statuses = [], [], []
    for _ in range(rounds):
        try:
            n, dt, st = one_request(host, path, port=port)
        except Exception as exc:                      # noqa: BLE001
            print(f"{label:26s} 请求失败: {exc}")
            return None
        sizes.append(n)
        times.append(dt)
        statuses.append(st)

    if not sizes:
        return None

    total_bytes = sum(sizes)
    total_time = sum(times)
    kbps = (total_bytes * 8 / total_time / 1000) if total_time else 0
    print(f"{label:26s} 轮次={rounds:3d} "
          f"响应={statistics.median(sizes):7.0f} B "
          f"单个={statistics.median(times) * 1000:7.1f} ms "
          f"最快={min(times) * 1000:7.1f} ms "
          f"HTTP={statuses[0]} "
          f"≈{kbps:7.1f} kbit/s")
    return {
        "label": label,
        "rounds": rounds,
        "median_bytes": statistics.median(sizes),
        "median_ms": statistics.median(times) * 1000,
        "kbit_s": kbps,
    }


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "10.168.1.226"
    rounds = 20
    port = 80
    for i, a in enumerate(sys.argv):
        if a == "--rounds" and i + 1 < len(sys.argv):
            rounds = int(sys.argv[i + 1])
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    print(f"target={host}  port={port}  rounds={rounds}\n")

    results = []
    for path, label in (
        (PATH_SMALL, "GET /api/status (小)"),
        (PATH_STATIC, "GET / (页面)"),
        (PATH_LARGE, "GET /api/scan (大 JSON)"),
    ):
        r = bench(host, path, rounds, label, port=port)
        if r:
            results.append(r)

    print()
    # 用页面那一项换算成"传 760 KB 固件要多久"—— OTA 可行性判断
    for r in results:
        if "/ (页面)" in r["label"] and r["kbit_s"] > 0:
            fw_kb = 760
            secs = fw_kb * 8 / r["kbit_s"]
            print(f"按上面带宽估：传 760 KB 固件约需 {secs:.1f} s（仅链路下限估算）")


if __name__ == "__main__":
    main()
