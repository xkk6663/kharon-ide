"""Minimal telnet test: connect, optionally send, then hold and drain.

Used to separate three cases:
  * accept alone kills the board
  * only sending data kills it
  * neither does, and the crash needs the full shell prompt path
"""
import os
import socket
import sys
import time

host = sys.argv[1] if len(sys.argv) > 1 else "10.168.1.226"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 23
send = os.environ.get("S3_TELNET_CMD") or (sys.argv[3] if len(sys.argv) > 3 else "-")
if send == "-":
    send = ""  # PowerShell drops empty arguments, so "-" means "send nothing"
hold = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0

s = socket.create_connection((host, port), timeout=5)
print("connected")
s.settimeout(0.5)

time.sleep(0.5)
try:
    print("early:", repr(s.recv(4096)[:120]))
except socket.timeout:
    print("early: <no data in 0.5s>")

if send:
    s.sendall(send.encode())
    print("sent:", repr(send))

deadline = time.time() + hold
got = 0
buf = bytearray()
while time.time() < deadline:
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        continue
    except OSError as exc:
        print("recv error:", exc)
        break
    if not chunk:
        print("peer closed")
        break
    got += len(chunk)
    buf.extend(chunk)
print("received_bytes=", got)
# ANSI 转义会搅乱日志，这里剥掉再看内容
import re
text = buf.decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
print("--- payload ---")
print(text[-2000:])
s.close()
print("done")
