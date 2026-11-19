"""cockpit.api.routes.sse —— SSE 推送。"""

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


def sse_debug(h, debug_session: str = ""):
    """调试事件流（独立信封 §6.4）。

    ★ 与 run SSE 分开：``ElabEvent`` 的 ``run`` 字段是必填的，硬塞会把
      "调试会话"伪装成"某个 run"，工具层与前端都得靠猜。
    ★ 多会话：``?debug_session=<id>`` 只推该会话的事件（信封 ``session`` 双保险）。
    """
    if not h.sse_sem.acquire(blocking=False):
        return h._err(503, f"SSE 连接数已达上限（{MAX_SSE_CONNS}）")
    q = h.dbg.subscribe(debug_session)
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache, no-transform")
        h.send_header("Connection", "keep-alive")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        h._sent_headers = True
        last_beat = time.time()
        while True:
            try:
                e = q.get(timeout=PROC_BATCH_S)
            except queue.Empty:
                if (time.time() - last_beat) >= SSE_HEARTBEAT_S:
                    send_chunk(h, ": ping\n\n")
                    last_beat = time.time()
                continue
            sse_write(h, e)
            last_beat = time.time()
    except ConnectionError:
        return
    except Exception as exc:                       # noqa: BLE001
        sys.stderr.write(f"[cockpit] 调试 SSE 异常：{exc!r}\n")
    finally:
        h.dbg.unsubscribe(q)
        h.sse_sem.release()


def sse(h, qs: dict):
    run_id = (qs.get("run") or [""])[0]
    after = int((qs.get("from") or ["0"])[0])
    # 断线续传：浏览器重连会自动带 Last-Event-ID（§17.2）
    leid = h.headers.get("Last-Event-ID")
    if leid:
        try:
            after = max(after, int(leid))
        except ValueError:
            pass
    if not run_id:
        return h._err(400, "缺少 run")

    if not h.sse_sem.acquire(blocking=False):
        return h._err(503, f"SSE 连接数已达上限（{MAX_SSE_CONNS}）")

    ar = h.mgr.active(run_id)
    sub = h.mgr.subscribe(run_id)          # 非活动 run 也能拿（subscribe 会返回空队列）
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache, no-transform")
        h.send_header("Connection", "keep-alive")
        h.send_header("X-Accel-Buffering", "no")
        # ★ 用 chunked 而不是"靠关连接断句"：HTTP/1.1 下没有 Content-Length
        #   就必须显式分块，否则浏览器会一直等自己的缓冲，日志"不动"。
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        h._sent_headers = True

        last_sent = after
        last_beat = time.time()
        pending: list[str] = []
        pending_step = ""
        last_flush = time.time()

        def chunk(data: str) -> None:
            send_chunk(h, data)

        def flush_proc() -> None:
            nonlocal pending, last_flush
            if pending:
                sse_frame(h, "proc/stdout-batch",
                                {"topic": "proc/stdout-batch", "run": run_id,
                                 "seq": last_sent, "ts": time.time(),
                                 "actor": "agent", "step": pending_step,
                                 "lines": pending},
                                eid=last_sent)
                pending = []
            last_flush = time.time()

        # ① 先把 after_seq 之后的存量重放掉（覆盖"断线期间"与"run 已结束"两种情况）
        for e in ev_mod.read_run(h.mgr.runs_dir, run_id, after_seq=after):
            if e.get("seq", 0) <= last_sent:
                continue
            sse_write(h, e)
            last_sent = e.get("seq", last_sent)

        # ② 已结束（或未知）的 run：重放完就收尾。
        #    ★ 无论哪种情况都**必须**发出 stream/closed + chunked 终止块：
        #      否则客户端只看到"连接被关"，分不清"run 结束了"和"服务端出错"，
        #      而 EventSource 还会傻傻地不停重连。
        if ar is None or not ar.alive:
            flush_proc()
            if ar is None and not run_exists(h, run_id):
                sse_frame(h, "stream/closed",
                                {"run": run_id, "reason": "unknown-run"})
            else:
                sse_frame(h, "stream/closed",
                                {"run": run_id, "reason": "run-finished"})
            chunk("0\r\n\r\n")
            return

        # ③ 活动 run：跟读队列
        heartbeats = 0
        while True:
            try:
                e = sub.get(timeout=PROC_BATCH_S)
            except queue.Empty:
                now = time.time()
                if pending and (now - last_flush) >= PROC_BATCH_S:
                    flush_proc()
                if (now - last_beat) >= SSE_HEARTBEAT_S:
                    chunk(": ping\n\n")             # §17.3 注释行心跳
                    last_beat = now
                    heartbeats += 1
                    if heartbeats > 8 and not h.mgr.active(run_id):
                        break
                continue

            topic = e.get("topic", "")
            if topic == "stream/closed":
                flush_proc()
                sse_frame(h, "stream/closed",
                                {"run": run_id, "reason": e.get("reason", ""),
                                 "rc": e.get("rc")})
                break

            # ★ `stream/overrun` 必须在**去重之前**处理，理由有两条：
            #   ① 它的 topic 前缀是 `stream/`（非 `run/`）→ `is_activity()` 为真，
            #      会落进下面的 activity 分支被当成"一行普通输出"。而它没有
            #      `line` 字段 → 界面收到一个**空字符串行**，什么提示都没有。
            #      契约里"补一条 stream/overrun 让 UI 提示日志已截断"就是被这里吞掉的。
            #   ② 它带的是 `ar.last_seq`（**定位锚点**，不是新序号），
            #      必然 `<= last_sent` → 走下面的去重就会被当成"与重放重叠"丢掉。
            #      即"刚发出的告警被自己的去重逻辑吃掉"，是最隐蔽的那种丢法。
            if topic == "stream/overrun":
                flush_proc()
                sse_frame(h, 
                    "stream/overrun",
                    {"run": run_id, "topic": "stream/overrun",
                     "seq": e.get("seq", 0), "ts": e.get("ts") or time.time(),
                     "actor": e.get("actor") or "agent",
                     "dropped": e.get("dropped", 0),
                     "topic_scope": e.get("topic_scope", ""),
                     "reason": e.get("reason", "")},
                    eid=None)     # ★ 刻意不带 id：它是"传输"的事实，不是 run 的序号；
                                  #   写进 Last-Event-ID 会让重连游标越过它自己
                last_beat = time.time()
                continue

            seq = e.get("seq", 0)
            if seq and seq <= last_sent:
                continue                            # 与重放重叠 → 去重
            if ev_mod.is_batched(topic):
                # 100ms 合并（§17.3）；**只有进程输出**进这里 ——
                # 见 `events.is_batched()`：`serial/*` 曾被 `is_activity()`
                # 误判进合并，于是 serial/closed-loop 变成一行空文本、
                # 「串口闭环」UI 永远是死的，且只在**实时**路径上复现。
                step = e.get("step", "")
                if pending and step != pending_step:
                    flush_proc()
                pending_step = step
                pending.append(str(e.get("line", "")))
                last_sent = max(last_sent, seq)
                if (time.time() - last_flush) >= PROC_BATCH_S:
                    flush_proc()
                continue
            flush_proc()
            sse_write(h, e)
            last_sent = max(last_sent, seq)
            last_beat = time.time()

        flush_proc()
        chunk("0\r\n\r\n")                          # chunked 终止块
    except ConnectionError:
        return                                      # 客户端走了（head / 关标签页），常态
    except Exception as exc:                        # noqa: BLE001
        sys.stderr.write(f"[cockpit] SSE 异常（{run_id}）：{exc!r}\n")
        return
    finally:
        h.mgr.unsubscribe(run_id, sub)
        h.sse_sem.release()


