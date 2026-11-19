"""cockpit.api.routes.pkg_actions —— 适配包动作。"""

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


def pkg(h, action: str, body: dict):
    """``POST /api/pkg/*`` —— 包操作的唯一入口。

    语义与 CLI ``elab pkg`` 同源（都调 ``elab.pkg`` 的同名函数）：

        install   202 + job（异步：git clone / 哈希 / 复制可能很久）
        remove    202 + job（卸载 + 清理平台 skill）
        update    202 + job（按市场索引升级，缺省全部）
        verify    200（只读、快，同步返回逐包结果）
        activate  200（启用一套已装包 + 重生成平台 skill + 重载 Config）
        market    202 + job（现场遍历 GitHub 上的 elab-pack 仓库，写本机市场缓存）

    ★ 运行约束：有闭环在跑 → 包操作 409；有包操作在跑 → 新包操作 409。
      两者与 ``/api/run`` 的冻结守卫成对（"安装期间冻结闭环"）。
    ★ 只收包名：来源、sha256 都由内核从市场条目解析，前端无从篡改。
    """
    name = str(body.get("name") or "").strip()
    if action == "verify":
        return h._json({"ok": True,
                           "results": pkg_mod.verify(h.cfg, name or None)})
    if action == "market":
        # ★ 首屏遍历：把 GitHub 上现有 elab-pack 仓库现场发现一遍（异步 job，
        #   结果落 .work/pkg/market-live.json，15 分钟内复用）。不改芯片数据，
        #   所以**不**参与闭环冻结守卫。
        def work(log, stage):
            cfg = type(h).cfg
            stage("遍历 GitHub 上的 elab-pack 仓库")
            res = pkg_mod.refresh_market(cfg, log=log)
            stage("生成市场索引")
            return res
        return h._json(h.pkg_jobs.start("market", "", work), 202)
    active_run = h.mgr.list_active()
    if active_run:
        return h._err(409, (
            f"有闭环正在跑（run={active_run[0].get('run')}，"
            f"步骤 {'/'.join(active_run[0].get('steps') or [])}）—— "
            f"等它结束再做包操作（安装/卸载会改芯片数据）"))
    if h.pkg_jobs.busy():
        return h._err(409, "已有包操作进行中 —— 等它完成")
    if not name and action not in ("update", "market"):
        return h._err(400, "缺少 name（适配包名，如 st/stm32）")

    if action == "activate":
        rec = pkg_mod.set_active(h.cfg, name, log=lambda *a: None)
        cfg2 = Config(h.cfg.root)
        type(h).cfg = cfg2          # 与 /api/adapt 写盘后重载同源
        h.mgr.cfg = cfg2
        skills = pkg_mod.regenerate_skills(
            cfg2, (pkg_mod.discover(cfg2).get(name) or {}).get("chips") or [],
            log=lambda *a: None)
        return h._json({**rec, "skills": skills,
                           "setup": setup_mod.pack_report(cfg2, pack=name),
                           "packs": pkg_mod.status(cfg2, include_market=False)}, 200)

    if action == "install":
        official = bool(body.get("official"))
        force = bool(body.get("force"))

        def work(log, stage):
            cfg = type(h).cfg
            stage("安装：校验清单与来源")
            rec = pkg_mod.install_entry(cfg, name, official=official,
                                        force=force, log=log)
            stage("生成平台 skill")
            skills = pkg_mod.regenerate_skills(cfg, rec.get("chips"), log)
            stage("环境自检")
            report = setup_mod.pack_report(cfg, pack=rec.get("name") or name)
            for item in report["items"]:
                if item["level"] in ("error", "warn"):
                    log(f"[setup] {item['code']}: {item['message']}")
                    if item.get("action"):
                        log(f"[setup]   → {item['action']}")
            return {**rec, "skills": skills, "setup": report}
        return h._json(h.pkg_jobs.start("install", name, work), 202)

    if action == "remove":
        def work(log, stage):
            cfg = type(h).cfg
            man = (pkg_mod.discover(cfg).get(name) or {}).get("manifest") or {}
            platforms = {(man.get("vendor"), man.get("platform"))} if man else set()
            stage("卸载")
            rec = pkg_mod.remove(cfg, name, log=log)
            stage("清理平台 skill")
            pruned = pkg_mod.prune_skills(cfg, platforms, log)
            return {**rec, "skills_removed": pruned}
        return h._json(h.pkg_jobs.start("remove", name, work), 202)

    # update（name 可缺省 = 全部已装包）
    force = bool(body.get("force"))

    def work(log, stage):
        cfg = type(h).cfg
        stage("按市场索引更新")
        recs = pkg_mod.update(cfg, name or None, force=force, log=log)
        stage("生成平台 skill")
        skills = pkg_mod.regenerate_skills(
            cfg, [c for r in recs for c in r.get("chips") or []], log)
        stage("环境自检")
        reports = [setup_mod.pack_report(cfg, pack=r.get("name") or "")
                   for r in recs]
        for report in reports:
            for item in report["items"]:
                if item["level"] in ("error", "warn"):
                    log(f"[setup] {item['code']}: {item['message']}")
        return {"updated": recs, "skills": skills, "setup": reports}
    return h._json(h.pkg_jobs.start("update", name, work), 202)


