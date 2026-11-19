"""只对 applications/esp32s3/s3_wifi_portal/src/main.c 做语法检查（-fsyntax-only）。

从 compile_commands.json 里取出该文件完整编译参数，去掉输出/依赖生成参数后
直接调用交叉 gcc。单文件语法检查内存占用很小，可在系统内存紧张时使用。
"""
import json
import shlex
import subprocess
import sys

BUILD = r"C:\Users\xiao1\Desktop\esp32\zephyr-learn\build\esp32s3\s3_wifi_portal\default"
TARGET = "applications/esp32s3/s3_wifi_portal/src/main.c"

with open(BUILD + r"\compile_commands.json", encoding="utf-8") as f:
    entries = json.load(f)

entry = None
for e in entries:
    if e["file"].replace("/", "\\").endswith("applications\\esp32s3\\s3_wifi_portal\\src\\main.c"):
        entry = e
        break

if entry is None:
    print("main.c entry not found in compile_commands.json")
    sys.exit(2)

argv = shlex.split(entry["command"])
# 去掉 ccache 前缀
while argv and ("ccache" in argv[0].lower()):
    argv = argv[1:]

out = []
skip_next = False
for tok in argv:
    if skip_next:
        skip_next = False
        continue
    if tok in ("-o", "-MT", "-MF"):
        skip_next = True
        continue
    if tok in ("-MD", "-MMD"):
        continue
    out.append(tok)

out.append("-fsyntax-only")
print("obj would be:", entry.get("output", "")[:80])
proc = subprocess.run(out, capture_output=True, text=True)
print("exit:", proc.returncode)
if proc.stdout.strip():
    print(proc.stdout)
if proc.stderr.strip():
    print(proc.stderr)
