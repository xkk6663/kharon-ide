import glob
import hashlib
import io
import os
import subprocess

JS = r"C:\Users\xiao1\Desktop\esp32\zephyr-learn\tools\_otaauth_js.txt"
# 取该目录里最新的一个签名镜像当"真实固件"样本；旧版写死 fw_v107，那个文件早就被清掉了
BIN_DIR = r"C:\Users\xiao1\Desktop\esp32\zephyr-learn\build\esp32s3\s3ota"
TEST = r"C:\Users\xiao1\Desktop\esp32\zephyr-learn\tools\_sha_test.js"

cands = sorted(glob.glob(os.path.join(BIN_DIR, "fw_v*.signed.bin")),
               key=lambda p: os.path.getmtime(p))
if not cands:
    print("找不到 fw_v*.signed.bin（先跑一次 tools\\build_ota.ps1），本次跳过 firmware 样本")
BIN = cands[-1] if cands else None

fn = io.open(JS, encoding="utf-8").read().split("$('upbtn')")[0]

cases = {
    "empty": b"",
    "abc": b"abc",
    "firmware": io.open(BIN, "rb").read() if BIN else b"\x00",
}

js = [fn, "const fs=require('fs');"]
for name, data in cases.items():
    path = TEST + "." + name + ".bin"
    io.open(path, "wb").write(data)
    js.append(
        "console.log('%s', sha256Hex(new Uint8Array(fs.readFileSync(%r))));"
        % (name, path)
)
io.open(TEST, "w", encoding="utf-8", newline="\n").write("\n".join(js))

out = subprocess.run(["node", TEST], capture_output=True, text=True)
if out.returncode != 0:
    print("NODE FAILED:\n", out.stderr[:400])
    raise SystemExit(1)

got = dict(line.split(" ", 1) for line in out.stdout.strip().splitlines())
ok = True
for name, data in cases.items():
    want = hashlib.sha256(data).hexdigest()
    same = got.get(name) == want
    ok = ok and same
    print(f"{name:9s} node={got.get(name)[:16]}… py={want[:16]}… {'OK' if same else 'MISMATCH'}")
print("RESULT:", "ALL MATCH" if ok else "HAS MISMATCH")