def run_exists(h, run_id: str) -> bool:
    return (h.mgr.runs_dir / f"{run_id}.jsonl").exists()


def sse_warn_unknown(h, topic: str) -> None:
    """发出的 topic 不在 ``SSE_TOPICS`` 登记表里 → 打一条 WARN。

    ★ 为什么值得单独做这件事：这个缺陷是**完全静默**的 —— 服务端 HTTP 200、
    帧格式正确、日志正常；浏览器不报错也不触发 `onerror`，只是那块 UI 永远
    不更新。没有这行 WARN，"新增了 topic 但忘了同步前端白名单"只能靠人肉发现。
    """
    if topic in SSE_TOPICS or topic in CockpitHandler._warned_topics:
        return
    CockpitHandler._warned_topics.add(topic)
    h.mgr.log(
        f"[cockpit] ⚠ SSE topic 未登记：{topic!r} —— 若前端 `KNOWN_TOPICS`"
        f" 也没同步，浏览器会**静默丢弃**该 topic 的全部事件（ICD §6.1）。"
        f" 两端都要改：cockpit/server.py 的 SSE_TOPICS + web 的 KNOWN_TOPICS。")


def send_chunk(h, data: str) -> None:
    raw = data.encode("utf-8")
    h.wfile.write(f"{len(raw):X}\r\n".encode("ascii") + raw + b"\r\n")
    h.wfile.flush()


def sse_frame(h, event: str, data: dict, eid: int | None = None) -> None:
    """命名事件帧。``eid`` 为 None 时**不**写 ``id:``。

    ★ 为什么 ``proc/stdout-batch`` 也必须带 ``id``（实测发现的缺陷）：
      浏览器只用 ``id:`` 维护 ``Last-Event-ID``。合并帧若不带 ``id``，
      客户端记录的游标会停在**上一个 run\\* 事件**上，于是重连时服务端从
      那个旧游标重放 —— 已经通过合并帧交付过的日志行**会再发一遍**，
      证据轨里出现重复行（且只在"断过线"时才复现，极难归因）。
      合并帧里的事件 seq 天然连续（非活动事件写入前必先冲刷 pending），
      故用其最大 seq 当 ``id`` 是精确且单调的。
    """
    sse_warn_unknown(h, event)
    head = f"id: {eid}\n" if eid is not None else ""
    send_chunk(h, f"{head}event: {event}\ndata: "
                + json.dumps(data, ensure_ascii=False) + "\n\n")


def sse_write(h, e: dict) -> None:
    """一条事件 → 一个 SSE 帧。``id:`` 即 ``seq``，浏览器重连时自动回传。"""
    topic = e.get("topic", "message")
    sse_warn_unknown(h, topic)
    send_chunk(h, f"id: {e.get('seq', 0)}\n"
                f"event: {topic}\n"
                "data: " + json.dumps(e, ensure_ascii=False) + "\n\n")
