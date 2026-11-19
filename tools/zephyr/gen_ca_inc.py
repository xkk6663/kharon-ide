#!/usr/bin/env python3
"""把 cloudhub 的根 CA（PEM）转成固件内嵌的 C 头文件（hub_ca.inc）。

用法：
  scp -i <key> root@120.76.230.17:/etc/mosquitto/certs/ca.crt applications/esp32s3/s3_wifi_portal/src/l0/hub_ca.pem
  python tools/zephyr/gen_ca_inc.py

为什么内嵌：设备只信这一把自签根（证书固定），中间人在链路上换任何
证书都过不了 TLS 校验。CA 到期（2036）前要重跑本脚本换发。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "applications/esp32s3/s3_wifi_portal/src/l0"

pem_path = APP / "hub_ca.pem"
pem = pem_path.read_text(encoding="utf-8").strip()

lines = "".join('    "' + line + '\\n"\n' for line in pem.splitlines())
out = f'''/*
 * cloudhub 的根 CA（scau-hub-ca，自签，服务器证书由它签发）。
 * 生成方式：tools/zephyr/gen_ca_inc.py，从 VPS /etc/mosquitto/certs/ca.crt 转出。
 * 内嵌进固件 = 证书固定（certificate pinning）：设备只信这一把根，
 * 中间人换证书必死。CA 有效期见证书本身，过期前要换发并重新刷板。
 */
#ifndef HUB_CA_INC_
#define HUB_CA_INC_
static const char hub_ca_pem[] =
{lines}    "\\0";
static const unsigned int hub_ca_pem_len = sizeof(hub_ca_pem) - 1;
#endif
'''
(APP / "hub_ca.inc").write_text(out, encoding="utf-8", newline="\n")
print("hub_ca.inc generated (%d bytes pem)" % len(pem))
