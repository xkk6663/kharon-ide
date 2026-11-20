#!/usr/bin/env python3
"""APP 层边界检查：APP 只放 APP 差异，板级真相必须在 kharon-boards。

检查对象 = Zephyr 工作区的 ``applications/``（外加产品清单里的 target 一致性）。

规则（对应《应用层多板解耦与企业级改造计划》A4）：
  1. APP 内不允许 ``CMakePresets.json``（旧课程模式的绝对路径来源）。
  2. APP 内不允许完整板级定义：``boards/<board>/board.yml``、``board.cmake``、
     ``*.dts``/``*_defconfig``。APP 自己的 ``boards/<board>.overlay`` 是允许的，
     它只描述“这个 APP 额外要开的外设”，不是板级真相。
  3. APP 内不允许 ``common/dap_link_openocd.cfg`` 之类的调试器板级文件。
  4. 非 lab APP 的 C/H 源码里不允许 ``GPIO0_BASE`` / ``sys_read32()`` /
     ``.pin = <数字>`` 这类寄存器或引脚硬编码；``app.yaml: kind: lab`` 的
     教学 APP 可保留，但必须显式标记。
  5. 产品清单里 ``chip`` + ``targets[]`` 不能两个真相；``target`` 的 ``chip``
     必须存在，``id`` 不能重复。
  6. 非原厂 DevKitC 的产品/APP 不应继续引用 ``esp32s3_devkitc``。

用法:
    python scripts/app_boundary_check.py --root <ZEPHYR_ROOT> [--json]

退出码: 0 通过；1 有违规；2 参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

RE_BOARD_DEF = re.compile(r"^(board\.yml|board\.cmake|Kconfig(\.\w+)?|.*\.dts|.*\.dtsi|.*_defconfig)$")
RE_HARDCODE = [
    (re.compile(r"\bGPIO[0-9]+_BASE\b"), "寄存器基址 GPIOx_BASE"),
    (re.compile(r"\bsys_read32\s*\("), "直接 sys_read32()"),
    (re.compile(r"\.pin\s*=\s*[0-9]+"), "GPIO 引脚数字硬编码 .pin = N"),
    (re.compile(r"\b0x6000[4-5][0-9A-Fa-f]{3}\b"), "ESP GPIO 寄存器绝对地址"),
]
RE_DEVKITC = re.compile(r"esp32s3_devkitc")
SKIP_DIRS = {"build", ".work", "zephyr", "modules", "tools", "__pycache__", ".git"}


def load_yaml(path: Path) -> dict:
    """只用 PyYAML；仓库测试环境已有；没有就明确报错而不是静默跳过。"""
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - 环境错误
        raise SystemExit(f"[boundary] 缺少 PyYAML：{exc}") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SystemExit(f"[boundary] 无法解析 {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def app_kind(app_dir: Path) -> str:
    meta = app_dir / "app.yaml"
    if not meta.is_file():
        return ""
    return str(load_yaml(meta).get("kind") or "").strip().lower()


def iter_apps(root: Path):
    apps = root / "applications"
    if not apps.is_dir():
        raise SystemExit(f"[boundary] 找不到 {apps}")
    for p in sorted(apps.rglob("*")):
        if p.is_dir() and any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.name in ("CMakeLists.txt", "prj.conf"):
            yield p.parent


def check_app(root: Path, app: Path) -> list[dict]:
    out: list[dict] = []
    rel = app.relative_to(root).as_posix()
    kind = app_kind(app)

    presets = app / "CMakePresets.json"
    if presets.is_file():
        out.append({"rule": "app.presets", "path": rel + "/CMakePresets.json",
                    "msg": "APP 内不允许 CMakePresets.json（旧课程模式）"})

    boards = app / "boards"
    if boards.is_dir():
        for sub in sorted(boards.iterdir()):
            if not sub.is_dir():
                continue
            if any(f.is_file() and RE_BOARD_DEF.match(f.name) for f in sub.iterdir()):
                out.append({"rule": "app.board_definition", "path": sub.relative_to(root).as_posix(),
                            "msg": "APP 内出现完整板级定义；板级真相应只进 kharon-boards"})

    common = app / "common" / "dap_link_openocd.cfg"
    if common.is_file():
        out.append({"rule": "app.debugger_truth", "path": common.relative_to(root).as_posix(),
                    "msg": "调试器板级配置不允许放在 APP/common"})

    if kind != "lab":
        for f in sorted(app.rglob("*")):
            if f.suffix.lower() not in {".c", ".h"} or not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for regex, label in RE_HARDCODE:
                if regex.search(text):
                    out.append({"rule": "app.hardware_constant",
                                "path": f.relative_to(root).as_posix(),
                                "msg": f"非 lab APP 出现{label}；请移到 Board / zephyr,user"})

    return out


def check_products(root: Path) -> list[dict]:
    out: list[dict] = []
    manifest = root / "kharon-manifest" / "products"
    if not manifest.is_dir():
        return out
    for p in sorted(manifest.rglob("*.yaml")):
        if p.name.endswith(".disabled"):
            continue
        data = load_yaml(p)
        rel = p.relative_to(root).as_posix()
        targets = data.get("targets") or []
        top = str(data.get("chip") or "").strip()
        if targets and not isinstance(targets, list):
            out.append({"rule": "product.targets_type", "path": rel,
                        "msg": "targets 必须是列表"})
            continue
        ids: set[str] = set()
        for t in targets:
            if not isinstance(t, dict):
                out.append({"rule": "product.target_type", "path": rel,
                            "msg": "target 必须是映射"})
                continue
            tid = str(t.get("id") or "").strip()
            chip = str(t.get("chip") or "").strip()
            if not tid or not chip:
                out.append({"rule": "product.target_missing", "path": rel,
                            "msg": f"target 缺 id/chip: {t!r}"})
            if tid in ids:
                out.append({"rule": "product.target_duplicate", "path": rel,
                            "msg": f"target id 重复: {tid}"})
            ids.add(tid)
        if targets and top and top != str((targets[0] or {}).get("chip") or ""):
            out.append({"rule": "product.two_truths", "path": rel,
                        "msg": f"顶层 chip={top} 与 targets[0].chip 不一致"})
        if RE_DEVKITC.search(p.read_text(encoding="utf-8", errors="replace")):
            out.append({"rule": "product.devkitc", "path": rel,
                        "msg": "非原厂 DevKitC 产品不应引用 esp32s3_devkitc"})

    for app_dir in (root / "applications").rglob("*"):
        if not app_dir.is_dir():
            continue
        if any(part in SKIP_DIRS for part in app_dir.parts):
            continue
        if (app_dir / "app.yaml").is_file():
            if RE_DEVKITC.search((app_dir / "app.yaml").read_text(encoding="utf-8", errors="replace")):
                out.append({"rule": "app.devkitc", "path": app_dir.relative_to(root).as_posix(),
                            "msg": "APP 的 app.yaml 仍引用 esp32s3_devkitc"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="APP / Product 边界检查")
    ap.add_argument("--root", default=os.environ.get("ZEPHYR_ROOT", ""),
                    help="Zephyr 工作区根；默认 $ZEPHYR_ROOT")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not args.root:
        print("[boundary] 需要 --root 或 ZEPHYR_ROOT", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[boundary] 根目录不存在：{root}", file=sys.stderr)
        return 2

    violations: list[dict] = []
    seen: set[Path] = set()
    for app in iter_apps(root):
        if app in seen:
            continue
        seen.add(app)
        violations.extend(check_app(root, app))
    violations.extend(check_products(root))

    if args.json:
        print(json.dumps({"root": str(root), "violations": violations}, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"[boundary] OK：{len(seen)} 个 APP，产品清单无 target 冲突")
        else:
            print(f"[boundary] {len(violations)} 条违规：")
            for v in violations:
                print(f"  - [{v['rule']}] {v['path']}: {v['msg']}")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
