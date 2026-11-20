"""kharon.bootstrap —— Zephyr 工作区引导（学习 / 可复现双通道）。

    kharon bootstrap                 # 学习通道：跟随上游 + 全量 west update（全新 clone 默认）
    kharon bootstrap --pinned        # 复现通道：manifest 钉 commit + 6 个模块闭包
    kharon bootstrap --check         # 只读体检（离线可跑；默认沿用当前工作区通道）
    kharon bootstrap --offline       # 只用本机已有目录，不联网
    kharon bootstrap --json          # 体检结果机读
    kharon bootstrap --write-local   # 把检测到的 SDK 路径写入 host.local.yaml

通道语义
--------
- ``learning``：跟 Zephyr 教程/官方文档一致。``west init`` 不钉分支，``west update``
  全量拉 manifest 里的所有 project。适合第一次学 Zephyr、看完整 boards/samples/drivers。
- ``pinned``：按 ``manifests/zephyr.west.yml`` 的 zephyr commit 初始化，只 ``west update``
  ``zephyr.pinned_modules`` 里那 6 个模块。适合 CI、回归、复现某次实验。

两条通道只影响“拉多少、跟不跟上游”，不会改构建链。切换通道只做增量补齐，不删除
已有模块；``west`` 是 junction 时拒绝联网更新，避免把旧仓工作区改掉。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from kharon import roots

ROOT = roots.ide_root()
IDE_ROOT = ROOT
ZEPHYR_ROOT = roots.zephyr_root()
#: 目标布局用 workspace 内的 kharon-manifest；旧布局仍用 IDE 的 manifests 快照
MANIFEST = (ZEPHYR_ROOT / "kharon-manifest" / "west.yml"
            if (ZEPHYR_ROOT / "kharon-manifest" / "west.yml").is_file()
            else ROOT / "manifests" / "zephyr.west.yml")
VENV = ROOT / ".deps" / "venv"
BOARDS_MODULE = (ZEPHYR_ROOT / "kharon-boards"
                 if (ZEPHYR_ROOT / "kharon-boards").is_dir()
                 else ROOT / "zephyr_modules" / "elab-boards")
WEST = ZEPHYR_ROOT
STATE = ((ZEPHYR_ROOT / ".kharon" / "bootstrap.json")
         if (ZEPHYR_ROOT / "kharon-manifest").is_dir()
         else (ROOT / ".kharon" / "bootstrap.json"))

LEARNING = "learning"
PINNED = "pinned"
VALID_CHANNELS = (LEARNING, PINNED)

#: manifest 没写 ``zephyr.pinned_modules`` 时的兜底；旧仓实测就是这样 6 个。
PINNED_FALLBACK = (
    "hal_espressif",
    "hal_stm32",
    "cmsis_6",
    "mbedtls",
    "tf-psa-crypto",
    "mcuboot",
)


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
    s = str(value or "")
    return (s.replace("${IDE_ROOT}", IDE_ROOT.as_posix())
             .replace("${ELAB_ROOT}", IDE_ROOT.as_posix())
             .replace("${ZEPHYR_ROOT}", ZEPHYR_ROOT.as_posix()))


def _run(cmd: list[str], cwd: Path | None = None,
         timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            **_winproc_kwargs(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _winproc_kwargs() -> dict:
    """Windows 下不弹窗；非 Windows 返回空表。"""
    if not sys.platform.startswith("win"):
        return {}
    try:
        from elab import _winproc

        return _winproc.proc_kwargs()
    except Exception:                                    # noqa: BLE001
        return {}


def _fix(py: str) -> str:
    return f"{py} -m kharon.bootstrap"


def _pinned_modules(manifest: dict) -> list[str]:
    zephyr = manifest.get("zephyr") or {}
    names = zephyr.get("pinned_modules") or list(PINNED_FALLBACK)
    return [str(n) for n in names if str(n).strip()]


def _state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(channel: str) -> None:
    zephyr = (_load_manifest().get("zephyr") or {})
    base = WEST / str(zephyr.get("path") or "zephyr")
    head = ""
    if (base / ".git").exists():
        rc, out = _run(["git", "-C", str(base), "rev-parse", "HEAD"], timeout=30)
        head = out.strip() if rc == 0 else ""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "channel": channel,
        "zephyr_commit": head,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_redirect(path: Path) -> bool:
    """``path`` 是否指向别处（Windows junction / symlink）。"""
    try:
        return path.resolve() != path.absolute()
    except OSError:
        return False


def _workspace_exists() -> bool:
    return (WEST / ".west" / "config").is_file()


def _present_projects(py: str) -> list[dict]:
    """``west list`` 实际声明了哪些 project（离线，不联网）。"""
    rc, out = _run([py, "-m", "west", "list", "--format", "{name}|{path}"],
                   cwd=WEST, timeout=90)
    if rc != 0:
        return []
    projects: list[dict] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, path = line.split("|", 1)
        name, path = name.strip(), path.strip()
        if not name or name == "manifest" or not path:
            continue
        projects.append({"name": name, "path": path})
    return projects


def _required_modules(channel: str, manifest: dict, py: str) -> tuple[list[dict], str]:
    """返回当前通道需要检查的模块和标签。

    - pinned：只认 ``zephyr.pinned_modules``，路径从 manifest 取；
    - learning：认 workspace 当前 ``west list`` 的全部 project（排除 optional），
      这样上游新增/删除模块时不会被仓库里的旧快照误判。
    """
    projects = manifest.get("projects") or []
    optional = {str(p.get("name")) for p in projects if p.get("optional")}

    if channel == PINNED:
        paths = {str(p.get("name")): str(p.get("path") or "") for p in projects}
        return [
            {"name": name, "path": paths.get(name, "")}
            for name in _pinned_modules(manifest)
        ], "钉版本模块"

    actual = _present_projects(py)
    if actual:
        return [p for p in actual if p["name"] not in optional], "模块"

    return [p for p in projects if str(p.get("name")) not in optional], "清单"


def _missing(modules: list[dict]) -> list[dict]:
    return [
        p for p in modules
        if not str(p.get("path") or "")
        or not (WEST / str(p["path"])).is_dir()
    ]


def _infer_channel() -> str:
    """没有状态文件时，从盘上形态推断通道。

    全新 clone（没有 west）→ learning；已有工作区里只落了少数模块 → pinned；
    已经落了大部分模块 → learning。
    """
    if not _workspace_exists():
        return LEARNING
    manifest = _load_manifest()
    projects = manifest.get("projects") or []
    present = sum(
        1 for p in projects
        if str(p.get("path") or "") and (WEST / str(p["path"])).is_dir()
    )
    return LEARNING if present >= 10 else PINNED


def _resolve_channel(explicit: str) -> str:
    active = _state().get("channel")
    if active not in VALID_CHANNELS:
        active = _infer_channel()
    if explicit and explicit != active:
        print(f"[kharon] 通道 {active} → {explicit}；只增量补齐，不删除已有模块")
    return explicit or active


def collect(channel: str) -> list[dict]:
    """跑全部检查，返回 ``[{name, ok, detail, fix}]``（不改盘、不联网）。"""
    checks: list[dict] = []
    py = str(_venv_python())

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    # 1. manifest / host profile
    manifest = _load_manifest()
    zephyr = manifest.get("zephyr") or {}
    add("manifest", bool(zephyr.get("commit")),
        f"{MANIFEST.name}: zephyr@{str(zephyr.get('commit'))[:12]} / {channel}")
    try:
        _host()
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
    ws_ok = _workspace_exists()
    if channel == PINNED:
        init_fix = (f"{py} -m west init -m {zephyr.get('remote')} "
                    f"--mr {zephyr.get('commit')} {WEST}")
    else:
        init_fix = f"{py} -m west init -m {zephyr.get('remote')} {WEST}"
    add("workspace", ws_ok, str(WEST / ".west" / "config"), init_fix)
    head = ""
    zb = WEST / str(zephyr.get("path") or "zephyr")
    if (zb / ".git").exists():
        rc, out = _run(["git", "-C", str(zb), "rev-parse", "HEAD"])
        head = out.strip() if rc == 0 else ""
    if channel == PINNED:
        want = str(zephyr.get("commit") or "")
        pinned = want not in ("", "HEAD")
        add("zephyr.commit", (not pinned) or head == want,
            f"HEAD={head[:12] or '?'} 期望={want[:12] or '?'}",
            f"git -C {zb} checkout {want}")
    else:
        add("zephyr.commit", bool(head),
            f"HEAD={head[:12] or '?'}（learning 跟随上游，不钉版本）",
            init_fix)

    # 4. modules：pinned 只认 6 个闭包；learning 认当前 workspace 的全部 project
    modules, label = _required_modules(channel, manifest, py)
    missing = _missing(modules)
    total = len(modules)
    detail = f"{total - len(missing)}/{total} {label}就绪"
    if missing:
        detail += f"；缺：{', '.join(str(p['name']) for p in missing[:8])}"
        if total > 8 and len(missing) > 8:
            detail += " …"
    fix = ""
    if missing:
        if channel == PINNED:
            fix = f"{py} -m west update {' '.join(str(p['name']) for p in missing)}  # cwd={WEST}"
        elif _is_redirect(WEST):
            fix = "先移除 west junction，再运行 kharon bootstrap --learning 重建工作区"
        else:
            fix = f"{py} -m west update  # cwd={WEST}（全量）"
    add("modules", not missing, detail, fix)

    # 5. 板级 module（真相）与树内残留副本（必须不存在）
    boards_ok = (BOARDS_MODULE / "boards").is_dir()
    add("boards.module", boards_ok, str(BOARDS_MODULE),
        f"git checkout -- {BOARDS_MODULE}")
    stale: list[str] = []
    try:
        for bm in (BOARDS_MODULE / "boards").rglob("board.yml"):
            name = bm.parent.name
            for hit in zb.glob(f"boards/*/{name}"):
                stale.append(str(hit))
    except OSError:
        pass
    add("boards.no-tree-copy", not stale,
        "无树内副本" if not stale else f"树内残留：{', '.join(stale[:3])}",
        "删除上列目录（module 化后树内不得保留副本）")

    # 6. SDK / 工具链 / 构建三件套
    host = _host()
    tools = host.get("tools") or {}
    z = tools.get("zephyr") or {}
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


def _init_workspace(py: str, channel: str, zephyr: dict) -> int:
    remote = str(zephyr.get("remote") or "").strip()
    if not remote:
        print("[kharon] manifest 缺少 zephyr.remote", file=sys.stderr)
        return 1
    cmd = [py, "-m", "west", "init", "-m", remote]
    if channel == PINNED:
        commit = str(zephyr.get("commit") or "").strip()
        if not commit:
            print("[kharon] pinned 通道缺少 zephyr.commit", file=sys.stderr)
            return 1
        cmd += ["--mr", commit]
    cmd.append(str(WEST))
    print(f"[kharon] 初始化 workspace（{channel}）：{WEST}")
    rc, out = _run(cmd, timeout=600)
    if rc != 0:
        print(out, file=sys.stderr)
    return rc


def _update_workspace(py: str, channel: str, manifest: dict) -> int:
    if channel == PINNED:
        modules, _ = _required_modules(channel, manifest, py)
        missing = _missing(modules)
        if not missing:
            print("[kharon] pinned 模块已齐，跳过 west update")
            return 0
    else:
        missing = []

    if _is_redirect(WEST):
        print("[kharon] west 是 junction / symlink，联网更新会改到目标仓库；"
              "已拒绝。请先移除 west 链接，再重建工作区。", file=sys.stderr)
        return 1

    if channel == PINNED:
        names = [str(p["name"]) for p in missing]
        cmd = [py, "-m", "west", "update", *names]
        print(f"[kharon] 拉取 pinned 模块：{' '.join(names)}")
    else:
        cmd = [py, "-m", "west", "update"]
        print("[kharon] learning 通道：全量 west update（首次约 1.1G，可能较久）")

    rc, out = _run(cmd, cwd=WEST, timeout=3600)
    if rc != 0:
        print(out, file=sys.stderr)
    return rc


def apply(channel: str, offline: bool) -> int:
    """幂等补齐：venv → workspace → 通道模块。"""
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

    manifest = _load_manifest()
    zephyr = manifest.get("zephyr") or {}

    if not _workspace_exists():
        if offline:
            print("[kharon] --offline：缺少 workspace，跳过 west init。修复："
                  f"{py} -m west init -m <zephyr-remote> {WEST}", file=sys.stderr)
            return 1
        rc = _init_workspace(py, channel, zephyr)
        if rc != 0:
            return rc

    rc = _update_workspace(py, channel, manifest)
    if rc != 0:
        return rc

    _write_state(channel)

    # 全新 clone：自动安装随仓的官方适配包（packs/official/zephyr）。
    # 只在"一个已装包都没有"时生效；用户卸载内置包后不会被悄悄装回来。
    from elab.config import Config
    from elab.platform import pkg as pkg_mod
    pkg_mod.ensure_bootstrap(Config(ROOT), log=print)

    checks = collect(channel)
    bad = [c for c in checks if not c["ok"]]
    if bad:
        print("[kharon] 仍有缺失：", file=sys.stderr)
        for c in bad:
            print(f"  - {c['name']}: {c['detail']}", file=sys.stderr)
        print(f"修复：{_first_fix(bad)}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kharon bootstrap",
        description="Zephyr 工作区引导（learning 全量跟随上游 / pinned 6 模块复现）")
    ap.add_argument("--check", action="store_true", help="只读体检（默认行为）")
    ap.add_argument("--offline", action="store_true", help="只用本机已有目录，不联网")
    ap.add_argument("--write-local", action="store_true", dest="write_local",
                    help="把检测到的 SDK 路径写入 host.local.yaml")
    ap.add_argument("--json", action="store_true", help="机读输出")
    channel_group = ap.add_mutually_exclusive_group()
    channel_group.add_argument(
        "--learning", action="store_true",
        help="学习通道：跟随上游 + 全量 west update（全新 clone 默认）")
    channel_group.add_argument(
        "--pinned", action="store_true",
        help="复现通道：manifest 钉 commit + 6 个模块闭包")
    args = ap.parse_args(argv)

    if args.write_local:
        return write_local()

    explicit = LEARNING if args.learning else PINNED if args.pinned else ""
    channel = _resolve_channel(explicit)

    if not args.check and not args.offline:
        rc = apply(channel, offline=False)
        if rc != 0:
            return rc
    checks = collect(channel)
    if args.json:
        print(json.dumps({
            "ok": all(c["ok"] for c in checks),
            "channel": channel,
            "checks": checks,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[kharon] 通道：{channel}")
        for c in checks:
            print(f"  [{'ok' if c['ok'] else '!!'}] {c['name']:<22} {c['detail']}")
    bad = [c for c in checks if not c["ok"]]
    if bad:
        print(f"修复：{_first_fix(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
