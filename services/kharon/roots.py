"""kharon.roots —— IDE 根 / Zephyr 工作区根的单一解析入口。

目标布局::

    ELAB/kharon/
    ├─ kharon-ide/                 # IDE_ROOT
    └─ zephyr-workspace/           # ZEPHYR_ROOT（west topdir）

旧布局仍兼容：没有同级 ``zephyr-workspace/.west/config`` 时，ZEPHYR_ROOT
回落到 ``IDE_ROOT/west``。这样 P1 双根脚手架可以先上线，P2 再搬目录。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 本文件位于 ``<IDE_ROOT>/services/kharon/roots.py``
DEFAULT_IDE_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _absolute(value: str, base: Path) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def ide_root() -> Path:
    """IDE 根：显式 ``IDE_ROOT`` > 兼容 ``ELAB_ROOT`` > 本文件位置。"""
    raw = _env_path("IDE_ROOT") or _env_path("ELAB_ROOT")
    if raw:
        return _absolute(raw, DEFAULT_IDE_ROOT)
    return DEFAULT_IDE_ROOT.resolve()


def zephyr_root() -> Path:
    """Zephyr 工作区根（west topdir）。

    优先级：
    1. ``ZEPHYR_ROOT`` / ``ELAB_ZEPHYR_ROOT`` 显式覆盖；
    2. 目标布局：``IDE_ROOT/../zephyr-workspace/.west/config``；
    3. 旧布局：``IDE_ROOT/west``。
    """
    raw = _env_path("ZEPHYR_ROOT") or _env_path("ELAB_ZEPHYR_ROOT")
    if raw:
        return _absolute(raw, ide_root())

    target = ide_root().parent / "zephyr-workspace"
    if (target / ".west" / "config").is_file():
        return target.resolve()
    return (ide_root() / "west").resolve()


def ensure_env() -> tuple[Path, Path]:
    """把两个根写进环境变量，供子进程 / provider 模板使用。"""
    ide = ide_root()
    zephyr = zephyr_root()
    os.environ.setdefault("IDE_ROOT", str(ide))
    os.environ.setdefault("ELAB_ROOT", str(ide))
    os.environ.setdefault("ZEPHYR_ROOT", str(zephyr))
    return ide, zephyr
