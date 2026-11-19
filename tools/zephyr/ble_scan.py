"""
用 Windows 自带蓝牙适配器扫描 BLE 广播，验证 ESP32-S3 的 Zephyr BLE 是否真的在对外广播。

用法:
    python tools\\ble_scan.py [SECONDS]

默认扫 8 秒。输出所有设备，并单独高亮名字里带 Zephyr / ESP 的条目。
"""
import asyncio
import sys
import time

from bleak import BleakScanner


async def main(seconds: float) -> None:
    seen = []

    def on_detect(device, adv_data):
        # rssi 是 int；adv_data.local_name 可能为 None
        seen.append(
            (
                device.address,
                device.name or adv_data.local_name or "(no name)",
                adv_data.rssi,
                adv_data.service_uuids,
            )
        )

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    print(f"--- {len(seen)} advertisement(s) in {seconds}s ---")
    hits = []
    for addr, name, rssi, uuids in sorted(seen, key=lambda x: -(x[2] or -200)):
        tag = ""
        if any(k in (name or "") for k in ("Zephyr", "ESP", "Peripheral")):
            tag = "  <== 目标"
            hits.append((addr, name, rssi, uuids))
        print(f"{addr}  rssi={rssi:>4}  {name}{tag}")
        if uuids:
            print(f"         services: {', '.join(uuids)}")

    print("\n=== 结论 ===")
    if hits:
        for addr, name, rssi, uuids in hits:
            print(f"FOUND: {name} / {addr} / rssi {rssi}")
            print(f"UUIDs: {uuids}")
    else:
        print("NOT FOUND: 没有扫到 Zephyr 设备")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    t0 = time.time()
    asyncio.run(main(secs))
    print(f"elapsed {time.time() - t0:.1f}s")
