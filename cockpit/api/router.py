"""cockpit.api.router —— HTTP 路由分发（P2 拆分自 server.py）。

按领域顺序调用各 route 模块：返回 ``False`` 表示该领域没有匹配到，
其余返回值（含 ``None``，即已写出响应）都视为「已处理」。
"""

from __future__ import annotations

from cockpit.api.routes import debug, project, runs, serial, system, workspace, web


def handle_get(h, path, qs) -> bool:
    for fn in (system.handle_get, workspace.handle_get, runs.handle_get,
               debug.handle_get, runs.handle_events, serial.handle_get):
        if fn(h, path, qs) is not False:
            return True
    return False


def handle_post(h, u, qs) -> bool:
    for fn in (debug.handle_post, runs.handle_post, serial.handle_post,
               workspace.handle_post, project.handle_post, system.handle_post):
        if fn(h, u, qs) is not False:
            return True
    return False


def handle_delete(h, u, qs) -> bool:
    return debug.handle_delete(h, u, qs) is not False


def static_fallback(h, path):
    return web.static(h, path)
