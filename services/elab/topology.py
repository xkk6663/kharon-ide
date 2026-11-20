"""elab.topology —— Board / APP / Product 的只读拓扑（驾驶舱架构视图的事实源）。

这个模块只回答三个问题，不猜测、不跑 west：

* 这块板级真相住在哪（``kharon-boards/boards/**/board.yml`` + DTS）；
* 这个 APP 是便携 APP、单板实验台，还是产品工程（``app.yaml`` / CMake）；
* 这个产品 target 落在哪块板、哪个 APP、哪颗芯片（``products/*.yaml``）。

输出是稳定的只读字典，不包含任何写盘动作；驾驶舱只渲染它。
"""

from __future__ import annotations

import re
from pathlib import Path

from elab.core import _yaml
from elab.core.config import Config, ElabError, to_fwd

#: 只扫这些浅层目录，避免把 ``build/``、``.git/`` 扫进来。
_SKIP_DIRS = {"build", ".git", ".west", "zephyr", "modules", "tools"}


def _yaml_load(path: Path) -> dict:
    try:
        data = _yaml.load_file(path) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── DTS 事实（只抽可稳定展示的键，不做完整 DTS 解析）────────────
def _dts_facts(dts_files: list[Path]) -> dict:
    text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in dts_files
    )
    chosen = {}
    for m in re.finditer(r"zephyr,(console|shell-uart)\s*=\s*&([A-Za-z0-9_]+)", text):
        chosen[m.group(1)] = m.group(2)

    aliases: list[str] = []
    block = re.search(r"aliases\s*\{([\s\S]*?)\};", text)
    if block:
        for m in re.finditer(r"^\s*([A-Za-z0-9_-]+)\s*=\s*&", block.group(1), re.M):
            aliases.append(m.group(1))

    user_props: list[str] = []
    block = re.search(r"zephyr,user\s*\{([\s\S]*?)\};", text)
    if block:
        for m in re.finditer(r"^\s*([A-Za-z0-9_-]+)\s*=", block.group(1), re.M):
            if m.group(1) not in {"compatible"}:
                user_props.append(m.group(1))

    return {
        "console": chosen.get("console", ""),
        "shell_uart": chosen.get("shell-uart", ""),
        "aliases": sorted(set(aliases)),
        "user_props": sorted(set(user_props)),
    }


