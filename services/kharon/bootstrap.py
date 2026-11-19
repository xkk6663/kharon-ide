"""kharon.bootstrap —— Zephyr 工作区的可复现引导（P1）。

    python -m kharon.bootstrap --check          # 只读体检（离线可跑）
    python -m kharon.bootstrap --offline        # 只用本机已有目录，不联网
    python -m kharon.bootstrap                  # 幂等补齐 venv / workspace
    python -m kharon.bootstrap --json           # 体检结果机读

设计（docs/design/架构优化与目录重构方案_2026-11-19.md §4-P1-6）：

- **允许清单**来自 ``manifests/zephyr.west.yml``：zephyr commit + module 列表。
  不跑 ``west update`` 也能判断"缺哪些 module"。
- 失败只输出**一条可执行修复命令**，不写 .env、不改 host 文件（路径由
  ``--write-local`` 显式写入 ``host.local.yaml`` 的 ``kharon:`` 分节）。
- 幂等：venv / workspace 已存在时只检查，不重建。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "manifests" / "zephyr.west.yml"
VENV = ROOT / ".deps" / "venv"
BOARDS_MODULE = ROOT / "zephyr_modules" / "elab-boards"
WEST = ROOT / "west"


def _venv_python() -> Path:
    if sys.platform.startswith("win"):
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _load_manifest() -> dict:
    from elab import _yaml

    return _yaml.load_file(MANIFEST) or {}


def _host() -> dict:
    os.environ.setdefault("ELAB_HOST", "kharon")
    from elab.config import Config

    return Config(ROOT).host


def _resolve(value) -> str:
    cfg_root = ROOT.as_posix()
    return str(value or "").replace("${ELAB_ROOT}", cfg_root)


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                              text=True, timeout=300)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _fix(py: str) -> str:
    return f"{py} -m kharon.bootstrap"


def collect() -> list[dict]:
    """跑全部检查，返回 ``[{name, ok, detail, fix}]``（不改盘、不联网）。"""
    checks: list[dict] = []
    py = str(_venv_python())

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    # 1. manifest / host profile
    manifest = _load_manifest()
    zephyr = manifest.get("zephyr") or {}
    add("manifest", bool(zephyr.get("commit")),
        f"{MANIFEST.name}: zephyr@{str(zephyr.get('commit'))[:12]}")
    try:
        host = _host()
        tools = host.get("tools") or {}
        z = tools.get("zephyr") or {}
        add("host.profile", True, "ELAB_HOST=kharon（profile + host.local.yaml）")
    except Exception as exc:                            # noqa: BLE001
        add("host.profile", False, str(exc),
            "set ELAB_HOST=kharon  # bash: export ELAB_HOST=kharon")
        return checks

    # 2. venv + west
    venv_ok = _venv_python().is_file()
    add("venv", venv_ok, py if venv_ok else f"缺少 {py}",
        f"{sys.executable} -m venv {VENV} && {py} -m pip install -U west")
    west_ok = False
    if venv_ok:
        rc, out = _run([py, "-c",
                        "import importlib.metadata as m; print(m.version('west'))"])
        west_ok = rc == 0
        add("west", west_ok, out.strip().splitlines()[-1] if out.strip() else "import west 失败",
            f"{py} -m pip install -U west")

    # 3. workspace + zephyr commit
    ws_ok = (WEST / ".west" / "config").is_file()
    add("workspace", ws_ok, str(WEST / ".west" / "config"),
        f"{py} -m west init -m {zephyr.get('remote')} --mr {zephyr.get('commit')} {WEST}")
    if ws_ok:
        zb = WEST / str(zephyr.get("path") or "zephyr")
        head = ""
        if (zb / ".git").exists():
            rc, out = _run(["git", "-C", str(zb), "rev-parse", "HEAD"])
            head = out.strip() if rc == 0 else ""
        want = str(zephyr.get("commit") or "")
        pinned = want not in ("", "HEAD")
        add("zephyr.commit", (not pinned) or head == want,
            f"HEAD={head[:12] or '?'} 期望={want[:12] or '?'}",
            f"git -C {zb} checkout {want}")

    # 4. module allowlist（optional 项由 `west update --narrow` 按 group-filter 跳过）
    projects = manifest.get("projects") or []
    required = [p for p in projects if not p.get("optional")]
    missing = [str(p.get("name")) for p in required
               if not (WEST / str(p.get("path") or "")).exists()]
    skipped = len(projects) - len(required)
    add("modules", not missing,
        f"{len(required) - len(missing)}/{len(required)} 必需就绪"
        + (f"（另 {skipped} 个 optional 不计）" if skipped else "")
        + (f"；缺：{', '.join(missing[:8])}" if missing else ""),
        f"{py} -m west update --narrow -o=--depth=1  # cwd={WEST}")

    # 5. 板级 module（真相）与树内残留副本（必须不存在）
    boards_ok = (BOARDS_MODULE / "boards").is_dir()
    add("boards.module", boards_ok, str(BOARDS_MODULE / "boards"),
        f"git checkout -- {BOARDS_MODULE.relative_to(ROOT)}")
    stale: list[str] = []
    try:
        zb = WEST / str(zephyr.get("path") or "zephyr")
        for bm in (BOARDS_MODULE / "boards").rglob("board.yml"):
            name = bm.parent.name
            for hit in zb.glob(f"boards/*/{name}"):
                stale.append(str(hit.relative_to(ROOT)))
    except OSError:
        pass
    add("boards.no-tree-copy", not stale,
        "无树内副本" if not stale else f"树内残留：{', '.join(stale[:3])}",
        "删除上列目录（module 化后树内不得保留副本）")

    # 6. SDK / 工具链 / 构建三件套
    sdk = _resolve(z.get("sdk"))
    add("zephyr.sdk", bool(sdk) and Path(sdk).is_dir(), sdk or "未声明",
        f"{_fix(py)} --write-local  # 或设置 ZEPHYR_SDK_INSTALL_DIR")
    for key in ("cmake", "ninja", "ccache"):
        path = _resolve((tools.get(key) or {}).get("path"))
        add(f"tool.{key}", bool(path) and Path(path).is_file(), path or "未声明",
            f"{py} -m pip install {key}" if key != "ccache" else "安装 ccache 后重跑检查")
    return checks


def _first_fix(checks: list[dict]) -> str:
    for c in checks:
        if not c["ok"] and c.get("fix"):
            return c["fix"]
    return ""


def write_local() -> int:
    """把检测到的 SDK / gnu bin 写入 host.local.yaml 的 kharon: 分节。"""
    host = _host()
    z = ((host.get("tools") or {}).get("zephyr") or {})
    sdk = _resolve(z.get("sdk")) or os.environ.get("ZEPHYR_SDK_INSTALL_DIR", "")
    gnu = _resolve(z.get("gnu_bin_dir"))
    if not sdk:
        print("[kharon] 未检测到 Zephyr SDK：设置 ZEPHYR_SDK_INSTALL_DIR 后重试",
              file=sys.stderr)
        return 1
    lines = [
        "# 由 kharon.bootstrap --write-local 生成（本机路径，不入库）",
        "kharon:",
        "  tools:",
        "    zephyr:",
    ]
    lines.append(f"      sdk: {Path(sdk).as_posix()}")
    if gnu:
        lines.append(f"      gnu_bin_dir: {Path(gnu).as_posix()}")
    out = ROOT / "host.local.yaml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[kharon] 已写入 {out.relative_to(ROOT)}")
    return 0


def apply(offline: bool) -> int:
    """幂等补齐：venv → west → workspace。已存在的部分一律不动。"""
    py = str(_venv_python())
    if not _venv_python().is_file():
        if offline:
            print(f"[kharon] --offline：缺少 venv，跳过创建。修复：{sys.executable} -m venv {VENV}",
                  file=sys.stderr)
            return 1
        print(f"[kharon] 创建 venv：{VENV}")
        rc, out = _run([sys.executable, "-m", "venv", str(VENV)])
        if rc != 0:
            print(out, file=sys.stderr)
            return rc
        rc, out = _run([py, "-m", "pip", "install", "-U", "west"])
        if rc != 0:
            print(out, file=sys.stderr)
            return rc
    if not (WEST / ".west" / "config").is_file():
        if offline:
            print("[kharon] --offline：缺少 workspace，跳过 west init。修复："
                  f"{py} -m west init -m <zephyr-remote> --mr <commit> {WEST}", file=sys.stderr)
            return 1
        z = (_load_manifest().get("zephyr") or {})
        print(f"[kharon] 初始化 workspace：{WEST}")
        rc, out = _run([py, "-m", "west", "init", "-m", str(z.get("remote")),
                        "--mr", str(z.get("commit")), str(WEST)])
        if rc != 0:
            print(out, file=sys.stderr)
            return rc
        rc, out = _run([py, "-m", "west", "update", "--narrow", "-o=--depth=1"], cwd=WEST)
        if rc != 0:
            print(out, file=sys.stderr)
            return rc
    checks = collect()
    # 全新 clone：自动安装随仓的官方适配包（packs/official/zephyr）。
    # 只在"一个已装包都没有"时生效；用户卸载内置包后不会被悄悄装回来。
    from elab.config import Config
    from elab.platform import pkg as pkg_mod
    pkg_mod.ensure_bootstrap(Config(ROOT), log=print)
    checks = collect()
    bad = [c for c in checks if not c["ok"]]
    if bad:
        print("[kharon] 仍有缺失：", file=sys.stderr)
        for c in bad:
            print(f"  - {c['name']}: {c['detail']}", file=sys.stderr)
        print(f"修复：{_first_fix(bad)}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kharon bootstrap",
                                 description="Zephyr 工作区引导（可复现、离线可查）")
    ap.add_argument("--check", action="store_true", help="只读体检（默认行为）")
    ap.add_argument("--offline", action="store_true", help="只用本机已有目录，不联网")
    ap.add_argument("--write-local", action="store_true", dest="write_local",
                    help="把检测到的 SDK 路径写入 host.local.yaml")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args(argv)

    if args.write_local:
        return write_local()
    if not args.check and not args.offline:
        rc = apply(offline=False)
        if rc != 0:
            return rc
    checks = collect()
    if args.json:
        print(json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks},
                         ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"  [{'ok' if c['ok'] else '!!'}] {c['name']:<22} {c['detail']}")
    bad = [c for c in checks if not c["ok"]]
    if bad:
        print(f"修复：{_first_fix(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
