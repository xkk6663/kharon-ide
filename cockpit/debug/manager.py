"""cockpit.debug.manager —— 调试会话登记簿 + 事件扇出。
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
from cockpit.debug.helpers import *  # noqa: F401,F403



class DebugManager:
    """调试会话登记簿 + 事件扇出（多会话，``debug.max_sessions`` 钳制 1–4）。

    语义（见执行计划_调试P3 §6）：
      - **一个目标一条会话**：同工程第二条直接 ``409 debug-target-busy``；
      - 租约只对 ``owner=agent`` 生效（到期自动关闭并**恢复运行**）；
      - 关闭必须恢复目标运行（C36 延伸）；
      - 与「会用到串口的闭环」「串口监视」互斥（N5，**全局**，不因多会话放松）；
      - ``debug_session`` 缺省时走**唯一**会话；多会话下缺省即 ``debug-session-required``。
    """

    def __init__(self, cfg: Config, runs: RunManager, *, log=print):
        self.cfg = cfg
        self.runs = runs
        self.log = log
        self._lock = threading.Lock()
        self._sessions: dict[str, dbgsess_mod.DebugSession] = {}
        self._allow_write: dict[str, bool] = {}
        self._open_seq = 0
        self._seq = 0
        self._subs: dict[queue.Queue, str] = {}      # queue -> 过滤用 debug_session（""=全量）
        self._lease_thread = threading.Thread(target=self._lease_loop, daemon=True,
                                              name="elab-debug-lease")
        self._lease_thread.start()

    # ── 查询 ────────────────────────────────────────────────────
    @property
    def max_sessions(self) -> int:
        """``elab.host.yaml: debug.max_sessions``（缺省 2，钳制 1–4）。"""
        try:
            raw = int(((self.cfg.host or {}).get("debug") or {}).get("max_sessions", 2))
        except (TypeError, ValueError):
            raw = 2
        return max(1, min(raw, 4))

    def list(self) -> list[dbgsess_mod.DebugSession]:
        with self._lock:
            return [s for s in self._sessions.values()
                    if s.state not in ("closed", "error")]

    def get(self, debug_session: str = "") -> dbgsess_mod.DebugSession | None:
        """按 id 取会话；``debug_session`` 为空时返回**唯一**会话（多会话时 None）。"""
        with self._lock:
            if debug_session:
                return self._sessions.get(debug_session)
            alive = [s for s in self._sessions.values() if s.state not in ("closed", "error")]
            return alive[0] if len(alive) == 1 else None

    def snapshot(self, debug_session: str = "") -> dict:
        """``{sessions:[摘要…], session: <默认会话快照|null>, session_id, max_sessions}``。

        顶层 ``session`` 保持 P2 的形态（单会话旧前端零改动）。
        """
        alive = self.list()
        rows = [s.snapshot() for s in alive]
        cur = self.get(debug_session) if debug_session else (alive[0] if alive else None)
        return {
            "sessions": rows,
            "session": cur.snapshot() if cur is not None else None,
            "session_id": cur.session_id if cur is not None else "",
            "max_sessions": self.max_sessions,
            "allow_write": (self._allow_write.get(cur.session_id, False)
                            if cur is not None else False),
        }

    def require(self, debug_session: str = "") -> dbgsess_mod.DebugSession:
        alive = self.list()
        if debug_session:
            sess = self.get(debug_session)
            if sess is None or sess.state in ("closed", "error"):
                raise dbgsess_mod.DebugError(
                    "debug-no-session", f"调试会话不存在或已结束：{debug_session}",
                    "重新 elab_debug_open 或读取 GET /api/debug/session 的 sessions 列表")
            return sess
        if not alive:
            raise dbgsess_mod.DebugError("debug-no-session", "当前没有调试会话",
                                         "先 elab_debug_open / 面板点「开始调试」")
        if len(alive) > 1:
            raise dbgsess_mod.DebugError(
                "debug-session-required",
                f"当前有 {len(alive)} 个调试会话，必须显式指定 debug_session",
                "从 GET /api/debug/session 的 sessions[].id 里选一个")
        return alive[0]

    # ── 事件 ────────────────────────────────────────────────────
    def subscribe(self, debug_session: str = "") -> queue.Queue:
        """订阅调试事件；带 ``debug_session`` 时只收该会话的事件（D7）。"""
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs[q] = str(debug_session or "")
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.pop(q, None)

    def _emit_for(self, session_id: str, topic: str, data: dict) -> None:
        with self._lock:
            self._seq += 1
            envelope = {
                "topic": topic,
                "seq": self._seq,
                "ts": time.time(),
                "session": session_id,
                "data": data,
            }
            subs = list(self._subs.items())
        for q, want in subs:
            if want and want != session_id:
                continue
            try:
                if q.full():
                    q.get_nowait()                 # 丢最旧（与 RunManager 同款有界背压）
                q.put_nowait(envelope)
            except (queue.Empty, queue.Full):
                pass

    def _emit(self, topic: str, data: dict) -> None:
        """兼容 P2 的 manager 级 emit：绑定到当前唯一会话（多会话时留空）。"""
        sess = self.get()
        self._emit_for(sess.session_id if sess else "", topic, data)

    # ── 互斥 ────────────────────────────────────────────────────
    def _blocker(self) -> dict | None:
        for a in self.runs.list_active():
            if not a.get("alive"):
                continue
            if set(a.get("steps") or []) & _SERIAL_STEPS:
                return {"code": "debug-busy",
                        "message": (f"闭环 {a.get('project')} 正在占用 SWD/串口"
                                    f"（steps={'/'.join(a.get('steps') or [])}）"),
                        "hint": "等闭环结束或先取消"}
        mon = serialmon_mod.status()
        if mon.get("active"):
            return {"code": "serial-monitor-active",
                    "message": f"串口监视正在占用 {mon.get('port')}",
                    "hint": "先在串口页签关闭监视"}
        return None

    # ── 打开 / 关闭 ──────────────────────────────────────────────
    def open(self, *, project: str, actor: str = "human", options: dict | None = None,
             lease_s: int = 0, allow_write: bool = False) -> dict:
        blocker = self._blocker()
        if blocker:
            raise dbgsess_mod.DebugError(blocker["code"], blocker["message"], blocker["hint"])
        with self._lock:
            alive = [s for s in self._sessions.values()
                     if s.state not in ("closed", "error")]
            # 同一工程 = 同一目标：一个 SWD 目标只能被一个 GDB 附着（D3）
            dup = next((s for s in alive if s.plan.name == project), None)
            if dup is not None:
                raise dbgsess_mod.DebugError(
                    "debug-target-busy",
                    f"目标 {project} 已有调试会话（{dup.session_id}，owner={dup.owner}）",
                    "同一块板/同一工程不能开第二会话；先关闭它，或选另一个工程")
            if len(alive) >= self.max_sessions:
                raise dbgsess_mod.DebugError(
                    "debug-session-limit",
                    f"已达会话上限（{self.max_sessions}，见 elab.host.yaml: debug.max_sessions）",
                    "关闭一个会话后再开")
            self._open_seq += 1
            open_seq = self._open_seq
            lease = (0 if actor != "agent" else
                     max(DEBUG_LEASE_MIN_S, min(int(lease_s or DEBUG_LEASE_DEFAULT_S),
                                                DEBUG_LEASE_MAX_S)))
            try:
                plan = plan_for(self.cfg, project)
            except Exception as exc:                   # noqa: BLE001
                raise dbgsess_mod.DebugError("debug-bad-request",
                                             f"工程不可用：{project!r}（{exc}）")
            sess = dbgsess_mod.DebugSession(
                plan, actor=actor, lease_s=lease, options=options or {},
                open_seq=open_seq, emit=self._emit, log=self.log)
            self._sessions[sess.session_id] = sess
            self._allow_write[sess.session_id] = bool(allow_write) and actor == "human"
        sid = sess.session_id
        sess.bind_emit(lambda topic, data, _sid=sid: self._emit_for(_sid, topic, data))
        _debug_audit(self.cfg, {"action": "open", "session": sid,
                                "project": project, "actor": actor,
                                "options": options or {}, "lease_s": lease})
        try:
            snap = sess.open()
        except dbgsess_mod.DebugError as exc:
            with self._lock:
                self._sessions.pop(sid, None)
                self._allow_write.pop(sid, None)
            sess.error = exc.as_dict()
            _debug_audit(self.cfg, {"action": "open-failed", "session": sid,
                                    "project": project, "code": exc.code,
                                    "message": exc.message})
            # 已有别的会话 + 探针类失败 → 明确告诉用户是**同一个探针**被占了，
            # 而不是让 openocd 的随机报错糊在脸上（D3）
            if self.list() and exc.code in ("debug-openocd-dead", "debug-timeout"):
                raise dbgsess_mod.DebugError(
                    "debug-port-hijacked",
                    f"探针被占用：{exc.message}",
                    "一个 CMSIS-DAP 探针同一时刻只能附着一颗芯片；换探针或先关另一会话")
            raise
        self.log(f"[debug] 会话 {sid} 打开：{project} ({actor})")
        return {"session": snap, "session_id": sid, "sessions": self.snapshot()["sessions"]}

    def close(self, *, debug_session: str = "", reason: str = "user-closed",
              resume: bool = True, actor: str = "human", force: bool = False) -> dict:
        try:
            sess = self.require(debug_session)
        except dbgsess_mod.DebugError:
            return {"closed": False, "reason": "no-session"}
        with self._lock:
            self._sessions.pop(sess.session_id, None)
            self._allow_write.pop(sess.session_id, None)
        res = sess.close(resume=resume, reason=reason)
        self.log(f"[debug] 会话 {sess.session_id} 关闭：{reason}（resumed={res['resumed']}）")
        _debug_audit(self.cfg, {"action": "close", "session": sess.session_id,
                                "reason": reason, "actor": actor, "resumed": res["resumed"]})
        return {**res, "session": sess.session_id}

    def drop(self, sess: dbgsess_mod.DebugSession) -> None:
        with self._lock:
            self._sessions.pop(sess.session_id, None)
            self._allow_write.pop(sess.session_id, None)

    # ── 写权限（按会话）──────────────────────────────────────────
    def require_write(self, *, confirm: bool, debug_session: str = "") -> None:
        sess = self.require(debug_session)
        if not self._allow_write.get(sess.session_id, False):
            raise dbgsess_mod.DebugError(
                "debug-write-denied", "会话未开启写权限",
                "以人类身份重开会话并传 allow_write=true")
        if not confirm:
            raise dbgsess_mod.DebugError("debug-bad-request", "写操作需要 confirm=true")

    def allow_write(self, debug_session: str = "") -> bool:
        sess = self.get(debug_session)
        return bool(self._allow_write.get(sess.session_id, False)) if sess else False

    # ── 租约回收 ────────────────────────────────────────────────
    def _lease_loop(self) -> None:
        while True:
            time.sleep(2.0)
            for sess in self.list():
                if sess.owner != "agent" or sess.lease_s <= 0:
                    continue
                idle = time.time() - sess.last_active
                if idle < sess.lease_s:
                    continue
                # 最后 30s 先发一条告警（面板显示"租约即将到期"）
                if idle >= sess.lease_s - 30 and not getattr(sess, "_lease_warned", False):
                    sess._lease_warned = True
                    self._emit_for(sess.session_id, "debug/output",
                                   {"stream": "log",
                                    "text": "租约将在 30s 后到期（agent 无活动）"})
                if idle >= sess.lease_s:
                    try:
                        self.close(debug_session=sess.session_id, reason="lease-expired",
                                   resume=True, actor="system")
                    except Exception as exc:             # noqa: BLE001
                        self.log(f"[debug] 租约回收失败：{exc!r}")


__all__ = [n for n in list(globals()) if not n.startswith("__")]
