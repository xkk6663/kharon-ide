"""cockpit.api.routes.project —— 工程接入 / 选目录。"""

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


from cockpit.api.routes import pkg_actions, web  # noqa: F401

def handle_post(h, u, qs):
    if u.path == "/api/adapt":
        # 一键适配（M5.6）：probe 只读在前，write 显式第二段。
        return pkg_actions.adapt(h, h._body())
    if u.path == "/api/pick-folder":
        # 原生「选择文件夹」对话框：浏览器沙箱拿不到本地绝对路径，
        # 由同机运行的内核代开系统目录选择框（适配路径的唯一入口）。
        return pick_folder(h, h._body())
    if u.path == "/api/project":
        # 工程接入文件的命名与移除（左侧工程轨的「⋯」菜单）。
        return project_admin(h, h._body())
    return False


def pick_folder(h, body: dict):
    """``POST /api/pick-folder`` —— 打开**系统原生**「选择文件夹」对话框。

    为什么必须由内核代开：浏览器安全沙箱下 ``<input>`` 拿不到本地目录的
    绝对路径，而 ``/api/adapt`` 硬性要求绝对路径（相对路径会让"适配到哪"
    取决于服务进程的 cwd）。内核与用户在同一台机器上，是最合适的中介。

    返回 ``{"path": "C:/..."}`` 或 ``{"cancelled": true}``；tkinter 不可用
    （精简版 Python / 无桌面会话）时抛 ElabError，前端回落到手工输入。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:                           # noqa: BLE001
        raise ElabError(
            f"当前 Python 环境没有可用的 tkinter，无法打开目录选择框"
            f"（{exc}）—— 请手工输入工程绝对路径") from exc
    initial = str(body.get("initial") or "").strip()
    # Tk 必须在创建窗口的线程里使用；HTTP 处理器线程即可（单次、独占）。
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:                                  # noqa: BLE001
        pass
    try:
        picked = filedialog.askdirectory(
            parent=root,
            title="选择工程根目录（含 CMakeLists.txt）",
            initialdir=initial or None,
            mustexist=True,
        )
    finally:
        try:
            root.destroy()
        except Exception:                              # noqa: BLE001
            pass
    if not picked:
        return h._json({"cancelled": True}, 200)
    return h._json({"path": Path(picked).as_posix()}, 200)


def project_admin(h, body: dict):
    """``POST /api/project`` —— 左侧工程轨的「⋯」菜单。

        action=rename     {name, new_name}  改 projects/<name>.yaml 的基名与 name 行
        action=remove     {name}            先留 .bak 再删接入文件
        action=set_probe  {name, probe}      只改 debug.probe（保留注释）

    ★ 只动接入层（`projects/*.yaml`），业务工程零改动（guard 快照不受影响）。
    ★ 成功后**必须**重载 Config —— 否则工程轨仍显示旧卡片（C29 同一教训）。
    ★ 有闭环在跑 → 409：改名/删除会让在途 run 的 project 键失效。
    ★ 调试会话打开 → 409：避免面板显示新探针、当前会话仍用旧探针。
    """
    action = str(body.get("action") or "").strip()
    name = str(body.get("name") or "").strip()
    if action not in ("rename", "remove", "set_probe"):
        raise ElabError(f"action 必须是 rename / remove / set_probe：{action!r}")
    if not name:
        raise ElabError("缺少 name")
    active = h.mgr.list_active()
    if active:
        raise ElabError(
            f"有闭环正在跑（run={active[0].get('run')}）—— 等它结束再改名/移除/切换探针")

    if action == "rename":
        res = project_admin_mod.rename(h.cfg, name, str(body.get("new_name") or ""))
    elif action == "remove":
        res = project_admin_mod.remove(h.cfg, name)
    else:
        sess = h.dbg.get()
        if sess is not None and sess.state not in ("closed", "error"):
            raise dbgsess_mod.DebugError(
                "debug-busy", f"调试会话打开中（项目 {sess.plan.name}）—— 先关闭会话再切换探针",
                "点调试工作区「关闭」，或 DELETE /api/debug/session")
        res = project_admin_mod.set_probe(
            h.cfg, name, str(body.get("probe") or ""))

    cfg2 = Config(h.cfg.root)
    type(h).cfg = cfg2
    h.mgr.cfg = cfg2
    return h._json({**res, "action": action})
