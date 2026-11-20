"""cockpit.platform.summary —— 事实投影（projects / matrix / packs）。
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
from cockpit.platform.modes import _profile_summary  # noqa: F401


# ══════════════════════════════════════════════════════════════════
#  数据投影：projects / capabilities
# ══════════════════════════════════════════════════════════════════
def project_cards(cfg: Config) -> dict:
    """工程卡片数据。

    ★ 要求（§6 / M1.6）：**100% 来自 `projects/*.yaml`，不新增任何元数据**。
      下面所有字段要么是 YAML 原值，要么是它 + ``chips/*.yaml`` 的直读，
      要么是由"文件是否存在"推导出来的状态位（明确标注）。
    """
    host = cfg.host or {}
    probes = ((host.get("probes") or {}).get("list") or {})
    caps = serialport_mod.capabilities()

    def _delegated(proj: dict, step: str) -> bool:
        """该步骤是否委托给项目声明的 tools.<name>。"""
        spec = (proj.get("steps") or {}).get(step)
        return isinstance(spec, str) and spec.startswith(run_mod.TOOL_PREFIX)

    cards = []
    #: 工程可见范围由**驾驶舱 profile** 决定（Kharon 只看 zephyr 工程）。
    #: 与「凭什么隐藏」的分工：profile 是装配数据，代码里没有任何厂商分支。
    _prof = _profile_summary(cfg) or {}
    _only_arch = [str(x) for x in ((_prof.get("projects") or {}).get("archetype") or [])]
    for name, raw in sorted(cfg.projects.items()):
        # 多 target 产品：驾驶舱默认展示第一个 target，并额外给出 targets 列表；
        # 后续 UI 选择 target 时用 target id 重新解析（不静默取第一个当真相）。
        raw_targets = cfg.targets_of(raw)
        if len(raw_targets) > 1:
            cfg.select_target(name, str(raw_targets[0]["id"]))
        proj = cfg.resolved_project(name)          # 完成 ${...} 插值
        if _only_arch and str(proj.get("archetype") or "") not in _only_arch:
            continue
        chip_ref = proj.get("chip", "")
        try:
            chip = cfg.chip_of(proj)               # 支持项目级 pack 固定
        except ElabError:
            chip = {}
        # ── 工具链 provider（内核解析，工具层只读投影；不 import 任何 provider 代码）──
        try:
            tc = provider_mod.resolve(cfg, name)
            prov_avail = provider_mod.evaluate(cfg, name, tc)
        except ElabError as exc:
            tc = None
            prov_avail = {"available": False, "reasons": [{
                "code": "provider.error", "key": "", "message": str(exc), "hint": ""}]}
        build = proj.get("build") or {}
        work_dir = build.get("work_dir", "")
        art = proj.get("artifacts") or {}

        # 派生状态位（**非**新元数据，只是"文件在不在"）
        elf = art.get("elf", "")
        built = bool(elf) and Path(elf).exists()

        probe = (proj.get("debug") or {}).get("probe", "")
        monitor = proj.get("monitor") or {}
        has_rules = bool(monitor.get("close_on") or [])

        avail: dict[str, dict] = {s: {"ok": True, "reason": ""} for s in run_mod.ALL_STEPS}
        # ★ 烧录要不要探针由【芯片 flash.runner】裁定（数据驱动，无厂商分支）：
        #   openocd/缺省 = 探针烧录（缺探针置灰）；esp/dfu/jlink 等走 west flash
        #   交回 runner（flash.py 分流），探针无关 —— 不能因"没插 DAP"而全灰。
        flash_runner = str((chip.get("flash") or {}).get("runner") or "")
        flash_needs_probe = flash_runner in ("", "openocd")
        if not (probe and probe in probes):
            reason = (f"未声明可用探针（debug.probe={probe or '空'}）"
                      if not probe else f"探针 {probe!r} 不在 elab.host.yaml: probes.list 中")
            # ★ 委托步骤不需要探针：烧录/校验由项目自带工具（如 esptool）执行。
            if not _delegated(proj, "flash") and flash_needs_probe:
                avail["flash"] = {"ok": False, "reason": reason}
            if not _delegated(proj, "debug_verify"):
                avail["debug_verify"] = {"ok": False, "reason": reason}
        if not has_rules:
            avail["monitor"] = {"ok": False,
                                "reason": "未声明 monitor.close_on → 只能判 inconclusive"}
        elif not caps.get("available"):
            avail["monitor"] = {"ok": False,
                                "reason": f"串口后端不可用（layer={caps.get('layer')}）"}

        # ── 每步 kind/source/available（全部来自内核 ResolvedToolchain；只读投影）──
        steps_view: dict[str, dict] = {}
        for step in run_mod.ALL_STEPS:
            decl = tc.steps.get(step) if tc else None
            if decl is None:
                steps_view[step] = {**avail[step], "kind": "native",
                                    "source": "elab/native", "tool": ""}
                continue
            if decl.kind == "unavailable":
                steps_view[step] = {"ok": False, "reason": decl.reason,
                                    "kind": "unavailable", "source": decl.provider,
                                    "tool": ""}
                continue
            if decl.kind == "external":
                reasons = list((prov_avail or {}).get("reasons") or [])
                if decl.provider == "project":
                    reasons = []
                elif not any("${serial_port}" in str(a) for a in decl.cmd):
                    reasons = [r for r in reasons
                               if r.get("code") != "serial.unavailable"]
                steps_view[step] = {
                    "ok": not reasons,
                    "reason": "；".join(str(r.get("message") or r) for r in reasons),
                    "kind": "external",
                    "source": (decl.provider if decl.provider == "project"
                               else f"{tc.provider.name}@{tc.provider.version}"),
                    "tool": decl.tool_name,
                }
                continue
            steps_view[step] = {**avail[step], "kind": "native",
                                "source": "elab/native", "tool": ""}

        # ── OTA 投影（C1/C3，全部派生：chips yaml ota_layout + boot 台账文件）──
        #   ota=None = 该芯片未声明 ota_layout（普通单镜像工程，UI 不渲染 OTA 行）
        ota_layout = chip.get("ota_layout") or {}
        ota = None
        if ota_layout:
            boot_proj = ota_layout.get("boot_project", "")
            ota = {"boot_project": boot_proj, "slots": len(ota_layout.get("slots") or []),
                   "boot_stamp": None}
            if boot_proj:
                # boot 台账 = C3 冻结策略的落盘状态（version/md5/flashed_at）
                ota["boot_stamp"] = flash_mod.boot_stamp(cfg.root, boot_proj)

        cards.append({
            "name": name,
            "target": proj.get("_target_id", ""),
            "targets": [{"id": str(t.get("id")), "chip": str(t.get("chip"))}
                        for t in raw_targets],
            "chip": chip_ref,
            # 多板对照分组（唯一的新元数据）：同一组 = 同一次对照。
            # 空 = 不参与矩阵（普通工程）。
            "matrix": str(proj.get("matrix") or ""),
            "archetype": proj.get("archetype", ""),
            "root": proj.get("root", ""),
            "work_dir": work_dir,
            "project_name": build.get("project_name", name),
            "generator": build.get("generator", ""),
            "build_type": build.get("build_type", ""),
            "linker_script": build.get("linker_script", ""),
            "artifacts": {k: v for k, v in art.items()},
            "probe": probe,
            # 调试后端：provider.debug.backend > 芯片声明（如 esp-idf）。
            # 非空时驾驶舱「调试」页签不因 probe 为空而置灰。
            "debug_backend": str(
                ((tc.debug if tc else {}) or {}).get("backend")
                or (chip.get("debug") or {}).get("backend") or ""),
            # 调试页签白名单：项目 debug_features ?? provider.debug.features ?? 芯片收敛
            "debug_features": (list(proj.get("debug_features") or [])
                               or list(((tc.debug if tc else {}) or {}).get("features") or [])
                               or _debug_features(chip)),
            # 工具链 provider 投影（available/reasons 来自内核 requires 求值）
            "provider": ({"name": tc.provider.name, "version": tc.provider.version,
                          "source": tc.provider.source, "path": tc.provider.path,
                          "available": bool(prov_avail.get("available")),
                          "reasons": list(prov_avail.get("reasons") or [])}
                         if tc and tc.provider.source != "builtin" else None),
            "serial": proj.get("serial") or {},
            "monitor": {"close_on": len(monitor.get("close_on") or []),
                        "fail_on": len(monitor.get("fail_on") or []),
                        "idle_timeout_s": monitor.get("idle_timeout_s")},
            "provenance": proj.get("provenance") or {},
            # 项目声明工具（通用机制）：驾驶舱据此渲染"工具"按钮
            "tools": [{"name": k, "label": ((v or {}).get("label") or k)}
                      for k, v in sorted((proj.get("tools") or {}).items())],
            # 步骤是否由外部工具执行（项目内联 tool: 或 provider）
            "delegated": {s: (_delegated(proj, s)
                              or steps_view[s].get("kind") == "external")
                          for s in run_mod.ALL_STEPS},
            # 项目级文案覆盖（可选）：label(step) = step_labels[step] ?? STEP_META.label
            "step_labels": dict(proj.get("step_labels") or {}),
            # OTA 投影（None=无 ota_layout；boot_owner 在下方第二遍填充）
            "ota": ota,
            "boot_owner": "",
            # ↓ 以下为派生状态位，不是新元数据
            "derived": {"built": built, "work_dir_exists": bool(work_dir) and Path(work_dir).is_dir()},
            "steps": steps_view,
            "chip_info": {
                "id": chip.get("id", ""), "vendor": chip.get("vendor", ""),
                "family": chip.get("family", ""), "part": chip.get("part", ""),
                "package": chip.get("package", ""), "frequency": chip.get("frequency", ""),
                "core": chip.get("core") or {}, "memory": chip.get("memory") or {},
                "debug": chip.get("debug") or {},
                # ★ 芯片来自哪个适配包（骨架本身不含芯片；空 = 未安装适配包）
                "pack": chip.get("_pack", ""),
                # ── board-as-chip（Zephyr）：目标板名 + 内核名 + 实测读数 ──
                #   `target` 是 `west -b` 的原文；`measured` 来自构建产物（见 _measured_memory）
                "target": chip.get("target", ""),
                "core_name": chip.get("core_name", ""),
                "measured": _measured_memory(work_dir),
            },
        })

    # 第二遍：boot 子工程归属（C1 交叉引用派生——被谁的 ota_layout.boot_project
    # 指到，谁就是"boot 子工程"；自引用除外：boot 工程自己就是 boot）。
    owners = {}
    for c in cards:
        bp = (c.get("ota") or {}).get("boot_project") or ""
        if bp and bp != c["name"]:
            owners[bp] = c["name"]
    for c in cards:
        c["boot_owner"] = owners.get(c["name"], "")

    return {
        "projects": cards,
        "steps": {
            "all": list(run_mod.ALL_STEPS),
            "default": list(run_mod.DEFAULT_STEPS),
            "onhw": sorted(run_mod.ONHW_STEPS),
        },
    }


def _run_summary(path) -> dict:
    """单个 run 的摘要：每步耗时/结论 + 总判决 + build 的内存。

    只解析 ``.jsonl``（几百行量级），**不读** ``.proc.jsonl``（那是全部 stdout）。
    """
    steps: dict[str, dict] = {}
    ok: bool | None = None
    memory: dict = {}
    memory_source = ""
    try:
        for ev in ev_mod.replay(path, 0):
            topic = ev.get("topic")
            if topic == "run/step-exit":
                steps[str(ev.get("step") or "")] = {
                    "ok": bool(ev.get("ok")),
                    "duration_s": float(ev.get("duration_s") or 0.0),
                    "exit_code": ev.get("exit_code"),
                }
                if ev.get("step") == "build" and ev.get("memory"):
                    memory = ev["memory"]
                    memory_source = str(ev.get("memory_source") or "")
            elif topic in ("run/end", "run/cancel"):
                ok = bool(ev.get("ok"))
    except OSError:
        return {}
    total = sum(s.get("duration_s") or 0.0 for s in steps.values())
    return {"steps": steps, "ok": ok, "memory": memory,
            "memory_source": memory_source, "elapsed_s": round(total, 1)}


def matrix_view(cfg: Config, group: str = "") -> dict:
    """多板并排对照：``matrix:`` 同组的工程 + 各自最近一次 run 的事实。

    数据来源只有两处，都是**留档**（断网重进页面仍然在）：
      * ``projects/*.yaml`` 的 `matrix:`（分组）与芯片 target（哪块板）；
      * ``.work/.cockpit/runs/*.jsonl``（最近一次 build 的内存/耗时/结论）。
    内存优先取 run 里的（历史可追溯），缺失时退回构建产物读数（`_measured_memory`）。
    """
    cards = project_cards(cfg)["projects"]
    runs = []
    try:
        runs = ev_mod.list_runs(run_mod.runs_dir_for(cfg))
    except (OSError, ElabError):
        runs = []
    # list_runs 的排序不保证：按开始时间倒序，取每个工程最新的一条
    runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    latest: dict[str, dict] = {}
    for r in runs:
        proj = str(r.get("project") or "")
        if proj and proj not in latest:
            latest[proj] = r

    groups: dict[str, dict] = {}
    for c in cards:
        g = str(c.get("matrix") or "")
        if not g or (group and g != group):
            continue
        node = groups.setdefault(g, {"group": g, "projects": []})
        run = latest.get(c["name"]) or {}
        run_id = str(run.get("run") or "")
        summ = _run_summary(Path(run_mod.runs_dir_for(cfg)) / f"{run_id}.jsonl") \
            if run_id else {}
        mem = summ.get("memory") or _measured_memory(str(c.get("work_dir") or ""))
        src = summ.get("memory_source") or ("zephyr-elf" if mem else "")
        ci = c.get("chip_info") or {}
        node["projects"].append({
            "name": c["name"],
            "chip": c.get("chip", ""),
            "part": ci.get("part") or c.get("chip", ""),
            "target": ci.get("target") or "",
            "board": (ci.get("target") or "").split("/")[0],
            "built": bool((c.get("derived") or {}).get("built")),
            "memory": mem,
            "memory_source": src,
            "elapsed_s": summ.get("elapsed_s", 0.0),
            "steps": summ.get("steps", {}),
            "verdict": ("ok" if summ.get("ok") is True
                        else "failed" if summ.get("ok") is False
                        else "unknown" if not run_id else "running"),
            "run": run_id,
            "started_at": run.get("started_at"),
        })
    out = [groups[k] for k in sorted(groups)]
    for node in out:
        node["projects"].sort(key=lambda p: p["name"])
    return {"groups": out, "group": group}


def _packs_summary(cfg: Config) -> dict:
    """适配包最小投影（capabilities 用；市场索引走 ``/api/packs``）。"""
    try:
        return pkg_mod.status(cfg, include_market=False)
    except Exception as exc:                          # noqa: BLE001
        return {"roots": [], "install_root": "", "installed": [],
                "chips": [], "error": str(exc)}


__all__ = [n for n in list(globals()) if not n.startswith("__")]
