"""往板子串口发一串 shell 命令，把回显全部抓下来。

命令可以写成 "cmd@秒数"，给需要长时间等待的命令单独加窗口，
例如: serial_cmd.py COM9 "lab i2c" "lab gpio@12"
"""
import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
CMDS = sys.argv[2:] or ["help"]


def split_cmd(text, default=2.5):
    if "@" in text:
        cmd, _, sec = text.rpartition("@")
        try:
            return cmd, float(sec)
        except ValueError:
            pass
    return text, default

ser = serial.Serial(PORT, 115200, timeout=0.5)
ser.setRTS(True)
time.sleep(0.15)
ser.setRTS(False)
time.sleep(0.05)

buf = []


def drain(seconds):
    end = time.time() + seconds
    while time.time() < end:
        line = ser.readline()
        if line:
            buf.append(line.decode("utf-8", "replace").rstrip())


drain(3)                      # 启动日志 + 首屏
for c in CMDS:
    cmd, wait = split_cmd(c)
    ser.write((cmd + "\r").encode())
    drain(wait)

ser.close()
print("---- %s : %d lines ----" % (PORT, len(buf)))
for l in buf:
    print(l)
