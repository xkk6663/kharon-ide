"""USB-Serial-JTAG 版命令工具：不发 RTS 复位（原生 USB 口一复位就重新枚举），
只在需要时用 --reset 打一次复位脉冲。

用法：
    python usb_cmd.py COM11 "app stats@3" "app rgb solid 20 0 0@3"
    python usb_cmd.py COM11 --reset "app saved@4"
"""
import sys
import time

import serial

args = [a for a in sys.argv[1:]]
port = args.pop(0)
do_reset = False
if args and args[0] == "--reset":
    do_reset = True
    args.pop(0)

items = []
for a in args:
    if "@" in a:
        cmd, secs = a.rsplit("@", 1)
        items.append((cmd, float(secs)))
    else:
        items.append((a, 2.5))

s = serial.Serial()
s.port = port
s.baudrate = 115200
s.timeout = 0.2
# 先设电平再 open，避免 open 瞬间的 DTR/RTS 脉冲把板子复位
s.dtr = False
s.rts = False
s.open()
time.sleep(0.2)

if do_reset:
    s.rts = True
    time.sleep(0.15)
    s.rts = False


def drain(secs):
    out = []
    t0 = time.time()
    while time.time() - t0 < secs:
        d = s.read(512)
        if d:
            out.append(d)
    return b"".join(out).decode("utf-8", "replace")


boot = drain(1.5 if do_reset else 0.5)
print(boot, end="")

for cmd, secs in items:
    print("\n>>> %s" % cmd)
    s.write((cmd + "\r\n").encode())
    print(drain(secs), end="")

s.close()
print()
