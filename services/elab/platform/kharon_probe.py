"""elab.kharon_probe —— Zephyr 应用识别 + project yaml 生成（T12）。

与 classic 的 ``elab adapt`` 刻意分开：``adapt`` 随 classic 退场，本模块**只认
Zephyr**，产出的 yaml 也只写 Zephyr 的形态（``archetype: zephyr`` + provider）。

判定三件套（方案 T12）：

1. ``CMakeLists.txt`` 含 ``find_package(Zephyr``；
2. ``prj.conf`` 存在（信息性，缺了只是提示）；
3. 向上找 ``.west/config`` — 认出 west 工作区根（信息性，写进事实源注释）。

芯片判定不猜：``boards/*.overlay`` 的文件名与芯片包 ``target`` 的
``/``→``_`` 全等即命中；多块板 / 多候选 → 返回 ``ambiguities[]``，由
``--board``/``--chip`` 人工裁决。

用法::

    kharon adapt applications/stm32f4/f411_breath          # 打印结论
    kharon adapt ... --write                               # 写 projects/kharon/<name>.yaml
    kharon adapt ... --board esp32s3_devkitc               # 多板目录显式选板
    kharon adapt ... --name my_app --chip blackpill_f411ce
"""

from __future__ import annotations

import re
from pathlib import Path

from elab.core.config import Config, ElabError, to_fwd

CMAKE_INCLUDE = "find_package(Zephyr"


def _rel_or_abs(cfg: Config, p: Path) -> str:
    """仓库内 → 相对路径（正斜杠）；仓库外 → 绝对路径（正斜杠）。"""
    try:
        return to_fwd(p.resolve().relative_to(Path(cfg.root).resolve()))
    except ValueError:
        return to_fwd(p.resolve())


def _west_top(app: Path) -> Path | None:
    """向上找 ``.west/config``（west 工作区根）；没有返回 None。"""
    for anc in (app, *app.parents):
        if (anc / ".west" / "config").is_file():
            return anc
    return None


def _project_name(cmake_text: str, fallback: str) -> str:
    m = re.search(r"project\s*\(\s*([A-Za-z0-9_.-]+)", cmake_text)
    return m.group(1) if m else fallback


def _board_stems(app: Path) -> list[str]:
    d = app / "boards"
    return sorted(p.stem for p in d.glob("*.overlay")) if d.is_dir() else []


