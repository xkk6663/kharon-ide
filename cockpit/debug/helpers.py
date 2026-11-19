"""cockpit.debug.helpers —— 调试路由的通用小工具。
"""

from __future__ import annotations

import json
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



def _bool_or_none(value):
    """JSON body 里的三态布尔：``True`` / ``False`` / ``None``（未给）。"""
    if value is None:
        return None
    return bool(value)


def _qs_int(qs: dict, key: str, default: int | None = 0) -> int | None:
    raw = (qs.get(key) or [""])[0]
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ElabError(f"{key} 必须是整数：{raw!r}")


def _qs_dsess(qs: dict) -> str:
    """调试会话 id（**不是**聊天 sessionId；命名冲突见 §3.4）。"""
    return (qs.get("debug_session") or [""])[0]


# ══════════════════════════════════════════════════════════════════
#  调试会话管理（单会话 + 租约 + 互斥 + 审计）§7.7
# ══════════════════════════════════════════════════════════════════
def _debug_audit_path(cfg: Config) -> Path:
    return Path(cfg.root) / ".work" / "debug-audit.jsonl"


def _debug_audit_tail(cfg: Config, limit: int = 100) -> dict:
    """审计尾读（``?limit=``）；坏行跳过，只回最近 N 条。"""
    path = _debug_audit_path(cfg)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out = []
    for line in lines[-limit:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return {"records": out, "count": len(out), "requested": limit,
            "path": to_fwd(path)}


def _debug_audit(cfg: Config, rec: dict) -> None:
    """审计留档（独立文件；**绝不**写进 state.json —— C14/C15/C28）。"""
    try:
        path = _debug_audit_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **rec}, ensure_ascii=False) + "\n")
        # 只保留最近 DEBUG_AUDIT_MAX 行（调试会话不是 run 归档，不无界增长）
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > DEBUG_AUDIT_MAX:
            path.write_text("\n".join(lines[-DEBUG_AUDIT_MAX:]) + "\n", encoding="utf-8")
    except OSError:
        pass


__all__ = [n for n in list(globals()) if not n.startswith("__")]
