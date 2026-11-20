"""cockpit.api.routes.runs —— run 快照 / 计划 / 事件流 / 启停。"""

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


from cockpit.api.routes import sse, pkg_actions  # noqa: F401

def _safe_run_id(run_id: str) -> bool:
    """run id 只允许单段文件名（它直接拼进 ``.work/runs/<id>.jsonl``）。"""
    return bool(run_id) and not any(ch in run_id for ch in ("/", "\\", ".."))


def _export_markdown(h, run_id: str):
    """返回 Markdown 文本；出错时已写响应并返回 ``None``。"""
    if not _safe_run_id(run_id):
        h._err(400, f"非法 run id：{run_id}")
        return None
    try:
        return ev_mod.export_markdown(h.mgr.runs_dir, run_id)
    except FileNotFoundError as exc:
        h._err(404, str(exc))
        return None


def handle_get(h, path, qs):
    if path == "/api/runs":
        return h._json({"runs": ev_mod.list_runs(h.mgr.runs_dir),
                           "active": h.mgr.list_active()})
    if path == "/api/runs/export":
        # T7：把单 run 的事件流导出为 Markdown 证据（每行结论带 #seq）。
        # 默认 inline 展示；?download=1 触发浏览器保存。
        run_id = (qs.get("run") or [""])[0]
        md = _export_markdown(h, run_id)
        if md is None:
            return True
        extra = {}
        if (qs.get("download") or [""])[0] not in ("", "0"):
            extra["Content-Disposition"] = f'attachment; filename="{run_id}.md"'
        h._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                extra=extra)
        return True
    if path == "/api/runs/active":
        # ★ 轻量端点：**不扫盘**，只读进程内登记簿（+ 首行回读 run/start）。
        #   给「agent 在别的工程起跑了吗」这种 2s 轮询用。
        #   历史 run 的全量扫描（含 proc 文件尺寸）留给 /api/runs，
        #   那条只在切工程时调一次 —— 否则轮询开销随历史条数线性增长
        #   （实测 800 条 2 s > 2 s 轮询周期，会自己把自己拖满）。
        return h._json({"active": h.mgr.list_active()})
    if path == "/api/plan":
        return h._json(_plan_preview(h.cfg, qs))
    return False


def handle_events(h, path, qs):
    if path == "/api/events":
        return sse.sse(h, qs)
    if path == "/api/run-events":
        run_id = (qs.get("run") or [""])[0]
        after = int((qs.get("from") or ["0"])[0])
        return h._json({"events": ev_mod.read_run(h.mgr.runs_dir, run_id, after_seq=after)})
    return False


def handle_post(h, u, qs):
    if u.path == "/api/runs/export":
        # T7 落盘：默认 ``<runs_dir>/../exports/<run>.md``；显式 dest 必须留在
        # ELAB_ROOT 内（驾驶舱不替用户往仓库外写文件）。
        body = h._body()
        run_id = body.get("run") or ""
        md = _export_markdown(h, run_id)
        if md is None:
            return True
        dest = body.get("dest")
        root = Path(h.cfg.root).resolve()
        if dest:
            target = Path(str(dest))
            target = (target if target.is_absolute() else root / target).resolve()
            if target != root and root not in target.parents:
                return h._err(400, "dest 必须位于 ELAB_ROOT 内")
        else:
            target = (Path(h.mgr.runs_dir).parent / "exports" / f"{run_id}.md").resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md, encoding="utf-8")
        return h._json({"ok": True, "path": str(target),
                        "bytes": len(md.encode("utf-8"))}, 200)
    if u.path == "/api/runs/prune":

        # 盘点 #11：历史 run 清理入口（CLI `elab runs prune` 同一份实现）。
        # ★ 与 CLI 同源（ev_mod.prune_runs），不存在"面板删的和 CLI 删的
        #   规则不一样"。running 的 run 永不删（函数内保护）。
        body = h._body()
        # 缺省与 CLI 一致（keep=50 / days=30）；显式 days=0 关时间闸
        keep = 50 if body.get("keep") is None else int(body["keep"])
        days = 30.0 if body.get("days") is None else float(body["days"])
        return h._json(ev_mod.prune_runs(
            h.mgr.runs_dir, keep=keep, days=days,
            dry_run=bool(body.get("dry_run"))), 200)
    if u.path == "/api/run":
        body = h._body()
        project = body.get("project") or ""
        if not project:
            return h._err(400, "缺少 project")
        target = str(body.get("target") or "").strip() or None
        if target:
            h.cfg.select_target(project, target)
        else:
            # 过渡期：面板尚未提供 target 切换时，按卡片展示的第一个 target 起跑；
            # CLI 仍然要求显式 --target，只有驾驶舱沿用自己展示的默认。
            raw = h.cfg.projects.get(project) or {}
            raw_targets = h.cfg.targets_of(raw)
            if len(raw_targets) > 1:
                target = str(raw_targets[0]["id"])
                h.cfg.select_target(project, target)
        # ★ 安装/卸载适配包期间**冻结闭环**：包操作会改芯片数据与启用状态，
        #   此刻起 run 会读到半成品配置。与包操作里的反向守卫成对出现。
        busy_job = h.pkg_jobs.current()
        if (busy_job is not None
                and busy_job.get("state") in ("queued", "running")
                # 市场遍历只写 .work 缓存，不改芯片数据 → 不冻结闭环
                and busy_job.get("action") != "market"):
            return h._err(409, (
                f"适配包操作进行中（{busy_job.get('action')} {busy_job.get('name')}，"
                f"阶段：{busy_job.get('stage')}）—— 闭环已冻结，等它完成后重试"
                f"（约束：安装期间冻结闭环）"))
        # ★ 监视会话 ↔ 闭环（N5 的驾驶舱侧）：不再让用户手动关监视 ——
        #   改为**自动让位 + run 结束回归**（M2 §4.3.6）。互斥仍然成立：
        #   会话先挂起并释放 IO，run 拿到口；结束后按策略恢复。
        want_steps = set(body.get("steps") or run_mod.DEFAULT_STEPS)
        yielded = bool(want_steps & _SERIAL_STEPS)
        if yielded:
            policy = _resume_policy(h.cfg, project,
                                    body.get("resume_after_run"))
            _serial_yield_begin(h.cfg, project, policy=policy)
        try:
            res = h.mgr.spawn(
                project=project,
                steps=body.get("steps"),
                clean=bool(body.get("clean")),
                jobs=body.get("jobs"),
                verbose=bool(body.get("verbose")),
                target=target,
                actor=body.get("actor") or "human",
                on_finish=((lambda ar, rc: _serial_yield_end(h.cfg))
                           if yielded else None),
            )
        except Exception:
            if yielded:
                _serial_yield_end(h.cfg)   # 起不来就立刻回归
            raise
        if yielded:
            res["serial_yielded"] = True
        return h._json(res, 202)
    if u.path == "/api/cancel":
        body = h._body()
        run_id = body.get("run") or ""
        if not run_id:
            return h._err(400, "缺少 run")
        res = h.mgr.cancel(run_id, reason=body.get("reason") or "用户取消")
        return h._json(res, 200 if res.get("ok") else 409)
    if u.path.startswith("/api/pkg/"):
        # 适配包写动作：install / remove / verify / update / activate。
        # ★ 只收**包名**（不收路径/URL/哈希）：来源与 sha256 由内核自己解析。
        action = u.path[len("/api/pkg/"):]
        if action not in ("install", "remove", "verify", "update",
                          "activate", "market"):
            return h._err(404, f"未知包操作：{action}")
        return pkg_actions.pkg(h, action, h._body())
    return False