def _match_chips(cfg: Config, stems: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """板名 → 芯片候选 ``[(id, 证据)]``；第二项是无法匹配的板名。"""
    hits: list[tuple[str, str]] = []
    unmatched: list[str] = []
    seen: set[str] = set()
    for stem in stems:
        found = False
        low = stem.lower()
        for ref, chip in cfg.all_chips.items():
            cid = str(chip.get("id") or "")
            if not cid or cid in seen:
                continue
            target = str(chip.get("target") or "")
            if not target:
                continue
            flat = target.replace("/", "_").lower()
            if flat == low:
                hits.append((cid, f"boards/{stem}.overlay == target {target!r}"))
                seen.add(cid)
                found = True
                break
            seg = target.split("/")
            if len(seg) >= 2 and "_".join(seg[:2]).lower() == low:
                hits.append((cid, f"boards/{stem}.overlay ≈ target {target!r}（前两段）"))
                seen.add(cid)
                found = True
                break
        if not found:
            unmatched.append(stem)
    return hits, unmatched


def probe(cfg: Config, app, *, chip: str = "", board: str = "",
          name: str = "", work_dir: str = "") -> dict:
    """识别一个 Zephyr app，返回结论（可 JSON 化），不做任何写操作。"""
    app_p = Path(app)
    if not app_p.is_absolute():
        app_p = Path(cfg.root) / app_p
    app_p = app_p.resolve()
    if not app_p.is_dir():
        raise ElabError(f"目录不存在：{app_p}")

    cmake = app_p / "CMakeLists.txt"
    if not cmake.is_file():
        raise ElabError(f"{app_p} 没有 CMakeLists.txt —— 不像 Zephyr app")
    cmake_text = cmake.read_text(encoding="utf-8", errors="replace")
    if CMAKE_INCLUDE.lower() not in cmake_text.lower():
        raise ElabError(
            f"{cmake} 里没有 {CMAKE_INCLUDE} —— 不是 Zephyr 应用")

    facts: list[str] = [f"CMakeLists.txt 含 {CMAKE_INCLUDE}"]
    ambiguities: list[str] = []
    if (app_p / "prj.conf").is_file():
        facts.append("prj.conf 存在")
    else:
        ambiguities.append("缺少 prj.conf（Zephyr 工程通常有；继续但不代为创建）")
    west = _west_top(app_p)
    if west:
        facts.append(f"west 工作区根：{to_fwd(west)}")
        if (west / "west.yml").is_file():
            facts.append("west.yml 存在（manifest 可复核）")
    if (app_p / "sysbuild.conf").is_file():
        facts.append("sysbuild.conf 存在（MCUboot 双镜像）")

    stems = _board_stems(app_p)
    hits, unmatched = _match_chips(cfg, stems) if stems else ([], [])

    chip_id = ""
    if chip:
        if chip not in cfg.all_chips:
            raise ElabError(f"未知芯片：{chip}（芯片包未安装？）")
        chip_id = str(cfg.all_chips[chip].get("id") or chip)
        facts.append(f"--chip 显式指定 {chip_id}")
    elif board:
        direct = cfg.all_chips.get(board)
        if direct:
            chip_id = str(direct.get("id") or board)
            facts.append(f"--board {board} → 芯片 {chip_id}")
        else:
            one, _miss = _match_chips(cfg, [board])
            if not one:
                raise ElabError(f"板名/芯片 {board!r} 在已安装芯片包里没有对应 target")
            chip_id = one[0][0]
            facts.append(f"--board {board} → 芯片 {chip_id}（{one[0][1]}）")
    elif len(hits) == 1:
        chip_id, ev = hits[0]
        facts.append(f"芯片 {chip_id}（{ev}）")
    elif len(hits) > 1:
        cands = "、".join(c for c, _ in hits)
        ambiguities.append(f"多个芯片候选（{cands}）—— 用 --board/--chip 指定")
    else:
        if stems:
            ambiguities.append(
                f"boards/*.overlay（{', '.join(stems)}）在已安装芯片包里没有 "
                f"对应 target —— 需安装/新建芯片包，或用 --chip")
        else:
            ambiguities.append("工程没有 boards/*.overlay —— 请用 --chip 显式指定")
    if unmatched and chip_id:
        # 选中的板之外还有别的板：提示其它板没进 yaml（一个工程 = app+board）
        ambiguities.append(
            f"另有未采用板 overlay：{', '.join(unmatched)}（本 yaml 只登记一块板）")

    proj_name = name or _project_name(cmake_text, app_p.name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", proj_name):
        raise ElabError(f"工程名不合法：{proj_name!r}")

    if work_dir:
        work = Path(work_dir)
        work = work if work.is_absolute() else Path(cfg.root) / work
    else:
        family = app_p.parent.name
        work = Path(cfg.root) / "build" / family / proj_name / "default"

    chip_data = (cfg.all_chips.get(chip_id) or {}) if chip_id else {}
    dbg = chip_data.get("debug") or {}
    probe_kind = str(dbg.get("probe") or "")
    if not probe_kind and dbg.get("openocd_config"):
        # 整份 openocd cfg（含 interface/target）通常就是 DAP-Link
        probe_kind = "dap"
        facts.append("openocd_config 存在 → probe=dap")

    return {
        "app": to_fwd(app_p),
        "root": _rel_or_abs(cfg, app_p),
        "name": proj_name,
        "chip": chip_id,
        "pack": "zephyr/framework",
        "board": stems[0] if len(stems) == 1 else (board or ""),
        "boards": stems,
        "west_top": to_fwd(west) if west else "",
        "work_dir": _rel_or_abs(cfg, work),
        "probe": probe_kind,
        "facts": facts,
        "ambiguities": ambiguities,
    }


def render_yaml(p: dict) -> str:
    """按 projects/kharon/*.yaml 的既有形态渲染（不引入第二套字段）。"""
    lines = [
        f"# projects/kharon/{p['name']}.yaml —— 由 `kharon adapt` 生成（T12）",
        "# 事实源：CMakeLists.txt 的 find_package(Zephyr) + boards/*.overlay + 芯片包",
    ]
    for f in p["facts"]:
        lines.append(f"#   - {f}")
    if p["west_top"]:
        lines.append(f"# west 工作区：{p['west_top']}")
    for a in p["ambiguities"]:
        lines.append(f"# ⚠ {a}")
    lines += [
        "",
        f"name: {p['name']}",
        f"root: ${{ELAB_ROOT}}/{p['root']}",
        f"pack: {p['pack']}",
        f"chip: {p['chip']}",
        "archetype: zephyr",
        "provider: zephyr/west@>=1 <2",
        "",
        "build:",
        "  kind: cmake",
        "  source: ${root}/CMakeLists.txt",
        f"  work_dir: ${{ELAB_ROOT}}/{p['work_dir']}",
        "",
        "artifacts:",
        "  elf: ${work_dir}/zephyr/zephyr.elf",
        "  bin: ${work_dir}/zephyr/zephyr.bin",
        "  hex: ${work_dir}/zephyr/zephyr.hex",
        "  map: ${work_dir}/zephyr/zephyr.map",
    ]
    if p["probe"]:
        lines += ["", "debug:", f"  probe: {p['probe']}"]
    lines += [
        "",
        "serial:",
        "  port: auto",
        "  baud: 115200",
        "  reconnect: { enabled: true, min_ms: 200, max_ms: 5000, max_attempts: 0 }",
        "",
        "monitor:",
        "  fail_on:",
        "    - { kind: regex, pattern: \"FATAL ERROR|ASSERTION FAIL|Unhandled kernel exception\" }",
        "  idle_timeout_s: 30",
        "",
    ]
    return "\n".join(lines)


def cmd_adapt(argv: list[str]) -> int:
    """``kharon adapt`` 子命令。"""
    import argparse

    ap = argparse.ArgumentParser(
        prog="kharon adapt",
        description="识别 Zephyr 应用并生成 projects/kharon/<name>.yaml（T12）")
    ap.add_argument("app", help="Zephyr 应用目录（含 find_package(Zephyr) 的 CMakeLists.txt）")
    ap.add_argument("--name", default="", help="工程名（缺省取 CMakeLists 的 project()）")
    ap.add_argument("--chip", default="", help="显式芯片 id（缺省按 boards/*.overlay 推断）")
    ap.add_argument("--board", default="", help="显式板名（多块 overlay 目录用）")
    ap.add_argument("--work-dir", default="", help="构建目录（缺省 build/<家族>/<name>/default）")
    ap.add_argument("--projects-dir", default="projects/kharon",
                    help="输出目录（缺省 projects/kharon）")
    ap.add_argument("--write", action="store_true", help="落盘（缺省只打印）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 yaml")
    args = ap.parse_args(argv)

    import os
    from elab.core.config import Config as _Config

    root = Path(os.environ.get("ELAB_ROOT") or Path(__file__).resolve().parents[3])
    cfg = _Config(root)
    p = probe(cfg, args.app, chip=args.chip, board=args.board,
              name=args.name, work_dir=args.work_dir)

    print(f"[kharon adapt] {p['app']}")
    print(f"  工程   {p['name']}   芯片   {p['chip'] or '（未定）'}")
    print(f"  root   {p['root']}")
    print(f"  work   {p['work_dir']}")
    for f in p["facts"]:
        print(f"  ✓ {f}")
    for a in p["ambiguities"]:
        print(f"  ! {a}")

    text = render_yaml(p)
    if not args.write:
        print("\n" + text)
        return 0
    if not p["chip"]:
        print("[kharon adapt] ✗ 芯片未确定，拒绝写 yaml（用 --chip/--board 指定）")
        return 2
    out_dir = Path(args.projects_dir)
    out_dir = out_dir if out_dir.is_absolute() else Path(cfg.root) / out_dir
    out = out_dir / f"{p['name']}.yaml"
    if out.exists() and not args.force:
        print(f"[kharon adapt] ✗ 已存在：{to_fwd(out)}（--force 覆盖）")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"[kharon adapt] ✓ 已写入 {to_fwd(out)}")
    return 0
