"""cockpit.api.routes.serial —— 串口 / Modbus / 帧协议。"""

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


from cockpit.api.routes import sse, web  # noqa: F401

def handle_get(h, path, qs):
    if path == "/api/serial/console":
        # 手写通道的 TX/RX 历史（M3-b2）。**独立的** console 日志，
        # 不是 run 事件流（写通道不属于任何 run，见 ICD §3.5）。
        raw = (qs.get("limit") or [""])[0]
        try:
            limit = int(raw) if raw else serialconsole_mod.DEFAULT_TAIL
        except ValueError:
            raise ElabError(f"limit 必须是整数：{raw!r}")
        limit = max(1, min(limit, serialconsole_mod.MAX_RECORDS))
        clog = serialconsole_mod.ConsoleLog(serialconsole_mod.console_path_for(h.cfg))
        recs = clog.tail(limit)
        return h._json({"records": recs, "count": clog.count(),
                           "requested": limit, "path": to_fwd(clog.path)})
    if path == "/api/serial/widgets":
        # 工程级发送组件（M1）：只读投影（rev / widgets / warnings）。
        # 坏文件不阻塞串口功能：文件级错误在 error，单组件问题在 warnings。
        project = (qs.get("project") or [""])[0]
        if not project:
            raise ElabError("缺少 project")
        return h._json(serialwidgets_mod.load(h.cfg, project))
    if path == "/api/serial/widget/job":
        # 不带 id = 当前未结束的任务（面板刷新/agent 找回在跑的那一个）。
        job_id = (qs.get("id") or [""])[0]
        snap = serialwidgets_mod.job_status(job_id)
        if snap is None:
            return h._err(404, ("没有正在进行的组件任务" if not job_id
                                   else f"没有这个组件任务（或已过期）：{job_id!r}"))
        return h._json(snap)
    if path == "/api/serial/monitor/bytes":
        # P1 字节真值：带方向/时间/序号的原始批次（data_b64）。
        # ?after=批次序号 &dir=all|rx|tx &limit=最多返回几个批次
        raw = (qs.get("after") or ["0"])[0]
        try:
            after = int(raw)
        except ValueError:
            raise ElabError(f"after 必须是整数：{raw!r}")
        direction = (qs.get("dir") or ["all"])[0]
        if direction not in ("all", "rx", "tx"):
            raise ElabError(f"dir 必须是 all/rx/tx：{direction!r}")
        raw_lim = (qs.get("limit") or ["0"])[0]
        try:
            limit = int(raw_lim) if raw_lim else 0
        except ValueError:
            raise ElabError(f"limit 必须是整数：{raw_lim!r}")
        return h._json(serialmon_mod.tail_bytes(
            after, direction=direction, limit=limit))
    if path == "/api/serial/monitor/decoded":
        # 会话级共享解码帧（P3.5）：agent 与面板波页读**同一份**。
        # 解码本身在读线程里增量完成（RX 批次入环时），这里只做增量读回。
        raw = (qs.get("after") or ["0"])[0]
        try:
            after = int(raw)
        except ValueError:
            raise ElabError(f"after 必须是整数：{raw!r}")
        return h._json(serialmon_mod.tail_decoded(after))
    if path == "/api/serial/control":
        # 复位预设清单 + 当前 DTR/RTS 已知状态 + 序列进度（M1）。
        project = (qs.get("project") or [""])[0]
        return h._json(serialcontrol_mod.state(h.cfg, project))
    if path == "/api/serial/log-profiles":
        # 可用日志 profile 清单（包 + 工程覆盖；M6）。空列表不是错误：
        # 说明这颗芯片/工程还没人提供诊断数据。
        project = (qs.get("project") or [""])[0]
        try:
            profiles = ld.load_profiles(h.cfg, project)
        except ValueError as exc:
            raise ElabError(str(exc))
        return h._json({"profiles": profiles})
    if path == "/api/modbus/meta":
        # Modbus 词汇表（功能码/类型/限制/异常码）—— 内核唯一事实源
        return h._json(MODBUS_META)
    if path == "/api/modbus/state":
        # 面板 300ms 轮询这一个端点：引擎快照 + 增量事务 + 当前映射。
        eng = modbusmaster_mod.current()
        project = (qs.get("project") or [""])[0]
        try:
            after = int((qs.get("after") or ["0"])[0])
        except ValueError:
            raise ElabError("after 必须是整数")
        out = {"ok": True,
               "engine": eng.snapshot() if eng else {"active": False}}
        if eng:
            out["transactions"] = eng.transactions(after)["transactions"]
            out["map"] = eng.map
        else:
            out["transactions"] = []
            if project:
                try:
                    out["map"] = modbusmaster_mod.load_map(h.cfg, project)
                except ValueError as exc:
                    raise ElabError(str(exc))
        return h._json(out)
    if path == "/api/modbus/transactions":
        eng = _require_modbus()
        try:
            after = int((qs.get("after") or ["0"])[0])
        except ValueError:
            raise ElabError("after 必须是整数")
        return h._json(eng.transactions(after))
    if path == "/api/modbus/map":
        project = (qs.get("project") or [""])[0]
        eng = modbusmaster_mod.current()
        if eng is not None:
            # 引擎在跑就以引擎里的为准：它可能刚保存过还没落盘的新映射
            return h._json({"ok": True, "map": eng.map,
                               "source": "engine"})
        if not project:
            raise ElabError("map 需要 project 参数")
        try:
            raw = modbusmaster_mod.load_map(h.cfg, project)
        except ValueError as exc:
            raise ElabError(str(exc))
        return h._json({"ok": True, "map": raw, "source": "file"})
    if path == "/api/serial/frames":
        # 帧协议配置（M5）：读全部 profile + rev + warnings。
        project = (qs.get("project") or [""])[0]
        if not project:
            raise ElabError("frames 需要 project 参数")
        return h._json(sf.load(h.cfg, project))
    if path == "/api/serial/frames/templates":
        # 只读模板库（P1）：协议数据，不是可执行代码。
        return h._json(sf.list_templates(h.cfg))
    if path == "/api/serial/monitor/stats":
        # P3.1 帧级健康度：帧率 / 到达间隔 / 错误分类 / 每 variant 计数。
        raw_w = (qs.get("window_s") or ["10"])[0]
        try:
            window_s = float(raw_w)
        except ValueError:
            raise ElabError(f"window_s 必须是数字：{raw_w!r}")
        return h._json(serialmon_mod.frame_stats(window_s))
    if path == "/api/serial/monitor/issues":
        # 结构化 issue 增量读回（panic / hardfault / 复位；M6）。
        raw = (qs.get("after") or ["0"])[0]
        try:
            after = int(raw)
        except ValueError:
            raise ElabError(f"after 必须是整数：{raw!r}")
        return h._json(serialmon_mod.tail_issues(after))
    if path == "/api/serial/monitor/poll":
        # 盘点 #4：串口轮询合并端点 —— 一次带回四路增量
        # （行 tail + 字节批次 + 结构化 issue + 共享解码帧）。
        # ★ 动机：前端每 500ms 并发 2~4 个请求，HTTP/1.1 同域只有
        #   6 个连接，稳态就占掉 2~4 个；合并后 1 个，且四路数据
        #   天然同拍（同一时刻的快照），前端不再需要自己拼时序。
        #   每路游标独立传（line/byte/issue/dec），缺省 0 = 从头追。
        def _cur(name: str) -> int:
            raw = (qs.get(name) or [""])[0]
            if not raw:
                return 0
            try:
                return int(raw)
            except ValueError:
                raise ElabError(f"{name} 必须是整数：{raw!r}")
        direction = (qs.get("dir") or ["all"])[0]
        if direction not in ("all", "rx", "tx"):
            raise ElabError(f"dir 必须是 all/rx/tx：{direction!r}")
        lt = serialmon_mod.tail(_cur("line"))
        out: dict = {"line": lt,
                     "bytes": serialmon_mod.tail_bytes(
                         _cur("byte"), direction=direction, limit=0)}
        # 与旧前端逻辑同规则：profile / decoder 开着才拉对应增量
        if (lt.get("issues") or {}).get("profile"):
            out["issues"] = serialmon_mod.tail_issues(_cur("issue"))
        decoder = (lt.get("decode") or {}).get("decoder") or ""
        if decoder:
            out["decoded"] = serialmon_mod.tail_decoded(_cur("dec"))
        return h._json(out)
    if path == "/api/serial/monitor":
        # 常驻监视会话的增量读回（tail）。「after」= 浏览器已收到的最大 seq。
        raw = (qs.get("after") or ["0"])[0]
        try:
            after = int(raw)
        except ValueError:
            raise ElabError(f"after 必须是整数：{raw!r}")
        return h._json(serialmon_mod.tail(after))
    return False