def _board_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(to_fwd(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file())


def _board_record(root: Path, board_yml: Path, source: str) -> dict:
    data = _yaml_load(board_yml)
    node = data.get("board") or {}
    socs = node.get("socs") or []
    soc_names = []
    if isinstance(socs, list):
        for soc in socs:
            if isinstance(soc, dict):
                soc_names.append(str(soc.get("name") or soc.get("cpucluster") or ""))
            elif soc:
                soc_names.append(str(soc))
    dts_files = sorted(board_yml.parent.glob("*.dts")) + sorted(board_yml.parent.glob("*.dtsi"))
    return {
        "id": str(node.get("name") or board_yml.parent.name),
        "name": str(node.get("name") or board_yml.parent.name),
        "full_name": str(node.get("full_name") or ""),
        "vendor": str(node.get("vendor") or board_yml.parent.parent.name),
        "socs": [x for x in soc_names if x],
        "source": source,
        "root": to_fwd(board_yml.parent),
        "board_yml": to_fwd(board_yml),
        "files": _board_files(board_yml.parent),
        "dts": _dts_facts(dts_files),
        "apps": [],
        "products": [],
    }


def _upstream_board(cfg: Config, board_id: str) -> dict | None:
    """按产品 target 的板名，到内核仓 ``boards/*/<board>/board.yml`` 找。"""
    base = Path(cfg.zephyr_root) / "zephyr" / "boards"
    if not base.is_dir():
        return None
    for bm in sorted(base.glob(f"*/{board_id}/board.yml")):
        return _board_record(base, bm, "zephyr")
    return None


# ── APP 目录 ────────────────────────────────────────────────────
def _app_record(app_root: Path, cfg: Config) -> dict:
    data = _yaml_load(app_root / "app.yaml")
    overlays = sorted(
        to_fwd(p.relative_to(app_root))
        for p in (app_root / "boards").rglob("*")
        if p.is_file() and p.suffix in {".overlay", ".conf", ".dts", ".dtsi"}
    ) if (app_root / "boards").is_dir() else []
    return {
        "name": app_root.name,
        "root": to_fwd(app_root),
        "app_yaml": to_fwd(app_root / "app.yaml") if (app_root / "app.yaml").is_file() else "",
        "kind": str(data.get("kind") or "product"),
        "board_scope": str(data.get("board_scope") or ""),
        "declared_board": str(data.get("board") or ""),
        "requires": [str(x) for x in (data.get("requires") or []) if str(x)],
        "optional": [str(x) for x in (data.get("optional") or []) if str(x)],
        "overlays": overlays,
        "products": [],
    }


def _app_dirs(cfg: Config) -> list[Path]:
    root = Path(cfg.zephyr_root) / "applications"
    if not root.is_dir():
        return []
    out: list[Path] = []
    for cmake in sorted(root.rglob("CMakeLists.txt")):
        parent = cmake.parent
        if any(part in _SKIP_DIRS for part in parent.parts):
            continue
        if parent not in out:
            out.append(parent)
    return out


def topology(cfg: Config) -> dict:
    """返回驾驶舱架构视图：boards / apps / products / warnings。"""
    root = Path(cfg.zephyr_root)
    boards: dict[str, dict] = {}
    warnings: list[str] = []

    # 1) Kharon 自研板：板级真相的唯一宿主
    kharon_root = root / "kharon-boards"
    for bm in sorted((kharon_root / "boards").glob("*/*/board.yml")):
        rec = _board_record(kharon_root, bm, "kharon")
        boards[rec["id"]] = rec

    # 2) APP：以 CMakeLists 为锚，app.yaml 只补充 APP 级差异
    apps: dict[str, dict] = {}
    app_paths: list[tuple[Path, dict]] = []
    for app_root in _app_dirs(cfg):
        rec = _app_record(app_root, cfg)
        apps[rec["name"]] = rec
        app_paths.append((app_root.resolve(), rec))

    def app_for(root_raw: str) -> dict | None:
        if not root_raw:
            return None
        target = Path(root_raw).resolve()
        for app_root, rec in app_paths:
            if target == app_root or app_root in target.parents:
                return rec
        return None

    # 3) Product + target：每个 target 独立解析，失败进 warnings 而不是整页 500
    products: list[dict] = []
    for name, raw in sorted(cfg.projects.items()):
        try:
            raw_targets = cfg.targets_of(raw)
        except ElabError as e:
            warnings.append(str(e))
            raw_targets = []
        if not raw_targets:
            entries = [{"id": "", "chip": str(raw.get("chip") or "")}]
        else:
            entries = raw_targets
        targets = []
        for t in entries:
            tid = str(t.get("id") or "")
            try:
                proj = cfg.resolved_project(name, tid or None)
                chip = cfg.chip_of(proj)
            except ElabError as e:
                warnings.append(f"{name}/{tid or 'default'}：{e}")
                continue
            chip_target = str(chip.get("target") or chip.get("id") or "")
            board_id = chip_target.split("/")[0] if chip_target else ""
            app = app_for(str(proj.get("root") or ""))
            row = {
                "product": name,
                "target": tid,
                "chip": str(proj.get("chip") or ""),
                "board": board_id,
                "board_target": chip_target,
                "app": app["name"] if app else Path(str(proj.get("root") or "")).name,
                "app_root": app["root"] if app else to_fwd(proj.get("root") or ""),
                "root": to_fwd(proj.get("root") or ""),
                "work_dir": to_fwd(proj.get("work_dir") or ""),
            }
            targets.append(row)
            if app is not None:
                app["products"].append({"product": name, "target": tid})
            if board_id:
                if board_id not in boards:
                    up = _upstream_board(cfg, board_id)
                    if up:
                        boards[board_id] = up
                    else:
                        warnings.append(
                            f"产品 {name}/{tid or 'default'} 指向未知板 {board_id}"
                            f"（target={chip_target}）")
                if board_id in boards:
                    boards[board_id]["products"].append({"product": name, "target": tid})
                    if app is not None and app["name"] not in {
                        a["name"] for a in boards[board_id]["apps"]
                    }:
                        boards[board_id]["apps"].append({"name": app["name"], "root": app["root"]})
        products.append({
            "name": name,
            "root": to_fwd(raw.get("root") or ""),
            "yaml": to_fwd(raw.get("_path") or ""),
            "archetype": str(raw.get("archetype") or ""),
            "targets": targets,
        })

    # 4) 去重 APP 关联（同一 APP 的多个 target 只留一条）
    for b in boards.values():
        b["products"] = sorted(
            {(x["product"], x["target"]): x for x in b["products"]}.values(),
            key=lambda x: (x["product"], x["target"]),
        )
        b["apps"] = sorted({x["name"]: x for x in b["apps"]}.values(),
                           key=lambda x: x["name"])
    for a in apps.values():
        a["products"] = sorted(
            {(x["product"], x["target"]): x for x in a["products"]}.values(),
            key=lambda x: (x["product"], x["target"]),
        )

    # 在用的板（有产品 target）排前面；空壳/历史板留在列表里但靠后。
    boards_out = sorted(
        boards.values(),
        key=lambda x: (-len(x["products"]), -len(x["apps"]), x["source"], x["id"]),
    )
    return {
        "zephyr_root": to_fwd(root),
        "boards": boards_out,
        "apps": sorted(apps.values(), key=lambda x: x["name"]),
        "products": products,
        "warnings": warnings,
    }
