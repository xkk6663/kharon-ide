"""cockpit.api.routes.workspace —— 工作区 / west / zephyr / packs（GET）。"""

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

from elab import westexec as westexec_mod  # noqa: E402

#: 需要写权限的 west 动作（内核侧二次校验，不能只靠工具层门控）。
_WEST_WRITE_ACTIONS = {"update", "init"}

#: ``action=meta`` 的 kind → zephyr_meta 函数（全部只读，输入来自构建产物）。
_WEST_META_KINDS = ("runners", "kconfig", "devicetree", "footprint")


def handle_get(h, path, qs):
    if path == "/api/workspace":
        # 工作区事实（「三形态 + 双拓扑」的读口，2026-11-18）：
        # ★ 默认**只读文件系统**（不跑 west）—— L1「看懂」必须零依赖；
        #   要模块/板子清单时加 `?modules=1`（那时才跑 west list / boards）。
        # 与 /api/west 的分工：/api/west 是**面板**快照（含模块/板子/构建），
        # /api/workspace 是**语义**视图（拓扑/形态/去哪儿构建）。
        return h._json(_workspace_view(
            h.cfg,
            app=(qs.get("app") or qs.get("project") or [""])[0],
            with_modules=(qs.get("modules") or ["0"])[0].lower()
                         in ("1", "true", "yes"),
            refresh=(qs.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes"),
        ))
    if path == "/api/west":
        # west 工作区快照：模块树 / board 清单 / SDK / 最近构建事实（只读）
        return h._json(westws_mod.snapshot(
            h.cfg,
            q=(qs.get("q") or [""])[0],
            refresh=(qs.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes"),
        ))
    if path.startswith("/api/zephyr/"):
        # Zephyr 构建元数据的只读投影（业务事实仍只来自构建产物）：
        #   runners    → zephyr/runners.yaml（探针/GDB/OpenOCD 的唯一事实源）
        #   kconfig    → zephyr/.config（含未启用项）
        #   devicetree → zephyr/zephyr.dts（节点 + 来源文件行号）
        #   footprint  → ram.json / rom.json（符号级用量，供旭日图）
        kind = path[len("/api/zephyr/"):]
        project = (qs.get("project") or [""])[0]
        wd = _zephyr_work_dir(h.cfg, project)
        if not wd:
            return h._err(404, f"没有这个项目，或它未声明构建目录：{project!r}")
        if kind == "runners":
            return h._json(zephyr_meta_mod.runners(wd))
        if kind == "kconfig":
            return h._json(zephyr_meta_mod.kconfig(wd))
        if kind == "devicetree":
            return h._json(zephyr_meta_mod.devicetree(wd))
        if kind == "footprint":
            try:
                depth = max(1, min(6, int((qs.get("depth") or ["3"])[0])))
            except ValueError:
                depth = 3
            return h._json(zephyr_meta_mod.footprint(wd, depth=depth))
        return h._err(404, f"未知的 zephyr 元数据：{kind!r}")
    if path == "/api/packs":
        # 适配包 / 社区市场投影（内核事实源；工具层只渲染）。
        # ?market=0 可跳过市场索引读取（离线/慢网络）；?q= 只返回命中的市场条目。
        with_market = (qs.get("market") or ["1"])[0].lower() not in ("0", "false", "no")
        q = (qs.get("q") or [""])[0]
        return h._json(pkg_mod.status(h.cfg, include_market=with_market,
                                         query=q))
    if path == "/api/pkg/job":
        # 包操作任务快照（202 后前端 500ms 轮询这里）。
        job_id = (qs.get("job") or [""])[0]
        snap = h.pkg_jobs.snapshot(job_id)
        if snap is None:
            return h._err(404, "没有这个包操作任务（或已结束很久）")
        return h._json(snap)
    return False


def handle_post(h, u, qs):
    if u.path == "/api/west":
        # west 的受控执行口（agent ``elab_west`` 的内核半）：
        #   status/list/boards/meta → 只读；update/init → 写，双重门控。
        return west_action(h, h._body())
    return False


def _num(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def west_action(h, body: dict):
    action = str(body.get("action") or "").strip()

    if action == "status":
        return h._json(westexec_mod.status(
            h.cfg, app_dir=str(body.get("app") or "")))

    if action in ("list", "boards"):
        return h._json(westexec_mod.read_snapshot(
            h.cfg, action,
            q=str(body.get("q") or ""),
            refresh=body.get("refresh") is True,
            limit=_num(body.get("limit"), 200),
        ))

    if action == "meta":
        kind = str(body.get("kind") or "").strip()
        project = str(body.get("project") or "").strip()
        if kind not in _WEST_META_KINDS:
            raise ElabError(
                f"meta 的 kind 必须是 {'/'.join(_WEST_META_KINDS)}，收到 {kind!r}")
        wd = _zephyr_work_dir(h.cfg, project)
        if not wd:
            raise ElabError(f"没有这个工程，或它未声明构建目录：{project!r}")
        if kind == "footprint":
            depth = max(1, min(6, _num(body.get("depth"), 3)))
            return h._json(zephyr_meta_mod.footprint(wd, depth=depth))
        return h._json(getattr(zephyr_meta_mod, kind)(wd))

    if action in _WEST_WRITE_ACTIONS:
        # ★ 内核二次校验：工具层已挡一次，这里再挡一次 —— 直接 curl 也得过两道闸。
        if body.get("allow_agent_write") is not True:
            raise ElabError("west 写动作需要 allow_agent_write=true（用户须显式开启）")
        if body.get("confirm") is not True:
            raise ElabError(f"west {action} 会改工作区文件，必须 confirm=true")
        timeout_s = _num(body.get("timeout_s"), 0)
        if action == "update":
            return h._json(westexec_mod.update(
                h.cfg, modules=body.get("modules"), timeout_s=timeout_s))
        return h._json(westexec_mod.init(
            h.cfg,
            directory=str(body.get("dir") or ""),
            manifest=str(body.get("manifest") or "zephyr"),
            timeout_s=timeout_s,
        ))

    raise ElabError(
        f"未知的 west action：{action!r}（status/list/boards/meta/update/init）")