def handle_post(h, u, qs):
    if u.path == "/api/serial/widgets":
        # 组件增删（M1）：与 agent/人共用一套 rev 乐观锁。
        # ★ 人的修改永远优先：rev 不匹配 → 409 + 最新内容，写者必须重读。
        body = h._body()
        action = str(body.get("action") or "")
        project = str(body.get("project") or "")
        if not project:
            return h._err(400, "缺少 project")
        if action not in ("save", "delete"):
            return h._err(400, f"action 必须是 save/delete：{action!r}")
        try:
            rev = int(body.get("rev"))
        except (TypeError, ValueError):
            return h._err(400, f"rev 必须是整数：{body.get('rev')!r}")
        if action == "save":
            widget = body.get("widget")
            if not isinstance(widget, dict):
                return h._err(400, "save 需要 widget 对象")
            res = serialwidgets_mod.save(h.cfg, project, widget, rev)
        else:
            wid = str(body.get("id") or "")
            if not wid:
                return h._err(400, "delete 需要 id")
            res = serialwidgets_mod.delete(h.cfg, project, wid, rev)
        return h._json(res, 409 if res.get("conflict") else 200)
    if u.path == "/api/serial/widget/run":
        # 执行一个组件：dry_run 只渲染（不占口），否则走现有写穿/往返通道。
        body = h._body()
        project = str(body.get("project") or "")
        wid = str(body.get("id") or "")
        if not project or not wid:
            return h._err(400, "缺少 project 或 id")
        dry = bool(body.get("dry_run"))
        if not dry:
            busy = _serial_blocker(h.mgr)
            if busy:
                return h._err(409, (
                    f"有闭环正在跑（run={busy.get('run')}，"
                    f"步骤 {'/'.join(busy.get('steps') or [])}）—— "
                    f"它要用串口/SWD（约束 N5）。等它跑完，或先取消它。"))
        res = serialwidgets_mod.run(
            h.cfg, project, wid, body.get("params"),
            dry_run=dry, repeat=bool(body.get("repeat")),
            confirm=bool(body.get("confirm")),
            actor=str(body.get("actor") or "human"),
            journal=serialconsole_mod.journal_for(h.cfg, project=project))
        if res.get("stage") == "busy":
            return h._err(409, res.get("error") or "已有组件任务在跑")
        if res.get("stage") == "validate":
            return h._err(400, res.get("error") or "组件请求不合法")
        # "执行了但写失败"沿用 200 + ok:false（与 POST /api/serial 同一条纪律）
        return h._json(res, 200)
    if u.path == "/api/serial/widget/cancel":
        body = h._body()
        job_id = str(body.get("id") or "")
        if not job_id:
            return h._err(400, "缺少 id")
        res = serialwidgets_mod.cancel(job_id)
        return h._json(res, 200 if res.get("ok") else 404)
    if u.path == "/api/serial/widget/pause":
        # 暂停/恢复（M2）：只改标志位。人的暂停优先于 agent ——
        # agent 的周期任务被人按停后，必须人再按恢复（不自动续跑）。
        body = h._body()
        job_id = str(body.get("id") or "")
        if not job_id:
            return h._err(400, "缺少 id")
        paused = bool(body.get("paused", True))
        res = serialwidgets_mod.set_paused(job_id, paused)
        return h._json(res, 200 if res.get("ok") else 404)
    if u.path == "/api/serial/loop":
        # 发送框的「定时发送」：自由帧 → 内核瞬态任务（不落盘）。
        # ★ 与组件周期共用同一个任务引擎/单任务锁：两条发送入口
        #   不允许出现两套周期语义（固定节拍、skipped、暂停/取消、10min 上限）。
        body = h._body()
        project = str(body.get("project") or "")
        data = body.get("data")
        if not isinstance(data, str) or not data.strip():
            return h._err(400, "缺少 data（要发送的内容）")
        dry = bool(body.get("dry_run"))
        if not dry:
            busy = _serial_blocker(h.mgr)
            if busy:
                return h._err(409, (
                    f"有闭环正在跑（run={busy.get('run')}，"
                    f"步骤 {'/'.join(busy.get('steps') or [])}）—— "
                    f"它要用串口/SWD（约束 N5）。等它跑完，或先取消它。"))
        res = serialwidgets_mod.run_raw(
            h.cfg, project, data,
            newline=bool(body.get("newline", True)),
            hex_=bool(body.get("hex")), format=body.get("format"),
            eol=body.get("eol"), encoding=body.get("encoding"),
            interval_ms=body.get("interval_ms"),
            count=body.get("count") or 0,
            actor=str(body.get("actor") or "human"),
            journal=serialconsole_mod.journal_for(h.cfg, project=project),
            dry_run=dry)
        if res.get("stage") == "busy":
            return h._err(409, res.get("error") or "已有周期任务在跑")
        if res.get("stage") == "validate":
            return h._err(400, res.get("error") or "定时发送请求不合法")
        return h._json(res, 200)
    if u.path == "/api/serial":
        # 手写通道（M3-b）：写一条出去、收一小段回显，一次请求内闭环。
        # ★ 返回 **200 + ok:false**（而不是 5xx）承载"执行了但没成功"：
        #   前端要渲染的字段在两种结果下完全一样（port/baud/error），
        #   用 5xx 只会让它走异常分支、丢掉这些结构化信息。
        #   400 只留给"请求本身不合法"，409 留给"串口正被闭环占着"。
        body = h._body()
        data = body.get("data")
        if not isinstance(data, str) or not data.strip():
            return h._err(400, "缺少 data（要发送的内容）")
        project = body.get("project") or ""
        fmt = body.get("format")
        eol = body.get("eol")
        encoding = body.get("encoding")
        # ★ 预览与发送共用 resolve_payload：前端只看结果，不在客户端
        #   自己编码 —— 否则"预览的字节"和"真发出去的字节"会漂移。
        #   预览必须在 blocker 之前：它不碰串口，闭环跑着也能先编辑。
        payload, rfmt, reol, perr = serialterm_mod.resolve_payload(
            data, newline=bool(body.get("newline", True)),
            hex_=bool(body.get("hex")), format=fmt, eol=eol,
            encoding=encoding)
        if body.get("dry_run"):
            if perr:
                return h._json({"ok": False, "dry_run": True,
                                   "error": perr, "format": rfmt,
                                   "bytes": 0, "preview_hex": ""}, 200)
            return h._json({
                "ok": True, "dry_run": True, "bytes": len(payload),
                "preview_hex": payload.hex(" ").upper(),
                "format": rfmt, "eol_key": reol,
                "eol": serialterm_mod.EOLS.get(reol, b"").decode("ascii"),
            }, 200)
        busy = _serial_blocker(h.mgr)
        if busy:
            return h._err(409, (
                f"有闭环正在跑（run={busy.get('run')}，"
                f"步骤 {'/'.join(busy.get('steps') or [])}）—— 它要用串口/SWD，"
                f"手写通道已在让路（约束 N5）。等它跑完，或先取消它。"))
        # ★ 监视会话开着 → 写通道路由**进会话**（同一个口直接 write，
        #   不重开口 —— 不破坏 N5）。回显不再由本请求带回：读线程会把它
        #   送进实时流。TX 照旧落 console 留档（"我敲过这一行"不因失败消失）。
        if perr:
            return h._json({"ok": False, "error": perr, "port": "",
                               "baud": 0, "written": 0, "echoed": [],
                               "via": "monitor"}, 200)
        n = serialmon_mod.write_through(payload)
        if n is not None:
            via_res = {"ok": n == len(payload), "via": "monitor",
                       "written": n, "payload_bytes": len(payload),
                       "format": rfmt,
                       "eol": serialterm_mod.EOLS.get(
                           reol, b"").decode("ascii"),
                       "echoed": [], "error": None if n == len(payload)
                       else "只写出部分字节（口可能已失效，试试关闭重开监视）"}
            st_ok = serialmon_mod.status()
            via_res.update({"port": st_ok.get("port") or "",
                            "baud": st_ok.get("baud") or 0,
                            "line_text": st_ok.get("line_text") or "",
                            "line": st_ok.get("line")})
            try:
                # TX 照旧落 console 留档（同一条 journal_for 路径，带 project）
                serialconsole_mod.journal_for(h.cfg, project=project)({
                    "dir": "tx", "text": data, "ok": via_res["ok"],
                    "written": n, "payload_bytes": len(payload),
                    "via": "monitor", "format": rfmt,
                    "port": via_res["port"], "baud": via_res["baud"],
                    "error": via_res["error"]})
            except Exception as exc:               # noqa: BLE001
                h.log_message("serialmon journal: %s", exc)
            return h._json(via_res, 200)
        res = serialterm_mod.roundtrip(
            h.cfg,
            project,
            data=data,
            port=body.get("port") or "",
            baud=body.get("baud") or 0,
            read_ms=body.get("read_ms") or serialterm_mod.DEFAULT_READ_MS,
            newline=bool(body.get("newline", True)),
            hex_=bool(body.get("hex")),
            format=fmt, eol=eol, encoding=encoding,
            line=body.get("line"),
            transport=str(body.get("transport") or ""),
            net=body.get("net"),
            # ★ 落盘：写通道**不属于任何 run**，所以落到独立的 console 日志，
            #   绝不写进 run 的 .jsonl（那会破坏单写者模型，约束 C17）。
            journal=serialconsole_mod.journal_for(h.cfg, project=project),
        )
        return h._json(res, 200)
    if u.path == "/api/serial/control":
        # DTR/RTS：{dtr?, rts?} 立即生效；{preset} 跑 pack.yaml 里的序列；
        # {cancel: true} 取消序列。物理线路动作 —— 不进字节环、不算 TX。
        body = h._body()
        if body.get("cancel"):
            return h._json(serialcontrol_mod.cancel(), 200)
        # 动线会打断烧录/调试时序（尤其 RTS/DTR 参与 BOOT 同步）—— 先拦。
        busy = _serial_blocker(h.mgr)
        if busy:
            return h._err(409, (
                f"有闭环正在跑（run={busy.get('run')}）—— "
                f"复位/动 DTR/RTS 会打断烧录或调试，等它跑完或先取消它。"))
        if body.get("preset"):
            res = serialcontrol_mod.run_preset(
                h.cfg, str(body.get("project") or ""),
                str(body["preset"]), port=str(body.get("port") or ""),
                line=body.get("line"),
                actor=str(body.get("actor") or "human"))
            return h._json(res, 200 if res.get("ok") else 400)
        res = serialcontrol_mod.apply(
            h.cfg, str(body.get("project") or ""),
            dtr=_bool_or_none(body.get("dtr")),
            rts=_bool_or_none(body.get("rts")),
            port=str(body.get("port") or ""), line=body.get("line"),
            actor=str(body.get("actor") or "human"))
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/monitor":
        # 常驻串口监视（「打开监视 / 关闭监视 / 清空 / 暂停」）。
        # open 前先查闭环占用：与手写通道同款 409 指名道姓 ——
        # 不让用户对着 ERROR_ACCESS_DENIED 拔插。
        body = h._body()
        action = body.get("action") or ""
        if action == "open":
            busy = _serial_blocker(h.mgr)
            if busy:
                return h._err(409, (
                    f"有闭环正在跑（run={busy.get('run')}，"
                    f"步骤 {'/'.join(busy.get('steps') or [])}）—— "
                    f"它要用串口/SWD（约束 N5）。等它跑完，或先取消它。"))
            try:
                lease_s = float(body.get("lease_s") or 0)
            except (TypeError, ValueError):
                raise ElabError(f"lease_s 必须是数字：{body.get('lease_s')!r}")
            res = serialmon_mod.open_session(
                h.cfg, body.get("project") or "",
                port=body.get("port") or "",
                baud=body.get("baud") or 0,
                opened_by=str(body.get("actor") or "human"),
                lease_s=lease_s,
                allow_agent_write=bool(body.get("allow_agent_write")),
                decoder=str(body.get("decoder") or ""),
                line=body.get("line"),
                reconnect=body.get("reconnect"),
                log_profile=str(body.get("log_profile") or ""),
                transport=str(body.get("transport") or ""),
                net=body.get("net"))
            return h._json(res, 200)
        if action == "close":
            return h._json(serialmon_mod.close_session(), 200)
        if action == "decoder":
            # 会话级解码器（P3.5）：agent 选定 → 人面板波页自动同图。
            # '' = 关闭共享解码（面板回退到本地无状态解码）。
            snap = serialmon_mod.status()
            res = serialmon_mod.set_decoder(
                str(body.get("decoder") or ""),
                actor=str(body.get("actor") or "human"),
                cfg=h.cfg,
                project=str(body.get("project") or snap.get("project") or ""))
            return h._json(res, 200 if res.get("ok") else 400)
        if action == "clear":
            return h._json(serialmon_mod.clear(), 200)
        if action == "pause":
            return h._json(serialmon_mod.set_paused(True), 200)
        if action == "resume":
            return h._json(serialmon_mod.set_paused(False), 200)
        if action == "restore":
            # M2：闭环让位后手动「立即恢复」（不必等 run 结束/自动回归）
            return h._json(serialmon_mod.resume(), 200)
        if action == "log_profile":
            # M6：会话级日志诊断 profile（'' = 关闭诊断）。
            snap = serialmon_mod.status()
            res = serialmon_mod.set_log_profile(
                h.cfg, str(body.get("project") or snap.get("project") or ""),
                str(body.get("profile") or ""))
            return h._json(res, 200 if res.get("ok") else 400)
        raise ElabError(
            f"action 必须是 open/close/decoder/clear/pause/resume/restore/"
            f"log_profile：{action!r}")
    if u.path == "/api/modbus/open":
        # 复用已打开的串口会话（不新开口、不复制读线程）；没开就先开。
        # probe=true（默认）顺手读一次设备 ID：把"上电首帧撞心跳"这类
        # 一次性抖动吸收在 open 里，面板打开即是绿的。
        body = h._body()
        project = str(body.get("project") or "")
        if not project:
            raise ElabError("modbus open 需要 project")
        sess = serialmon_mod.current()
        if sess is None:
            busy = _serial_blocker(h.mgr)
            if busy:
                return h._err(409, (
                    f"有闭环正在跑（run={busy.get('run')}，"
                    f"步骤 {'/'.join(busy.get('steps') or [])}）—— "
                    f"它要用串口/SWD，等它跑完或先取消它。"))
            res = serialmon_mod.open_session(
                h.cfg, project,
                port=str(body.get("port") or ""),
                baud=_as_int(body, "baud", 0),
                opened_by=str(body.get("actor") or "human"),
                allow_agent_write=bool(body.get("allow_agent_write")),
                line=body.get("line"))
            if not res.get("ok"):
                return h._json(res, 400)
            sess = serialmon_mod.current()
        if sess is None:
            raise ElabError("串口会话没有打开成功（端口可能被别的程序占用）")
        eng = modbusmaster_mod.open_engine(
            sess, cfg=h.cfg, project=project,
            unit_id=_as_int(body, "unit_id", 1),
            timeout_ms=_as_int(body, "timeout_ms",
                               modbusmaster_mod.DEFAULT_TIMEOUT_MS),
            retries=_as_int(body, "retries",
                            modbusmaster_mod.DEFAULT_RETRIES))
        out = {"ok": True, "engine": eng.snapshot(), "map": eng.map}
        if body.get("probe", True):
            try:
                out["probe"] = _mb_call(eng.read,
                                        modbus_mod.FC_READ_HOLDING, 0, 2)
            except ElabError as exc:
                out["probe"] = {"ok": False,
                                "error": {"kind": "malformed",
                                          "detail": str(exc)}}
        return h._json(out)
    if u.path == "/api/modbus/close":
        return h._json(modbusmaster_mod.close_engine("面板关闭"))
    if u.path == "/api/modbus/start":
        body = h._body()
        eng = _require_modbus()
        groups = body.get("groups")
        if groups is not None and not isinstance(groups, list):
            raise ElabError("groups 必须是数组（或省略）")
        try:
            res = eng.start_polling(groups)
        except ValueError as exc:
            raise ElabError(str(exc)) from exc
        return h._json({**res, "engine": eng.snapshot()})
    if u.path == "/api/modbus/stop":
        eng = _require_modbus()
        res = eng.stop_polling()
        return h._json({**res, "engine": eng.snapshot()})
    if u.path == "/api/modbus/read":
        body = h._body()
        eng = _require_modbus()
        fc = _as_int(body, "fc", modbus_mod.FC_READ_HOLDING)
        addr = _as_int(body, "address", 0)
        count = _as_int(body, "count", 1)
        unit = body.get("unit_id")
        res = _mb_call(eng.read, fc, addr, count,
                       unit_id=(None if unit is None else int(unit)))
        return h._json(res)
    if u.path == "/api/modbus/write":
        # 写 = 会动物理世界（LED/继电器/变频器频率）。必须显式 confirm。
        body = h._body()
        if not body.get("confirm"):
            raise ElabError("写操作需要 confirm=true（面板会先弹确认框）")
        eng = _require_modbus()
        fc = _as_int(body, "fc", modbus_mod.FC_WRITE_REGISTER)
        addr = _as_int(body, "address", 0)
        if fc in (modbus_mod.FC_WRITE_COIL, modbus_mod.FC_WRITE_REGISTER):
            values = [_as_int(body, "value", 0)]
        else:
            values = body.get("values")
            if not isinstance(values, list) or not values:
                raise ElabError("批量写需要 values 数组")
        res = _mb_call(eng.write, fc, addr, values,
                       broadcast=bool(body.get("broadcast")))
        return h._json(res)
    if u.path == "/api/modbus/map":
        body = h._body()
        project = str(body.get("project") or "")
        if not project:
            raise ElabError("保存映射需要 project")
        raw = body.get("map")
        if not isinstance(raw, dict):
            raise ElabError("map 必须是对象")
        rev = body.get("rev")
        try:
            saved = modbusmaster_mod.save_map(
                h.cfg, project, raw,
                None if rev is None else int(rev))
        except ValueError as exc:
            msg = str(exc)
            return h._err(409 if "rev" in msg else 400, msg)
        eng = modbusmaster_mod.current()
        if eng is not None:
            eng.apply_map(saved)
        return h._json({"ok": True, "map": saved})
    if u.path == "/api/serial/frames":
        # 保存 / 删除一个 profile。rev 不匹配 → 409（不覆盖别人的改动）。
        body = h._body()
        project = str(body.get("project") or "")
        if not project:
            raise ElabError("frames 需要 project")
        try:
            rev = int(body.get("rev") or 0)
        except (TypeError, ValueError):
            raise ElabError("rev 必须是整数")
        action = str(body.get("action") or "save").strip().lower()
        if action == "save":
            res = sf.save(h.cfg, project, rev, body.get("profile") or {})
        elif action == "delete":
            res = sf.delete(h.cfg, project, rev,
                            str(body.get("id") or ""))
        else:
            raise ElabError("action 必须是 save/delete")
        if res.get("conflict"):
            return h._json(res, 409)
        if res.get("ok"):
            pid = (str(body.get("id") or "") if action == "delete"
                   else str((body.get("profile") or {}).get("id") or ""))
            if pid:
                _sync_serial_decoder(h.cfg, project, pid,
                                     action == "delete")
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/frames/templates/import":
        # 模板 → 工程 profile（仍走 save：rev 乐观锁，不覆盖冲突）。
        body = h._body()
        project = str(body.get("project") or "")
        if not project:
            raise ElabError("模板导入需要 project")
        try:
            rev = int(body.get("rev") or 0)
        except (TypeError, ValueError):
            raise ElabError("rev 必须是整数")
        res = sf.import_template(
            h.cfg, project, rev,
            str(body.get("template") or ""),
            str(body.get("new_id") or ""))
        if res.get("conflict"):
            return h._json(res, 409)
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/frames/encode":
        # 反向组帧（P1）：字段值 → 字节；支持未保存草稿。
        body = h._body()
        prof = None
        if isinstance(body.get("profile"), dict):
            try:
                prof = sf.normalize_profile(body["profile"])
            except ValueError as exc:
                raise ElabError(str(exc))
        else:
            project = str(body.get("project") or "")
            pid = str(body.get("id") or "")
            if not project or not pid:
                raise ElabError("encode 需要 project+id 或 profile 草稿")
            prof = sf.get(h.cfg, project, pid)
            if prof is None:
                raise ElabError(f"未找到帧协议 profile：{pid!r}")
        res = serialcodec_mod.encode(
            prof, body.get("values") or {}, body.get("variant"),
            body.get("length_value"))
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/frames/learn":
        # P4.1：从抓包学一个**草稿** profile（不落盘）。
        # 保存仍走 /api/serial/frames 的 rev 乐观锁 —— 学习不会偷偷改配置。
        body = h._body()
        hints = body.get("hints") or {}
        if not isinstance(hints, dict):
            raise ElabError("hints 必须是对象")
        try:
            fc = int(body.get("field_count") or 0)
        except (TypeError, ValueError):
            raise ElabError("field_count 必须是整数")
        try:
            res = framelearn_mod.learn(
                frames=body.get("frames"),
                hex=str(body.get("hex") or ""),
                b64=str(body.get("b64") or ""),
                capture=_capture_text(h.cfg, body.get("capture")),
                name=str(body.get("name") or "learned"),
                label=str(body.get("label") or ""),
                hints=hints, field_count=fc)
        except ValueError as exc:
            raise ElabError(str(exc))
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/frames/verify":
        # P4.2：解 → 编 → 再解，逐帧比对（证明规则可逆）。
        body = h._body()
        prof = _frame_profile_of(h.cfg, body)
        try:
            res = framelearn_mod.verify(
                prof, body.get("vectors"),
                hex=str(body.get("hex") or ""),
                b64=str(body.get("b64") or ""),
                capture=_capture_text(h.cfg, body.get("capture")))
        except ValueError as exc:
            raise ElabError(str(exc))
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/frames/auto":
        # P4.3：给一段字节，在工程 profile 里选一个最像的（只推荐）。
        body = h._body()
        data = _frame_bytes_of(body, h.cfg)
        if not data:
            raise ElabError("auto 需要 hex / b64 / capture 之一")
        explicit = body.get("profiles")
        if isinstance(explicit, list) and explicit:
            profiles = []
            for i, raw in enumerate(explicit):
                try:
                    profiles.append(sf.normalize_profile(raw))
                except ValueError as exc:
                    raise ElabError(f"profiles[{i}]：{exc}")
        else:
            project = str(body.get("project") or "")
            if not project:
                raise ElabError("auto 需要 project，或直接传 profiles")
            profiles = sf.load(h.cfg, project).get("profiles") or []
            if not profiles:
                raise ElabError(f"工程 {project} 还没有帧协议 profile")
        res = framelearn_mod.auto(profiles, data)
        if res.get("ok") and body.get("apply"):
            pid = res["decoder"].split(":", 1)[1]
            applied = serialmon_mod.set_decoder(
                res["decoder"], actor="agent", cfg=h.cfg,
                project=str(body.get("project") or ""))
            res["applied"] = applied.get("ok", False)
            if not applied.get("ok"):
                res["apply_error"] = applied.get("error")
            del pid
        return h._json(res, 200 if res.get("ok") else 409)
    if u.path == "/api/serial/frames/codegen":
        # P4.4：profile → C 头文件。默认只返回文本，写盘需 write=true。
        body = h._body()
        prof = _frame_profile_of(h.cfg, body)
        res = framelearn_mod.codegen(prof, guard=str(body.get("guard") or ""))
        if body.get("write"):
            target = Path(h.cfg.root) / "generated" / f"{prof['id']}.h"
            if target.exists() and not body.get("overwrite"):
                res["ok"] = False
                res["error"] = f"{target} 已存在；确需覆盖请带 overwrite=true"
                return h._json(res, 409)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(res["text"], encoding="utf-8")
            except OSError as exc:
                raise ElabError(f"写文件失败：{exc}")
            res["written"] = str(target)
        return h._json(res)
    if u.path == "/api/serial/frames/preview":
        # 帧解析预览（M5）：不需要开串口/会话，改配置立即验证。
        body = h._body()
        project = str(body.get("project") or "")
        pid = str(body.get("id") or "")
        if isinstance(body.get("profile"), dict):
            # 未保存草稿：校验后直接预览（Agent 先验证再落盘）
            try:
                prof = sf.normalize_profile(body["profile"])
            except ValueError as exc:
                raise ElabError(str(exc))
            pid = prof["id"]
        else:
            prof = sf.get(h.cfg, project, pid) if pid else None
        if prof is None:
            raise ElabError(f"未找到帧协议 profile：{pid!r}")
        if body.get("b64") or body.get("data_b64"):
            import base64 as _b64mod
            try:
                data = _b64mod.b64decode(
                    str(body.get("b64") or body.get("data_b64")))
            except Exception as exc:               # noqa: BLE001
                raise ElabError(f"b64 不是合法 base64：{exc}")
        elif body.get("hex"):
            try:
                data = bytes.fromhex("".join(str(body["hex"]).split()))
            except ValueError as exc:
                raise ElabError(f"hex 不是合法十六进制：{exc}")
        else:
            data = b""
        res = serialcodec_mod.decode(data, f"profile:{pid}",
                                     profile=prof)
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/symbolize":
        # M6：地址 → 函数/文件:行（addr2line）。失败如实返回，不编造。
        body = h._body()
        project = str(body.get("project") or "")
        addrs = body.get("addrs") or []
        if not isinstance(addrs, list):
            raise ElabError("addrs 必须是数组")
        pid = str(body.get("profile") or "")
        prof = ld.get_profile(h.cfg, project, pid) if pid else None
        res = ld.symbolize(h.cfg, project, addrs, prof)
        return h._json(res, 200 if res.get("ok") else 400)
    if u.path == "/api/serial/capture":
        # K5 显式录制：把**字节**录成有界 JSONL（不录文本投影，可回放）。
        # start/stop 幂等；status 走 capabilities.serial.monitor.capture。
        body = h._body()
        action = body.get("action") or "start"
        if action == "start":
            raw_mb = body.get("max_bytes")
            try:
                max_bytes = int(raw_mb or 0)
            except (TypeError, ValueError):
                raise ElabError(f"max_bytes 必须是整数：{raw_mb!r}")
            try:
                pre_bytes = int(body.get("pre_bytes") or 0)
            except (TypeError, ValueError):
                raise ElabError("pre_bytes 必须是整数")
            trigger = body.get("trigger")
            if trigger is not None and not isinstance(trigger, dict):
                raise ElabError("trigger 必须是对象")
            res = serialmon_mod.capture_start(
                h.cfg, project=body.get("project") or "",
                max_bytes=max_bytes, trigger=trigger,
                pre_bytes=pre_bytes)
            return h._json(res, 200 if res.get("ok") else 400)
        if action == "stop":
            return h._json(serialmon_mod.capture_stop(), 200)
        if action == "status":
            return h._json({"ok": True,
                               **serialmon_mod.status()}, 200)
        raise ElabError(f"action 必须是 start/stop/status：{action!r}")
    if u.path == "/api/serial/decode":
        # P3 无状态协议解码：给一段字节（hex 或 base64），返回通道帧。
        # ★ 不解码"会话状态"：半帧由调用方拼下一批（pending 原样带回）。
        body = h._body()
        decoder = str(body.get("decoder") or "justfloat")
        if body.get("data_b64"):
            try:
                data = base64.b64decode(str(body.get("data_b64")))
            except Exception as exc:               # noqa: BLE001
                raise ElabError(f"data_b64 不是合法 base64：{exc}")
        elif body.get("hex"):
            try:
                data = bytes.fromhex(
                    "".join(str(body.get("hex")).split()))
            except ValueError as exc:
                raise ElabError(f"hex 不是合法十六进制：{exc}")
        else:
            data = b""
        prof = None
        pid = str(body.get("profile") or "")
        if decoder == "profile" or decoder.startswith("profile:"):
            if isinstance(body.get("profile"), dict):
                # 草稿 profile（Agent 不落盘也能解码）
                try:
                    prof = sf.normalize_profile(body["profile"])
                except ValueError as exc:
                    raise ElabError(str(exc))
                pid = prof["id"]
            else:
                if not pid and ":" in decoder:
                    pid = decoder.split(":", 1)[1]
                prof = sf.get(h.cfg, str(body.get("project") or ""), pid) \
                    if pid else None
        res = serialcodec_mod.decode(data, decoder,
                                     channels=body.get("channels"),
                                     profile=prof)
        return h._json(res, 200 if res.get("ok") else 400)
    return False
