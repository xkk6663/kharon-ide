# Single real compile edge: cold vs warm ccache (ASCII only)
import json, os, re, subprocess, time, sys, glob

ROOT = r"C:\Users\xiao1\Desktop\esp32\zephyr-learn"
CCACHE = os.path.join(ROOT, r".tools\ccache\ccache-4.14.3-windows-x86_64\ccache.exe")
SDKBIN = r"C:\Users\xiao1\zephyr-sdk-1.0.1\gnu\xtensa-espressif_esp32s3_zephyr-elf\bin"
OUTDIR = os.path.join(ROOT, "build", "_scratch", "ccbench")
os.makedirs(OUTDIR, exist_ok=True)

# 构建目录已统一收到 build\ 下（2026-11-14 布局），这里按名字从新位置找
CANDIDATES = sorted(glob.glob(os.path.join(ROOT, "build", "**", "compile_commands.json"), recursive=True))
if not CANDIDATES:
    sys.exit("没找到任何 compile_commands.json —— 先随便编一个 ESP32 工程，例如 build.ps1 -App applications\\esp32s3\\s3_wifi_portal -BuildDir ..\\build\\esp32s3\\s3_wifi_portal\\default")
CCJSON = CANDIDATES[0]
print(f"compile_commands: {os.path.relpath(CCJSON, ROOT)}")

env = dict(os.environ)
env["PATH"] = os.path.join(ROOT, r".tools\ccache\ccache-4.14.3-windows-x86_64") + ";" + SDKBIN + ";" + env["PATH"]
env["CCACHE_DIR"] = os.path.join(ROOT, ".ccache")

db = json.load(open(CCJSON, encoding="utf-8"))
entry = next(e for e in db if "esp_clk_tree_common" in e["file"])
cmd = entry["command"]
workdir = entry["directory"]

# 把输出对象重定向到一个临时目录，避免覆盖构建产物
cmd = re.sub(r"-o\s+[^\s]+", lambda m: "-o " + os.path.join(OUTDIR, "out.obj"), cmd)
cmd = re.sub(r"-MF\s+[^\s]+", lambda m: "-MF " + os.path.join(OUTDIR, "out.d"), cmd)

# 保证不相关环境变量残留（ccache 会做 direct mode 校验）
base_cmd = cmd

import shlex

def run(label, use_cache):
    args = shlex.split(base_cmd, posix=False)
    args = [a.strip('"') for a in args]
    if use_cache:
        args = [CCACHE] + args
    t = time.perf_counter()
    r = subprocess.run(args, cwd=workdir, env=env, capture_output=True, text=True)
    dt = (time.perf_counter() - t) * 1000
    print(f"{label}: {dt:.0f} ms exit={r.returncode}")
    if r.returncode != 0:
        print((r.stderr or "")[-400:])
    return dt

# 清空该文件相关缓存，确保第一次是 miss：用 ccache -C 会清全部，这里先跑一次普通编译做基线
t_plain = run("plain gcc (no cache)      ", False)
t_cold = run("ccache cold (miss)        ", True)
t_warm = run("ccache warm (hit)         ", True)
t_warm2 = run("ccache warm again (hit)   ", True)

print("\n--- summary ---")
print(f"plain      {t_plain:.0f} ms")
print(f"cold miss  {t_cold:.0f} ms")
print(f"warm hit   {t_warm:.0f} ms")
print(f"warm hit2  {t_warm2:.0f} ms")
if t_warm > 0:
    print(f"speedup (plain/warm) = {t_plain / t_warm:.1f}x")
r = subprocess.run([CCACHE, "--show-stats"], env=env, capture_output=True, text=True)
print(r.stdout[:1200])
