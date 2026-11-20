"""cockpit.platform.modes —— 装配清单（profile）读取。

本仓只有一份产品装配清单；这里不再提供 classic ⇄ Zephyr 的进程级模式切换。
"""

from __future__ import annotations

import mimetypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parents[1]
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "services") not in sys.path:
    sys.path.insert(0, str(_ROOT / "services"))

from elab import adapt as adapt_mod                      # noqa: E402
from elab import project_admin as project_admin_mod      # noqa: E402
from elab import flash as flash_mod                      # noqa: E402
from elab import debugsession as dbgsess_mod           # noqa: E402
from elab.plan import plan_for                          # noqa: E402
from elab.flow import KERNEL_PLANS, STEP_META           # noqa: E402  流程清单（唯一事实源）
from elab import run as run_mod                      # noqa: E402
from elab._winproc import adopt as adopt_proc        # noqa: E402
from elab import provider as provider_mod            # noqa: E402
from elab import zephyr_meta as zephyr_meta_mod      # noqa: E402
from elab import westws as westws_mod                # noqa: E402
from elab import serialcodec as serialcodec_mod      # noqa: E402
from elab import serialconsole as serialconsole_mod  # noqa: E402
from elab import serialcontrol as serialcontrol_mod  # noqa: E402
from elab import logdecode as ld                      # noqa: E402
from elab import serialframes as sf                   # noqa: E402
from elab import framelearn as framelearn_mod         # noqa: E402
from elab import serialmon as serialmon_mod          # noqa: E402
from elab import serialport as serialport_mod        # noqa: E402
from elab import serialtransport as serialtransport_mod  # noqa: E402
from elab import serialterm as serialterm_mod        # noqa: E402
from elab import serialwidgets as serialwidgets_mod  # noqa: E402
from elab import modbus as modbus_mod                # noqa: E402
from elab import modbusmaster as modbusmaster_mod    # noqa: E402
from elab.config import Config, ElabError, Host, to_fwd     # noqa: E402
from elab.kernel import events as ev_mod              # noqa: E402
from elab import pkg as pkg_mod                       # noqa: E402
from elab import setup as setup_mod                   # noqa: E402
from elab import _yaml as yaml_mod                    # noqa: E402


from cockpit.core import *  # noqa: F401,F403


def _profiles_dir(cfg: Config) -> Path:
    """装配清单目录（``<ELAB_ROOT>/cockpit/profiles``）。"""
    return Path(cfg.root) / "cockpit" / "profiles"


def _profile_files(cfg: Config) -> list[Path]:
    """全部装配清单，按文件名排序（排序 = 模式表的呈现顺序，稳定可预期）。"""
    d = _profiles_dir(cfg)
    return sorted(d.glob("*.yaml")) if d.is_dir() else []


def _read_profile(path: Path) -> dict:
    """读一份装配清单；**坏文件返回空 dict 而不是抛**。

    一份写坏的 yaml 只该让它自己那个按钮不可用，不该把整张模式表打挂 ——
    模式表是"选哪个工作区"的唯一入口，它挂了就一个都切不了。
    """
    try:
        return yaml_mod.load_file(path) or {}
    except Exception:                                 # noqa: BLE001
        return {}


def _active_profile_name(cfg: Config, files: list[Path]) -> str:
    """当前生效的装配清单（文件名 stem）。

    优先级：``ELAB_PROFILE``（启动前可设）→ ``<host>: profile`` → 排序第一份。

    ★ 本仓只有一份 `kharon.yaml`，不再有 `web`/classic 回落；运行期也没有切换端点。
    """
    if not files:
        return ""
    # ★ 两个来源要**逐个试**，不能先合成一个 wanted 再校验：合成之后
    #   "ELAB_PROFILE 指向一个已被删掉的名字"会直接跳到第一份，把 host 里
    #   那份显式声明白扔掉。
    def _pick(name: str) -> str:
        return name if any(p.stem == name for p in files) else ""

    picked = _pick((os.environ.get("ELAB_PROFILE") or "").strip())
    if picked:
        return picked
    try:
        picked = _pick(str(((cfg.host or {}).get("profile") or "")).strip())
    except ElabError:
        picked = ""
    return picked or files[0].stem


def _mode_host_path(cfg: Config, data: dict) -> str:
    """装配清单的 ``host:`` → 绝对路径（空串 = 只换装配、不换主机事实源）。"""
    raw = str(data.get("host") or "").strip()
    if not raw:
        return ""
    resolved = cfg.resolve(raw, {"ELAB_ROOT": to_fwd(cfg.root)})
    p = Path(resolved)
    if not p.is_absolute():
        p = Path(cfg.root) / p
    return to_fwd(p)


def _profile_summary(cfg: Config) -> dict | None:
    """读出驾驶舱 profile（R7 / P1-4 的最小装配）。

    文件位置：``<ELAB_ROOT>/cockpit/profiles/*.yaml``。激活顺序见
    :func:`_active_profile_name`；一份都没有时返回 None（前端回退到内置集合）。

    装配三件事 —— **左栏工程可见范围**、**右列工作区顺序**、**项目工具启停**：
    ``workspaces`` 为空 = 全部（内置三件套 + 工具页）；``tools.include``
    非空 = 只启用列出的工具，``tools.exclude`` 优先剔除。

    本仓只保留一份生效装配（`kharon.yaml`）；`available` 仅作诊断，
    不再给前端提供 classic ⇄ Zephyr 模式表，也没有运行期切换端点。
    """
    files = _profile_files(cfg)
    if not files:
        return None
    active = _active_profile_name(cfg, files)
    chosen = next((p for p in files if p.stem == active), files[0])
    data = _read_profile(chosen)
    tools = data.get("tools") or {}
    projs = data.get("projects") or {}
    return {
        "id": chosen.stem,
        "name": str(data.get("profile") or chosen.stem),
        "label": str(data.get("label") or chosen.stem),
        "desc": str(data.get("desc") or ""),
        "path": to_fwd(chosen),
        # 本清单自己的主机事实源（只读展示；本仓不再运行期切换）
        "host": _mode_host_path(cfg, data),
        "available": [p.stem for p in files],
        "workspaces": [str(x) for x in (data.get("workspaces") or []) if str(x)],
        "tools": {"include": [str(x) for x in (tools.get("include") or []) if str(x)],
                  "exclude": [str(x) for x in (tools.get("exclude") or []) if str(x)]},
        # ★ 工程可见范围（只剩一份 kharon.yaml）：
        #   不写这一节 = 全可见；
        #   `projects.archetype: [zephyr]` → 左栏只剩 Zephyr 工程。
        #   **不靠前端按名字猜**，也不在代码里判断厂商。
        "projects": {"archetype": [str(x) for x in (projs.get("archetype") or [])
                                   if str(x)]},
    }


__all__ = [n for n in list(globals()) if not n.startswith("__")]