def adapt(h, body: dict):
    """``POST /api/adapt`` —— 把图形配置器导出的工程接进 elab（M5.6）。

    **两段式，probe 永远在前**（"先看清楚再动手"，与只读预览 C26 同一精神）：

        action=probe   只读：把"会生成什么样的接入文件"摆出来，不落任何盘
        action=write   写入 ``projects/<name>.yaml``
        action=check   只读复探测：与已写入文件比对，报 missing/identical/drift

    write 的三类拒绝**原样**沿用 ``adapt.write`` 的实现（只有一份实现，CLI 与
    这里行为必然一致）：未决项 → 拒（force 可越）、芯片未匹配 → 拒、
    人工接管（generated-by 标记缺失）→ 拒。ElabError → 400。

    ★ **刻意不做**三绿灯 verify：那是 build 级长任务（真跑 configure + 编译，
      十几秒起步），同步做会把服务线程钉死（§5.1 K1 的反面教材）。
      故返回 ``next`` 引导前端走**现有** run 通道跑 ``doctor_deep + build`` ——
      三绿灯的判据（doctor_deep 全绿 / 出 elf / guard.untouched）一个不少，
      且进度走 SSE、结果落事件流，与"跑全闭环"完全同一套观测设施。
    """
    action = body.get("action") or "probe"
    if action not in ("probe", "write", "check"):
        raise ElabError(f"action 必须是 probe / write / check：{action!r}")
    raw = (body.get("path") or "").strip()
    if not raw:
        raise ElabError("缺少 path（要适配的工程根目录）")
    p = Path(raw).expanduser()
    # ★ 绝对路径硬性要求：相对路径会让"适配到哪了"取决于服务进程的 cwd ——
    #   那种"在我机器上能用"的坑没有观测手段，必须在入口就拒掉。
    if not p.is_absolute():
        raise ElabError(f"path 必须是绝对路径：{raw!r}")
    if not p.is_dir():
        raise ElabError(f"目录不存在：{p}")

    force = bool(body.get("force"))
    name = (body.get("name") or "").strip()
    probe = adapt_mod.probe_project(h.cfg, str(p))
    out = {"action": action, "probe": probe.to_dict(), "tier": probe.tier,
           "confidence": probe.confidence,
           "name": name or adapt_mod.default_name(probe)}
    if action == "write":
        out["result"] = adapt_mod.write(h.cfg, probe, name=out["name"],
                                        force=force, log=lambda *a: None)
        # ★ 真写出了新的 projects/<name>.yaml → **必须重载 Config**，否则新工程
        #   对驾驶舱不可见（/api/projects 没有它的卡片、/api/run 也起不了它）。
        #   与 CLI `_adapt_verify` 的做法同源（"新增接入文件后重载"）。
        #   赋值是原子的；在途请求仍握着旧引用跑完 —— 旧配置对旧工程依然自洽。
        if out["result"].get("written"):
            cfg2 = Config(h.cfg.root)
            type(h).cfg = cfg2
            h.mgr.cfg = cfg2
    elif action == "check":
        # 复探测（不改盘）：重新探测后与已写入的 projects/<name>.yaml 逐字节比对。
        # status ∈ missing / identical / drift —— 与 CLI `elab adapt --check` 同源。
        out["result"] = adapt_mod.write(h.cfg, probe, name=out["name"],
                                        check=True, log=lambda *a: None)
    out["next"] = {
        "steps": ["doctor_deep", "build"],
        "hint": ("写入后跑「体检 + 编译」即三绿灯自证：doctor_deep 全绿"
                 "+ build 出 elf + 零改动守卫 untouched=true"),
    }
    return h._json(out, 200)
