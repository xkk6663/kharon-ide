"""elab.architecture_edit —— 架构页 YAML 的受控读写。

驾驶舱架构页允许在 UI 内改 **产品 manifest**（``kharon-manifest/products/**``）
和 **APP 级 ``app.yaml``**，但写入路径只能由拓扑事实反查，客户端不允许传绝对路径。

边界（与 ``scripts/app_boundary_check.py`` 同一口径）：

* 产品 YAML 必须落在 ``<zephyr_root>/kharon-manifest/products`` 下，且顶层
  ``name`` 与所选产品逐字相同；
* APP ``app.yaml`` 必须落在 ``<zephyr_root>/applications/<app>`` 下；
* 写入前先解析 YAML，语法错误直接拒绝（不覆盖磁盘上的旧文件）；
* 采用同目录临时文件 + ``os.replace`` 原子落盘，保留调用方传入的换行。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from elab.core import _yaml
from elab.core.config import Config, ElabError

#: 单文件上限：400 KiB，足够容纳 target 矩阵，又挡住误贴大文件。
MAX_BYTES = 400 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def _safe_name(name: str) -> str:
    if not name or not _NAME_RE.match(name) or name in {".", ".."}:
        raise ElabError(f"非法的名字：{name!r}")
    return name


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _product_path(cfg: Config, name: str) -> Path:
    name = _safe_name(name)
    proj = cfg.projects.get(name)
    if proj is None:
        known = ", ".join(sorted(cfg.projects)) or "(无)"
        raise ElabError(f"未知产品 {name}；现有：{known}")
    raw = str(proj.get("_path") or "")
    if not raw:
        raise ElabError(f"产品 {name} 没有 manifest 路径")
    path = Path(raw)
    products_root = Path(cfg.zephyr_root) / "kharon-manifest" / "products"
    if not _inside(path, products_root):
        raise ElabError(f"产品 {name} 的 manifest 不在 {products_root} 下：{path}")
    return path


def _app_yaml_path(cfg: Config, name: str) -> Path:
    name = _safe_name(name)
    apps_root = Path(cfg.zephyr_root) / "applications"
    if not apps_root.is_dir():
        raise ElabError(f"APP 根不存在：{apps_root}")
    hits = [p for p in apps_root.rglob("CMakeLists.txt")
            if p.parent.name == name and not any(
                part in {"build", ".git", ".west", "zephyr", "modules", "tools"}
                for part in p.parent.parts)]
    if not hits:
        raise ElabError(f"未知 APP {name}（在 {apps_root} 下没有同名 CMakeLists.txt）")
    if len(hits) > 1:
        raise ElabError(f"APP {name} 有多个候选目录，拒绝猜测："
                        + ", ".join(str(p.parent) for p in hits))
    path = hits[0].parent / "app.yaml"
    if not _inside(path.parent, apps_root):
        raise ElabError(f"APP {name} 不在 {apps_root} 下：{path.parent}")
    return path


def _target_path(cfg: Config, kind: str, name: str) -> Path:
    if kind == "product":
        return _product_path(cfg, name)
    if kind == "app":
        return _app_yaml_path(cfg, name)
    raise ElabError(f"kind 只能是 product / app，收到 {kind!r}")


def read_source(cfg: Config, kind: str, name: str) -> dict:
    """读一个 YAML 源文件；``app.yaml`` 不存在时返回空文本而不是 404。"""
    path = _target_path(cfg, kind, name)
    exists = path.is_file()
    text = path.read_text(encoding="utf-8", errors="replace") if exists else ""
    return {
        "kind": kind,
        "name": name,
        "path": str(path),
        "exists": exists,
        "text": text,
        "bytes": path.stat().st_size if exists else 0,
    }


def _validate(kind: str, name: str, text: str) -> dict:
    if not isinstance(text, str):
        raise ElabError("text 必须是字符串")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ElabError(f"文件超过 {MAX_BYTES // 1024} KiB 上限")
    try:
        data = _yaml.load(text) or {}
    except Exception as e:  # noqa: BLE001
        raise ElabError(f"YAML 语法错误：{e}") from e
    if not isinstance(data, dict):
        raise ElabError("YAML 顶层必须是映射（key: value）")
    if kind == "product":
        if str(data.get("name") or "") != name:
            raise ElabError(
                f"产品 manifest 的 name 必须保持 {name!r}（当前 {data.get('name')!r}）；"
                "改名请新建文件并走产品登记")
        targets = data.get("targets")
        if targets is not None and not isinstance(targets, list):
            raise ElabError("targets 必须是列表")
        for i, t in enumerate(targets or []):
            if not isinstance(t, dict) or not str(t.get("id") or ""):
                raise ElabError(f"targets[{i}] 必须是带非空 id 的映射")
    return data


def write_source(cfg: Config, kind: str, name: str, text: str) -> dict:
    """校验后原子写入；失败时磁盘文件保持原样。"""
    path = _target_path(cfg, kind, name)
    _validate(kind, name, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    if kind == "product":
        cfg.reload_projects()
    return {
        "kind": kind,
        "name": name,
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
    }


def _chip_record(cfg: Config, chip_id: str) -> dict:
    """按 id / vendor/id 找已安装芯片；找不到时给出可选项。"""
    if not chip_id:
        raise ElabError("缺少 chip")
    for key, rec in cfg.all_chips.items():
        if rec.get("id") == chip_id or key == chip_id or key.endswith("/" + chip_id):
            return rec
    known = ", ".join(sorted({
        str(r.get("id") or "") for r in cfg.all_chips.values()
        if r.get("id")
    })) or "(无)"
    raise ElabError(f"未安装芯片 {chip_id}；已安装：{known}")


def _product_dir(cfg: Config) -> Path:
    root = Path(cfg.zephyr_root) / "kharon-manifest" / "products"
    sub = root / "kharon"
    return sub if sub.is_dir() else root


def _yaml_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_project_manifest(cfg: Config, *, name: str, chip_id: str, board: str,
                            app: str, target_id: str, close_on: str = "") -> str:
    """生成一份最小产品 manifest；所有路径都相对 ``ZEPHYR_ROOT``。"""
    from pathlib import Path
    from elab.topology import topology

    _safe_name(name)
    if not _SLUG_RE.match(name):
        raise ElabError("工程名只能是小写字母开头的 slug（a-z / 0-9 / _ / -，2–64 字符）")
    if not _SLUG_RE.match(target_id):
        raise ElabError("target id 只能是小写字母开头的 slug（a-z / 0-9 / _ / -）")

    chip = _chip_record(cfg, chip_id)
    chip_target = str(chip.get("target") or chip.get("id") or "")
    chip_board = chip_target.split("/")[0] if chip_target else ""
    if not chip_board:
        raise ElabError(f"芯片 {chip_id} 没有 target，无法反查板级真相")
    if board and board != chip_board:
        raise ElabError(
            f"板级真相不匹配：芯片 {chip_id} 的 target 是 {chip_target}，"
            f"对应板是 {chip_board}，不是 {board}")

    arch = topology(cfg)
    app_rec = next((a for a in arch["apps"] if a.get("name") == app), None)
    if app_rec is None:
        known = ", ".join(a["name"] for a in arch["apps"]) or "(无)"
        raise ElabError(f"未知 APP {app}；现有：{known}")

    zroot = Path(cfg.zephyr_root)
    app_root = Path(app_rec["root"])
    try:
        app_rel = app_root.relative_to(zroot / "applications")
    except ValueError as exc:
        raise ElabError(f"APP 不在 applications 下：{app_root}") from exc
    family = app_rel.parent
    build_rel = Path("build")
    if family != Path("."):
        build_rel = build_rel / family
    build_rel = build_rel / name / target_id

    pack = str(chip.get("_pack") or "zephyr/framework")
    root_ref = "${ZEPHYR_ROOT}/applications/" + app_rel.as_posix()
    work_ref = "${ZEPHYR_ROOT}/" + build_rel.as_posix()

    lines = [
        f"# {name} —— 由 Kharon 架构页“新建工程”向导生成",
        "# 芯片 / 板级真相 / APP 三层来自已安装芯片包与 topology；改之前先看架构页。",
        "",
        f"name: {name}",
        f"root: {root_ref}",
        f"pack: {pack}",
        f"chip: {chip_id}",
        "archetype: zephyr",
        "provider: zephyr/west@>=1 <2",
        "",
        "build:",
        "  kind: cmake",
        "  source: ${root}/CMakeLists.txt",
        "",
        "targets:",
        f"  - id: {target_id}",
        f"    chip: {chip_id}",
        "    build:",
        f"      work_dir: {work_ref}",
        "",
        "artifacts:",
        "  elf: ${work_dir}/zephyr/zephyr.elf",
        "  bin: ${work_dir}/zephyr/zephyr.bin",
        "  hex: ${work_dir}/zephyr/zephyr.hex",
        "  map: ${work_dir}/zephyr/zephyr.map",
        "",
        "debug:",
        f"  probe: {str((chip.get('debug') or {}).get('probe') or 'dap')}",
        "",
        "serial:",
        "  port: auto",
        "  baud: 115200",
        "  reconnect: { enabled: true, min_ms: 200, max_ms: 5000, max_attempts: 0 }",
        "",
        "monitor:",
    ]
    if close_on.strip():
        if "\n" in close_on or "\r" in close_on:
            raise ElabError("串口闭环关键字不能包含换行")
        lines += [
            "  close_on:",
            f"    - {{ kind: regex, pattern: {_yaml_quote(close_on.strip())} }}",
        ]
    lines += [
        "  fail_on:",
        "    - { kind: regex, pattern: \"FATAL ERROR|ASSERTION FAIL|Unhandled kernel exception\" }",
        "  idle_timeout_s: 30",
        "",
        f"matrix: {name}",
        "",
        "step_labels:",
        "  doctor: 工具链探活",
        "  build: 编译（west build）",
        "  report: 用量报告（ram/rom）",
        "  flash: 烧录（openocd + DAP-Link）",
        "  debug_verify: 断到 main",
        "  monitor: 串口闭环",
        "",
    ]
    text = "\n".join(lines)
    data = _yaml.load(text) or {}
    if not isinstance(data, dict) or data.get("name") != name:
        raise ElabError("生成的 manifest 校验失败")
    return text


def create_project(cfg: Config, body: dict) -> dict:
    """从架构页创建产品 manifest（可选 preview，不落盘）。

    客户端只提交 **chip / board / app / target** 四个语义选择；路径、芯片 target、
    work_dir 全部由后端从已安装包与 topology 反查，避免 UI 拼出第二套事实。
    """
    name = str(body.get("name") or "").strip()
    chip_id = str(body.get("chip") or "").strip()
    board = str(body.get("board") or "").strip()
    app = str(body.get("app") or "").strip()
    target_id = str(body.get("target") or "default").strip() or "default"
    close_on = str(body.get("close_on") or "")
    preview = bool(body.get("preview"))

    path = _product_dir(cfg) / f"{_safe_name(name)}.yaml"
    if path.exists():
        raise ElabError(f"产品已存在：{path.name}（换一个工程名，或先在架构页编辑现有 manifest）")

    text = render_project_manifest(cfg, name=name, chip_id=chip_id, board=board,
                                   app=app, target_id=target_id, close_on=close_on)
    if preview:
        return {"name": name, "target": target_id, "path": str(path),
                "exists": False, "written": False, "text": text}

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    cfg.reload_projects()
    return {"name": name, "target": target_id, "path": str(path),
            "exists": True, "written": True, "text": text}

