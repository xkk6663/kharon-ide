"""cockpit.platform.state —— capabilities / 工作区 / 计划预览 / 串口让渡。
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
from cockpit.run.manager import RunManager  # noqa: F401
from cockpit.platform.summary import _packs_summary  # noqa: F401
from cockpit.platform.modes import _profile_summary  # noqa: F401




def capabilities(cfg: Config, mgr: RunManager) -> dict:
    caps = serialport_mod.capabilities()
    ports, backend = ("[]", "")
    try:
        plist, backend = serialport_mod.list_ports()
        ports = [{"name": p.name, "kind": p.kind, "device": p.device, "desc": p.desc}
                 for p in plist]
    except Exception as exc:                          # noqa: BLE001
        ports = []
        backend = f"枚举失败：{exc}"
    host = cfg.host or {}
    return {
        "cockpit": {"version": COCKPIT_VERSION, "dist": to_fwd(DIST_DIR),
                    "dist_ready": (DIST_DIR / "index.html").exists(),
                    "sse_heartbeat_s": SSE_HEARTBEAT_S,
                    "proc_batch_s": PROC_BATCH_S},
        "schema_version": ev_mod.EVENT_SCHEMA_VERSION,
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "root": to_fwd(cfg.root),
        "runs_dir": to_fwd(mgr.runs_dir),
        # ★ 适配包投影（骨架无芯片：此处为空即"通用工具链"形态）
        "packs": _packs_summary(cfg),
        "serial": {**caps, "backend": backend, "ports": ports,
                   "host_default": (host.get("serial") or {}).get("default", "auto"),
                   "host_baud": (host.get("serial") or {}).get("baud", 115200),
                   # 宿主/工程没写 line 时的默认（面板按此渲染"跟随配置"）
                   "host_line": serialport_mod.LineConfig().as_dict(),
                   # 写能力与读能力同后端（ctypes L1 也已支持写，见 SerialIO.for_write）。
                   # 单列一个字段是因为前端要据此决定输入框是否可写 —— 不该让它去
                   # 猜"available 是不是也包括写"。
                   "write_available": bool(caps["available"]),
                   # 常驻监视会话快照（active/port/baud）—— 界面据此画「打开/关闭监视」
                   "monitor": serialmon_mod.status(),
                   # TX/RX 的 console 日志（M3-b2）—— 界面据此提示"服务端存有 N 条历史"
                   "console": _console_info(cfg)},
        "active_runs": mgr.list_active(),
        "steps": {"all": list(run_mod.ALL_STEPS),
                  "default": list(run_mod.DEFAULT_STEPS),
                  "onhw": sorted(run_mod.ONHW_STEPS)},
        # 工具层自描述（§16）：标签/通道/档位由内核声明
        "meta": {"steps": STEP_META, "plans": KERNEL_PLANS,
                 "channels": ["build", "flash", "debug", "serial"],
                 "serial": SERIAL_META,
                 "debug": _debug_meta(cfg),
                 # 探针清单：面板据此渲染下拉（available = interface cfg 是否落地）
                 "probes": _probes_summary(cfg),
                 # R7 / P1-4：profile 决定右列工作区与启用工具（缺省 = 内置集合）
                 "profile": _profile_summary(cfg)},
    }


def _probes_summary(cfg: Config) -> dict:
    """``capabilities.meta.probes`` —— 本机可用探针的唯一事实源。

    ★ 只读 L0（`elab.host.yaml: probes.list`）；interface cfg 是否存在决定 available。
      面板不该自己猜「某个探针能不能上板」。
    """
    host = Host(cfg)
    scripts = host.openocd_scripts
    items = []
    for name, raw in (host.probes or {}).items():
        info = raw or {}
        icfg = str(info.get("interface_cfg") or "")
        available = True
        if icfg and scripts:
            available = (Path(scripts) / icfg).is_file()
        items.append({"name": name,
                      "backend": str(info.get("backend") or ""),
                      "interface_cfg": icfg,
                      "speed": info.get("speed"),
                      "available": available})
    return {"default": host.default_probe, "list": items}


def _console_info(cfg: Config) -> dict:
    """手写通道 console 日志的位置与条数。

    ★ 为什么值得进 capabilities：那里是界面判断"要不要提示/提示什么"的唯一来源。
      没有它，前端只能猜"服务端到底存了多少历史"。
    """
    try:
        clog = serialconsole_mod.ConsoleLog(serialconsole_mod.console_path_for(cfg))
        return {"path": to_fwd(clog.path), "count": clog.count(),
                "max_records": clog.max_records, "error": None}
    except Exception as exc:                          # noqa: BLE001
        return {"path": "", "count": 0, "max_records": 0, "error": str(exc)}


def _workspace_view(cfg: Config, *, app: str = "", with_modules: bool = False,
                    refresh: bool = False) -> dict:
    """``GET /api/workspace`` —— 工作区事实（拓扑 / 应用形态 / 去哪儿构建）。

    **默认不跑 west**：L1「看懂」的门槛必须低到"什么都不用装"。需要模块清单与
    板子三来源时加 ``?modules=1``（那时才跑一次 ``west list`` / ``west boards``）。
    """
    from elab import westws as westws_mod2
    from elab import wszephyr as wszephyr_mod

    app_dir = app
    if app_dir and not app_dir.startswith(("/", "\\")) and ":" not in app_dir[:3]:
        # 既不是绝对路径、也不像 Windows 盘符 → 当工程名解析
        try:
            proj = cfg.resolved_project(app_dir)
            app_dir = str(proj.get("root") or "")
        except Exception:                             # noqa: BLE001
            app_dir = ""
    facts = wszephyr_mod.discover(cfg, app_dir, refresh=refresh)
    out = {"ok": True, "facts": facts.to_dict()}
    if with_modules:
        snap = westws_mod2.snapshot(cfg, refresh=refresh, app_dir=app_dir)
        out["modules"] = snap.get("modules") or []
        out["module_total"] = snap.get("module_total", 0)
        out["module_present"] = snap.get("module_present", 0)
        out["boards_by_source"] = snap.get("boards_by_source") or {}
    return out


def _plan_preview(cfg: Config, qs: dict) -> dict:
    """``GET /api/plan`` —— 只读命令预览（M2「先看命令再执行」，§14.4）。

    ★ 为什么是 **GET** 而不是 POST：它**没有副作用**（不 spawn、不写盘、
      不发射事件），语义上是一个纯查询。做成 GET 的收益是可 `curl` 直接验、
      可被浏览器/代理按幂等处理；做成 POST 则会让人以为"点了就动手了"。

    ★ 命令**不在这里拼**：整份预览来自 ``run.preview()``，而它复用各模块
      自己的 dry-run/print 分支。服务端只负责把 query 参数翻译过去 ——
      任何"在这里顺手拼一条命令"都是给未来埋分叉。
    """
    project = (qs.get("project") or [""])[0]
    if not project:
        raise ElabError("缺少 project")
    steps = (qs.get("steps") or [None])[0]
    clean = (qs.get("clean") or ["0"])[0].lower() in ("1", "true", "yes", "on")
    jobs_raw = (qs.get("jobs") or [""])[0]
    jobs = int(jobs_raw) if jobs_raw.strip().isdigit() else None
    return run_mod.preview(cfg, project, steps=steps, clean=clean, jobs=jobs)


#: 会占用串口 / SWD 的步骤 —— 它们运行期间，手写通道必须让路。
#:   flash/debug_verify 起 openocd（占 SWD → N5 会连带锁住 VCP）；
#:   monitor 自己就握着串口。
_SERIAL_STEPS = frozenset({"flash", "debug_verify", "monitor"})


def _serial_blocker(mgr: "RunManager") -> dict | None:
    """有没有「会用到串口的闭环」正在跑？有就返回它，否则 None。

    ★ 为什么**提前**拦，而不是让写入自己失败：openocd 占着 SWD 时，VCP 会以
      ``ERROR_ACCESS_DENIED`` 打不开，用户只看到"端口被拒"，却不知道是自己刚点的
      闭环占着 —— 于是去拔插、去杀进程。在这里拦下就能**指名道姓**地说清楚。
    """
    for a in mgr.list_active():
        if not a.get("alive"):
            continue
        if set(a.get("steps") or []) & _SERIAL_STEPS:
            return a
    return None


# ── 闭环与监视：让位 / 回归（M2 §4.3.6）──────────────────────────
#: 引用计数：并行的 serial 相关 run 全部结束才回归一次（不重复开）。
_YIELD_LOCK = threading.Lock()
_YIELD = {"count": 0, "armed": False, "preexisting": False,
          "project": "", "policy": "open", "suspended": False}


def _resume_policy(cfg: Config, project: str, override=None) -> str:
    """回归策略：请求 > 项目 ``serial.resume_after_run`` > 默认 ``open``。"""
    pol = str(override or "")
    if not pol:
        proj = cfg.resolved_project(project) if project else {}
        ser = (proj.get("serial") or {}) if isinstance(proj, dict) else {}
        pol = str(ser.get("resume_after_run") or "open")
    return pol if pol in ("open", "restore", "off") else "open"


def _serial_yield_begin(cfg: Config, project: str, *, policy: str) -> None:
    """run 要串口：把正在开的监视挂起（保留环与 epoch），并记下回归策略。"""
    with _YIELD_LOCK:
        _YIELD["count"] += 1
        if _YIELD["armed"]:
            return
        mon = serialmon_mod.status()
        preexist = bool(mon.get("active"))
        _YIELD.update({"armed": True, "preexisting": preexist,
                       "project": project, "policy": policy, "suspended": False})
    if not preexist:
        return
    # ★ suspend 在锁外执行：它要 join 读线程（最多 2s），不能让别人等锁。
    res = serialmon_mod.suspend("闭环占用串口，监视已暂停", by_run=True)
    with _YIELD_LOCK:
        _YIELD["suspended"] = bool(res.get("ok"))


def _serial_yield_end(cfg: Config, log=print) -> None:
    """run 结束：引用计数归零时按策略回归（原无会话则新开）。"""
    with _YIELD_LOCK:
        if _YIELD["count"] > 0:
            _YIELD["count"] -= 1
        if _YIELD["count"] > 0 or not _YIELD["armed"]:
            return
        state = dict(_YIELD)
        _YIELD.update({"armed": False, "preexisting": False,
                       "project": "", "policy": "open", "suspended": False})
    if state["policy"] == "off":
        # 回到旧行为：结束即关（把让位中的会话真正关掉，不留 suspended）。
        if state["preexisting"] and state["suspended"]:
            serialmon_mod.close_session()
        return
    if state["preexisting"]:
        if state["suspended"]:
            res = serialmon_mod.resume()
            log(f"[cockpit] 串口监视回归：{'ok' if res.get('ok') else res.get('error')}")
        return
    if state["policy"] == "open" and state["project"]:
        res = serialmon_mod.open_session(cfg, state["project"], opened_by="auto")
        if not res.get("ok"):
            log(f"[cockpit] 自动重开监视失败：{res.get('error')}")


__all__ = [n for n in list(globals()) if not n.startswith("__")]
