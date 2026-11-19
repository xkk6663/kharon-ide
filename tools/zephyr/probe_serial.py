"""被动抓串口：不碰 DTR/RTS，适合 USB-Serial-JTAG（原生 USB）这种一复位就重新枚举的口。

用法：
    python probe_serial.py COM11 8           # 抓 8 秒
    python probe_serial.py COM11 8 --reset   # 先用 RTS 脉冲复位，再抓 8 秒
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM11"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
do_reset = "--reset" in sys.argv[3:]

s = serial.Serial(port, 115200, timeout=1)
s.dtr = False
s.rts = False
time.sleep(0.2)

if do_reset:
    s.rts = True
    time.sleep(0.15)
    s.rts = False
    time.sleep(0.1)

buf = []
t0 = time.time()
while time.time() - t0 < secs:
    d = s.read(512)
    if d:
        buf.append(d)
s.close()

data = b"".join(buf).decode("utf-8", "replace")
print(data)
print("---- %d bytes, %d lines ----" % (len(data), data.count("\n")))
