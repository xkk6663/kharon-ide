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
