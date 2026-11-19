"""cockpit.api.routes.debug —— 调试会话 API（GET+POST+DELETE）。"""

from __future__ import annotations

from __future__ import annotations

import json
import base64
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

_ROOT = Path(__file__).resolve()
for _anc in _ROOT.parents:
    if (_anc / "services" / "elab").is_dir():
        _ROOT = _anc
        break
_HERE = _ROOT / "cockpit"
for _p in (str(_ROOT), str(_ROOT / "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cockpit.core import *  # noqa: F401,F403
from cockpit.run.manager import *  # noqa: F401,F403
from cockpit.platform.summary import *  # noqa: F401,F403
from cockpit.platform.modes import *  # noqa: F401,F403
from cockpit.platform.state import *  # noqa: F401,F403
from cockpit.platform.jobs import *  # noqa: F401,F403
from cockpit.debug.helpers import *  # noqa: F401,F403
from cockpit.debug.manager import *  # noqa: F401,F403


from cockpit.api.routes import sse  # noqa: F401

def handle_get(h, path, qs):
    if path == "/api/debug/session":
        return h._json(h.dbg.snapshot(_qs_dsess(qs)))
    if path == "/api/debug/events":
        return sse.sse_debug(h, _qs_dsess(qs))
    if path == "/api/debug/stack":
        thread = _qs_int(qs, "thread", None)
        return h._json(h.dbg.require(_qs_dsess(qs)).stack(thread=thread))
    if path == "/api/debug/variables":
        frame = _qs_int(qs, "frame", 0) or 0
        scope = (qs.get("scope") or ["locals"])[0]
        var = (qs.get("var") or [""])[0]
        return h._json(h.dbg.require(_qs_dsess(qs)).variables(
            frame=frame, scope=scope, var=var))
    if path == "/api/debug/registers":
        group = (qs.get("group") or ["core"])[0]
        fmt = (qs.get("format") or ["natural"])[0]
        return h._json(h.dbg.require(
            (qs.get("debug_session") or [""])[0]).registers(group=group, fmt=fmt))
    if path == "/api/debug/source":
        # 源码页签（P3-A）：路径受 plan.source_dir 沙箱约束
        file = (qs.get("file") or [""])[0]
        start = _qs_int(qs, "start", 1) or 1
        count = _qs_int(qs, "count", 120) or 120
        return h._json(h.dbg.require(
            (qs.get("debug_session") or [""])[0]).source(
                file=file, start=start, count=count))
    if path == "/api/debug/breakpoints":
        sess = h.dbg.require(_qs_dsess(qs))
        return h._json({"breakpoints": sess.breakpoints(),
                           "rev": sess.store_rev(),
                           "allow_write": h.dbg.allow_write(_qs_dsess(qs))})
    if path == "/api/debug/watch":
        return h._json(h.dbg.require(_qs_dsess(qs)).watch())
    if path == "/api/debug/audit":
        limit = _qs_int(qs, "limit", 100) or 100
        limit = max(1, min(limit, DEBUG_AUDIT_MAX))
        return h._json(_debug_audit_tail(h.cfg, limit))
    if path == "/api/debug/peripherals":
        p = (qs.get("path") or [""])[0]
        read = (qs.get("read") or ["0"])[0] in ("1", "true", "yes")
        return h._json(h.dbg.require(_qs_dsess(qs)).peripherals(path=p, read=read))
    if path == "/api/debug/rtos":
        return h._json(h.dbg.require(_qs_dsess(qs)).rtos())
    if path == "/api/debug/zephyr":
        return h._json(h.dbg.require(_qs_dsess(qs)).zephyr())
    if path == "/api/debug/memory":
        addr = (qs.get("addr") or [""])[0]
        count = _qs_int(qs, "count", 256) or 256
        return h._json(h.dbg.require(_qs_dsess(qs)).memory(addr=addr, count=count))
    if path == "/api/debug/disasm":
        addr = (qs.get("addr") or [""])[0]
        count = _qs_int(qs, "count", 32) or 32
        mode = _qs_int(qs, "mode", 2) or 2
        return h._json(h.dbg.require(_qs_dsess(qs)).disasm(
            addr=addr, count=count, mode=mode))
    return False


def handle_post(h, u, qs):
    if u.path == "/api/debug/session":
        body = h._body()
        return h._json(h.dbg.open(
            project=str(body.get("project") or ""),
            actor=str(body.get("actor") or "human"),
            options=body.get("options") or {},
            lease_s=int(body.get("lease_s") or 0),
            allow_write=bool(body.get("allow_write")),
        ))
    if u.path == "/api/debug/control":
        body = h._body()
        return h._json(h.dbg.require(
            str(body.get("debug_session") or "")).control(
            str(body.get("action") or ""),
            addr=str(body.get("addr") or ""),
            confirm=bool(body.get("confirm")),
            file=str(body.get("file") or ""),
            line=int(body.get("line") or 0),
        ))
    if u.path == "/api/debug/registers":
        # 按组批量写（§7.2）：整批校验 → 逐项 set → 一条审计
        body = h._body()
        dsid = str(body.get("debug_session") or "")
        h.dbg.require_write(confirm=bool(body.get("confirm")), debug_session=dsid)
        sess = h.dbg.require(dsid)
        out = sess.write_registers(items=body.get("items") or [])
        _debug_audit(h.cfg, {"action": "write", "target": "register-group",
                                "session": sess.session_id,
                                "project": sess.plan.name,
                                "actor": sess.owner,
                                "items": [{"name": a["name"], "before": a["before"],
                                           "after": a["after"]}
                                          for a in out.get("applied") or []],
                                "failed": out.get("failed")})
        return h._json(out)
    if u.path == "/api/debug/breakpoints":
        body = h._body()
        op = str(body.get("op") or "list")
        dsid = str(body.get("debug_session") or "")
        sess = h.dbg.require(dsid)
        rev = (None if body.get("rev") is None else int(body.get("rev")))
        if op == "add":
            out = sess.breakpoint_add(
                kind=str(body.get("kind") or "func"),
                location=str(body.get("location") or ""),
                condition=str(body.get("condition") or ""),
                hardware=bool(body.get("hardware")),
                temporary=bool(body.get("temporary")),
                rev=rev,
            )
            _debug_audit(h.cfg, {"action": "breakpoint-add",
                                    "session": sess.session_id,
                                    "project": sess.plan.name,
                                    "actor": sess.owner,
                                    "location": body.get("location") or ""})
            return h._json(out)
        if op == "update":
            out = sess.breakpoint_update(
                bp_id=str(body.get("id") or ""),
                condition=(None if "condition" not in body
                           else str(body.get("condition") or "")),
                enabled=(None if "enabled" not in body else bool(body.get("enabled"))),
                rev=rev,
            )
            return h._json(out)
        if op == "toggle":
            return h._json(sess.breakpoint_toggle(
                bp_id=str(body.get("id") or ""), rev=rev))
        if op == "remove":
            out = sess.breakpoint_remove(bp_id=str(body.get("id") or ""), rev=rev)
            _debug_audit(h.cfg, {"action": "breakpoint-remove",
                                    "session": sess.session_id,
                                    "project": sess.plan.name,
                                    "actor": sess.owner,
                                    "id": body.get("id") or ""})
            return h._json(out)
        if op == "list":
            return h._json({"breakpoints": sess.breakpoints(),
                               "rev": sess.store_rev(),
                               "allow_write": h.dbg.allow_write(dsid)})
        raise ElabError(f"未知断点操作：{op!r}")
    if u.path == "/api/debug/watch":
        body = h._body()
        op = str(body.get("op") or "list")
        sess = h.dbg.require(str(body.get("debug_session") or ""))
        rev = (None if body.get("rev") is None else int(body.get("rev")))
        if op == "add":
            return h._json(sess.watch_add(
                expr=str(body.get("expr") or ""),
                fmt=str(body.get("format") or "natural"), rev=rev))
        if op == "update":
            return h._json(sess.watch_update(
                wid=str(body.get("id") or ""),
                expr=(None if "expr" not in body else str(body.get("expr") or "")),
                fmt=(None if "format" not in body else str(body.get("format") or "")),
                rev=rev))
        if op == "remove":
            return h._json(sess.watch_remove(
                wid=str(body.get("id") or ""), rev=rev))
        if op == "list":
            return h._json(sess.watch())
        raise ElabError(f"未知监视操作：{op!r}")
    if u.path == "/api/debug/evaluate":
        body = h._body()
        return h._json(h.dbg.require(
            str(body.get("debug_session") or "")).evaluate(
            expr=str(body.get("expr") or ""),
            frame=(None if body.get("frame") is None else int(body.get("frame"))),
        ))
    if u.path == "/api/debug/write":
        body = h._body()
        target = str(body.get("target") or "")
        dsid = str(body.get("debug_session") or "")
        h.dbg.require_write(confirm=bool(body.get("confirm")), debug_session=dsid)
        sess = h.dbg.require(dsid)
        if target == "memory":
            out = sess.write_memory(
                addr=str(body.get("addr") or ""),
                value=str(body.get("value") or ""))
            _debug_audit(h.cfg, {"action": "write", "target": "memory",
                                    "session": sess.session_id,
                                    "project": sess.plan.name,
                                    "actor": sess.owner,
                                    "addr": body.get("addr") or "",
                                    "value": body.get("value") or ""})
            return h._json(out)
        if target == "register":
            out = sess.write_register(
                name=str(body.get("name") or ""),
                value=str(body.get("value") or ""))
            _debug_audit(h.cfg, {"action": "write", "target": "register",
                                    "session": sess.session_id,
                                    "project": sess.plan.name,
                                    "actor": sess.owner,
                                    "name": body.get("name") or "",
                                    "value": body.get("value") or ""})
            return h._json(out)
        if target == "var":
            # 按变量名写：名字交给 gdb 解析地址与类型（不自研 DWARF）。
            # 闸门与 memory/register 完全一致 —— 写就是写，不因为"更好用"
            # 而降级校验。返回 before/after 让调用方能自证生效。
            out = sess.write_var(
                name=str(body.get("name") or ""),
                value=str(body.get("value") or ""))
            _debug_audit(h.cfg, {"action": "write", "target": "var",
                                    "session": sess.session_id,
                                    "project": sess.plan.name,
                                    "actor": sess.owner,
                                    "name": body.get("name") or "",
                                    "value": body.get("value") or "",
                                    "after": out.get("after", "")})
            return h._json(out)
        raise ElabError(f"未知写目标：{target!r}")
    return False


def handle_delete(h, u, qs):                               # noqa: N802
    """``DELETE /api/debug/session`` —— 关闭会话并恢复运行（D8/C36）。"""
    u = urlparse(h.path)
    try:
        if u.path != "/api/debug/session":
            return h._err(404, f"未知 API：{u.path}")
        body = h._body()
        resume = bool(body.get("resume", True))
        return h._json(h.dbg.close(
            debug_session=str(body.get("debug_session") or ""),
            reason=str(body.get("reason") or "user-closed"),
            resume=resume, actor=str(body.get("actor") or "human"),
        ))
    except dbgsess_mod.DebugError as exc:
        return h._debug_err(exc)
    except ElabError as exc:
        return h._err(400, str(exc))
    except Exception as exc:                       # noqa: BLE001
        return h._fail(exc)
