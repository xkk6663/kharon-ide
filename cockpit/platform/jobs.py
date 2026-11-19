"""cockpit.platform.jobs —— 适配包操作任务簿（202 + 轮询）。
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



# ══════════════════════════════════════════════════════════════════
#  适配包操作（安装 / 卸载 / 校验 / 更新 / 启用）
# ══════════════════════════════════════════════════════════════════
class PkgJobManager:
    """包操作任务簿：**同一时间最多一个**包操作（全局一把锁）。

    ★ 为什么必须异步（202 + 轮询）：
      ``pkg.install()`` 里含 ``git clone``（无超时）/ 整树哈希 / 复制校验，
      同步塞进一条 HTTP 请求里，工具层代理的 ``fetch`` 没有超时，UI 就会
      "转圈到天荒地老"。异步 job 把长任务挪到独立线程，HTTP 立刻返回 202，
      前端 500ms 轮询 ``/api/pkg/job``。

    ★ 为什么"全局一个"：安装/卸载会改 ``packs/installed`` 与 ``active.json``，
      两个并发操作会让 discover 看到半成品目录。串行是最便宜的正确答案。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._thread: threading.Thread | None = None
        self._reload = None

    def bind(self, reload_cb) -> None:
        """注入"操作完成后重建 Config"的回调（make_server 里接线）。"""
        self._reload = reload_cb

    def busy(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.get("state") in ("queued", "running")

    def current(self) -> dict | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def snapshot(self, job_id: str = "") -> dict | None:
        with self._lock:
            if not self._job:
                return None
            if job_id and job_id != self._job["id"]:
                return None
            return dict(self._job)

    def start(self, action: str, name: str, work) -> dict:
        """登记并启动一个包操作；已有在跑 → 抛 ElabError（路由回 409）。"""
        with self._lock:
            if self._job is not None and self._job.get("state") in ("queued", "running"):
                raise ElabError(
                    f"已有包操作进行中（{self._job['action']} {self._job['name']}）—— 等它完成")
            job = {
                "id": time.strftime("%Y%m%d%H%M%S") + f"-{action}",
                "action": action,
                "name": name,
                "state": "queued",
                "stage": "排队",
                "log": [],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._job = job
            t = threading.Thread(target=self._run, args=(job, work), daemon=True,
                                 name=f"elab-pkg-{action}")
            t.start()
            self._thread = t
            return dict(job)

    def _run(self, job: dict, work) -> None:
        def log(msg) -> None:
            with self._lock:
                lines = job["log"]
                lines.append(str(msg))
                if len(lines) > 200:
                    del lines[: len(lines) - 200]

        def stage(text: str) -> None:
            with self._lock:
                job["stage"] = str(text)

        try:
            with self._lock:
                job["state"] = "running"
            result = work(log, stage)
            with self._lock:
                job.update(state="ok", stage="完成", result=result,
                           finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            if self._reload is not None:
                self._reload()
        except Exception as exc:                   # noqa: BLE001
            with self._lock:
                job.update(state="failed", stage="失败", error=str(exc),
                           finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))


__all__ = [n for n in list(globals()) if not n.startswith("__")]
