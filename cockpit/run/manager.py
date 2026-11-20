"""cockpit.run.manager —— run（子进程编排）登记簿。

spawn / 跟读 / 取消；长任务绝不进本进程（§5.1 K1）。
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
#  Run 管理：spawn / 跟读 / 取消（§5.1）
# ══════════════════════════════════════════════════════════════════
@dataclass
class ActiveRun:
    #: 以下四个是**临时占位**：``run/start`` 一落盘就以日志为准（见 ``list_active``）。
    #: 留着它们只是为了覆盖"子进程刚 spawn、首行还没刷出来"的那几百毫秒窗口。
    run_id: str
    project: str
    steps: list[str]
    actor: str
    #: 以下三个只有进程内拿得到 —— 日志里没有也不该有，故**必须**留在内存。
    proc: subprocess.Popen
    started_at: float
    subscribers: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: 背压统计：给 UI 显示"日志已截断"用（§17.3）。
    #: ★ 这是**run 级**计数（跨订阅者累加），`/api/runs.active` 直接读它。
    dropped: int = 0
    last_seq: int = 0
    #: 每个订阅者队列的溢出状态：``id(q) -> {"armed": bool, "marker": dict|None}``。
    #: 键用 ``id(q)`` 是安全的 —— q 在整个订阅期都被 ``subscribers`` 持有，
    #: 期间地址不会变也不会被回收（``unsubscribe`` 会同步清掉这条）。
    sub_state: dict = field(default_factory=dict)
    #: 终态回调（M2 让位/回归）：run 进程退出、stream/closed 扇出后调用。
    on_finish: "callable | None" = None

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class RunManager:
    """run 登记簿 + 事件扇出。

    登记簿（``_active``）记的是**本进程起过的 run**；子进程退出、follower 收尾后
    会把自己摘掉，故它长期看只包含"在跑的"。对外报"活动 run"的是
    :meth:`list_active`，它还会再过滤一次 ``alive``（见该方法的说明）。

    一个 run 一个 follower 线程：它用 ``kernel.events.follow`` 跟读
    ``<id>.jsonl`` + ``<id>.proc.jsonl``（**按 seq 归并**），再推给所有订阅者。
    子进程退出后，follower 补发一条 ``stream/closed`` 让浏览器**主动断开**
    （否则 EventSource 会不停重连一个已结束的 run）。

    ★ 登记簿**不是**真相：真相是事件日志（契约 §5.3）。``_active`` 里除
      ``proc``/``alive``/``last_seq`` 这几个"只有进程内才有"的句柄之外，
      其余元数据都在 ``run/start`` 里逐字存在，且以那份为准（见 :meth:`list_active`）。
      这条纪律是 N9 换来的：两份真相迟早会分叉。
    """

    def __init__(self, cfg: Config, *, log=print):
        self.cfg = cfg
        self.runs_dir = run_mod.runs_dir_for(cfg)
        self.log = log
        self._active: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()

    # ── 查询 ────────────────────────────────────────────────────
    def active(self, run_id: str) -> ActiveRun | None:
        with self._lock:
            return self._active.get(run_id)

    def list_active(self) -> list[dict]:
        """**只在跑的** run，按 ``started_at`` 倒序（最新在前）。

        ★ 为什么必须过滤 ``alive``（实测踩过的缺陷）：
          ``_active`` 的语义不是"活动 run 列表"，而是"本进程起过的 run 的登记簿" ——
          子进程退出后条目仍要在（SSE 靠它判定"已结束 → 重放完就收尾"）。
          早先把登记簿**原样**当成活动列表返回，于是 ``/api/runs.active``
          ①永远不为空、②无界增长，而且前端 ``active.find(project)`` 取到的是
          **最早**那条已结束的 run。真实症状：新起一个 build-only run，
          界面却显示上一条 ``doctor+build`` 的 36 行日志，
          并且 300ms 内就"跑完了"（那其实是旧 run 的重放）。
          过滤之后 ``active`` 才配得上它的名字，前端也因此能正确回退到
          "没有在跑的 → 显示最近一次的留档"。

        ★ **元数据以事件日志为准**（契约 §5.3）。``project``/``steps``/``actor``/
          ``started_at`` 在 ``run/start`` 里**逐字都有**，而 ``_active`` 里那份是
          ``spawn()`` 按自己的理解记下的 —— 两份真相必然有一天对不上
          （实测 N9：取消/重跑之后界面显示的步骤序列与日志里的不一致）。
          故这里一律**回读 ``run/start``**，内存那份退化为"子进程还没把首行刷出来"
          那几百毫秒的**临时占位**，并用 ``meta_source`` 把这件事**标出来**，
          而不是让人以为它同样权威。
          剩下的 ``pid``/``alive`` 只存在于进程内、日志里没有也不该有 —— 那才是
          ``_active`` 该独占的东西。
        """
        with self._lock:
            live = [a for a in self._active.values() if a.alive]
        out = []
        for a in live:
            st = self._start_event(a.run_id)
            if st is not None:
                meta = {
                    "project": st.get("project") or a.project,
                    "steps": list(st.get("steps") or a.steps),
                    "actor": st.get("actor") or a.actor,
                    "started_at": st.get("ts") or a.started_at,
                    "meta_source": "event-log",
                }
            else:
                meta = {"project": a.project, "steps": list(a.steps),
                        "actor": a.actor, "started_at": a.started_at,
                        "meta_source": "provisional"}
            out.append({
                "run": a.run_id, **meta,
                # 下面两个是**进程内**的事实，日志里没有也不该有：
                "pid": a.proc.pid, "alive": True,
                "dropped": a.dropped, "last_seq": a.last_seq,
            })
        out.sort(key=lambda x: x["started_at"], reverse=True)
        return out

    def _start_event(self, run_id: str) -> dict | None:
        """回读该 run 的**首条** ``run/start``（``seq`` 恒为 1，也就是文件第一行）。

        只 peek 一次、**不缓存**：事件文件是只追加的，首行永不改变，
        故"每次重读"与"缓存一份"结果必然相同 —— 那就不必再引入第二个
        需要保持同步的东西（本文件反复在为"少一个同步点"付代价）。
        """
        state = self.runs_dir / f"{run_id}.jsonl"
        try:
            for e in ev_mod.replay(state, 0):
                return e if e.get("topic") == ev_mod.RUN_START else None
        except OSError:
            return None
        return None

    # ── spawn ───────────────────────────────────────────────────
    def spawn(self, *, project: str, steps: list[str] | None = None,
              clean: bool = False, jobs: int | None = None, verbose: bool = False,
              target: str | None = None, actor: str = "human", on_finish=None) -> dict:
        """起一个 ``elab run --emit-events`` 子进程，并立刻开始跟读。

        ★ run id **由父进程分配**并显式传给子进程（``--run-id``）：
          否则父进程无法在"文件还没出现"时就知道该跟读哪个文件 —— 那是一个必然的竞态。
        """
        steps = list(steps or run_mod.DEFAULT_STEPS)
        # 与 run.parse_steps 同规则：tool:<name> 放行（<name> 是否声明由子进程
        # _resolve_tool 校验 —— 项目 YAML 在那一侧才读得到）
        bad = [s for s in steps
               if s not in run_mod.ALL_STEPS
               and not (s.startswith(run_mod.TOOL_PREFIX)
                        and len(s) > len(run_mod.TOOL_PREFIX))]
        if bad:
            raise ElabError(f"未知步骤：{', '.join(bad)}")

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = ev_mod.allocate_run_id(self.runs_dir)

        argv = [sys.executable, "-m", "elab", "run",
                "-p", project, "--steps", ",".join(steps),
                "--emit-events", "--run-id", run_id,
                "--events-dir", to_fwd(self.runs_dir),
                "--actor", actor, "-q"]
        if target:
            argv += ["--target", target]
        if clean:
            argv.append("--clean")
        if jobs:
            argv += ["-j", str(jobs)]
        if verbose:
            argv.append("-v")

        env = dict(os.environ)
        # 让子进程能 import elab（与 ./elab 启动器同一手法）
        env["PYTHONPATH"] = str(_ROOT / "services") + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"

        # ★ 独立进程组：cmake→ninja→gcc 是一棵树，取消时要整棵杀掉（K10）
        # ★ CREATE_NO_WINDOW：服务已 pythonw 化（无控制台可继承），不挂这个
        #   标志则每个子进程（python/cmake/openocd…）都会新弹一个黑色控制台
        #   窗口——用户实测"一手动按命令就弹命令行框框"。隐藏控制台会被
        #   孙进程继承，全树无窗口。
        kwargs: dict = {"cwd": str(_ROOT), "env": env,
                        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | 0x08000000)  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(argv, **kwargs)
        # 进「句柄关闭即终止」作业：内核被强杀时，这条 run 进程树（含用户固件
        # 程序，它会占串口）跟着一起走 —— 否则下次连串口会先遇到"端口被占用"。
        adopt_proc(proc)
        ar = ActiveRun(run_id=run_id, project=project, steps=steps, actor=actor,
                       proc=proc, started_at=time.time(), on_finish=on_finish)
        with self._lock:
            self._active[run_id] = ar

        threading.Thread(target=self._follow_loop, args=(ar,),
                         name=f"follow-{run_id}", daemon=True).start()
        self.log(f"[cockpit] ▶ run {run_id} project={project} steps={','.join(steps)} pid={proc.pid}")
        return {"run": run_id, "project": project, "steps": steps,
                "pid": proc.pid, "actor": actor}

    # ── 跟读 + 扇出 ─────────────────────────────────────────────
    def _follow_loop(self, ar: ActiveRun) -> None:
        paths = ev_mod.run_paths(self.runs_dir, ar.run_id)
        rc = None
        try:
            for e in ev_mod.follow(paths, 0,
                                   stop=lambda: ar.proc.poll() is not None,
                                   poll=0.05, max_idle_s=60 * 30,
                                   drain_after_stop=0.4):
                ar.last_seq = e.get("seq", ar.last_seq)
                self._fanout(ar, e)
            rc = ar.proc.wait()
        except Exception as exc:                      # noqa: BLE001 —— 后台线程不能炸掉服务
            self.log(f"[cockpit] follower 异常（{ar.run_id}）：{exc}")
        finally:
            try:
                rc = ar.proc.wait(timeout=5)
            except Exception:                          # noqa: BLE001
                pass
            self._fanout(ar, {"run": ar.run_id, "topic": "stream/closed",
                              "seq": ar.last_seq, "ts": time.time(),
                              "actor": "agent", "reason": "run-finished", "rc": rc})
            self.log(f"[cockpit] ■ run {ar.run_id} 结束 rc={rc}")
            # ★ 收尾后把自己从登记簿摘掉（否则跑一夜就是几百个僵尸条目）。
            #   注意顺序：必须**先** fanout `stream/closed` 再 pop —— 已经挂在
            #   `ar.subscribers` 上的 SSE 连接是靠那条事件收尾的，与登记簿无关。
            #   摘掉之后 `active(run_id)` 变 None，而 SSE 对"未知/已结束 run"的
            #   处理本来就是同一条重放分支（`ar is None or not ar.alive`），
            #   所以行为不变、只是内存有界。
            with self._lock:
                self._active.pop(ar.run_id, None)
            # M2 §4.3.6：run 结束 → 让位过的串口监视回归（失败不影响 run 收尾）
            if ar.on_finish is not None:
                try:
                    ar.on_finish(ar, rc)
                except Exception as exc:             # noqa: BLE001
                    self.log(f"[cockpit] on_finish 异常（{ar.run_id}）：{exc}")

    def _fanout(self, ar: ActiveRun, ev: dict) -> None:
        """把一条事件推给所有 SSE 订阅者；**队列满时按契约丢弃并留痕**。

        丢谁（ICD §3.2）：``proc/*`` 等 activity 类可丢，``run/*`` 绝不丢
        （``run/*`` 是界面状态的唯一真相，丢了界面就再也回不到正确状态）。

        ★ 旧实现只 ``ar.dropped += 1`` 然后 continue —— 于是"丢了多少"只有
          服务端自己知道，界面上什么都没有。这正是本仓库反复出现的
          **静默丢事件**形态：服务端全对、HTTP 全 200、日志无一行报错，
          只有那块 UI 少了内容。修法就是契约 §17.3 早就写好的那句
          "并发 `stream/overrun` 让 UI 提示'日志已截断'"。
        """
        with ar.lock:
            subs = list(ar.subscribers)
        topic = ev.get("topic", "")
        # `stream/closed` 虽属 activity 通道，却是控制帧 → 与 run/* 同级不可丢
        droppable = ev_mod.is_activity(topic) and topic not in CONTROL_TOPICS
        for q in subs:
            try:
                q.put_nowait(ev)
                # 队列不再满 → 结束了一个溢出 episode，下一轮可以再报一次
                ar.sub_state.pop(id(q), None)
                continue
            except queue.Full:
                pass

            if droppable:
                ar.dropped += 1
                self._signal_overrun(ar, q, scope="proc/*")
                continue

            # ── run/* 不可丢 → 挤掉队首（队首多半是最老的 activity 行）──
            evicted = None
            try:
                evicted = q.get_nowait()
            except queue.Empty:
                pass
            if evicted is not None:
                ar.dropped += 1          # 无论它是哪一类，订阅者都收不到了
            try:
                q.put_nowait(ev)
            except queue.Full:
                # 挤了还是满（队列里全是 run/*）→ 这一次的 run/* 只能丢，
                # 但**必须留痕**：界面据此知道"状态可能不完整，需要重载"。
                self._signal_overrun(ar, q, scope="run/*")
                continue
            if evicted is not None:
                self._signal_overrun(ar, q, scope="proc/*")

    def _signal_overrun(self, ar: ActiveRun, q: queue.Queue, *, scope: str) -> None:
        """把一个**合成的** ``stream/overrun`` 塞进该订阅者队列（同一 episode 只塞一个）。

        为什么是合成事件而不是 ``emit()`` —— 这条很关键：

        - ``stream/overrun`` 描述的是**这一条 SSE 流**丢了多少，不是 run 自己的事实。
          写进 ``<id>.proc.jsonl`` 会同时破两件事：① 单写者模型（父进程去写子进程的
          日志文件）；②"删掉 ``.proc.jsonl`` 只损失日志、不影响状态重建"的通道判据。
        - 真相**一条没少**地躺在文件里，浏览器断线重连即可按 ``seq`` 补齐
          （``_sse`` 的重放分支就是干这个的）。这条事件的作用只是把
          "你此刻看到的不是全部"**当场说出来**，而不是等用户自己发现日志断了。
        - 于是它对 DSH 那条硬约束是**不违逆**的：可观测性、重建都仍从日志派生；
          这里交付的是一个关于"传输"的瞬时事实，丢失它不影响任何状态重建。

        ★ 同一个溢出 episode 只报一次：队列满时若每条丢的事件都塞一个标记，
          队列里会全是标记、把真实内容彻底挤出去，UI 反而更瞎。
          标记里的 ``dropped`` 是**同一个 dict 对象**，后续每次丢弃都就地更新，
          故 SSE 真正读到它时拿到的是最新数字，而不是"标记诞生那一刻"的旧值。
        """
        st = ar.sub_state.setdefault(id(q), {"armed": False, "marker": None})
        if st["armed"] and st["marker"] is not None:
            st["marker"]["dropped"] = ar.dropped
            st["marker"]["ts"] = time.time()
            return
        marker = {
            "ts": time.time(), "run": ar.run_id, "topic": "stream/overrun",
            # 不带新 seq：它不是 run 的事实，不该消耗 run 的计数器。
            # 用"当前已推进到的 seq"当定位锚点；SSE 侧也不会把它写进 Last-Event-ID。
            "seq": ar.last_seq, "actor": "agent",
            # ★ 与 `/api/runs.active[].dropped` **同一个数**（run 级）。
            #   刻意不做 per-connection 计数：两个都叫 dropped 的数字含义不同，
            #   迟早会有人在界面上把它们并列显示，然后对不上。
            "dropped": ar.dropped, "topic_scope": scope,
            "reason": "sse-queue-full",
        }
        st["marker"] = marker
        try:
            q.put_nowait(marker)
            st["armed"] = True
        except queue.Full:
            # 满到连标记都放不下 → 让队首一条活动行让位（性质不变：仍是丢活动事件）
            try:
                q.get_nowait()
                q.put_nowait(marker)
                st["armed"] = True
            except (queue.Empty, queue.Full):
                pass

    # ── 订阅（SSE 用）───────────────────────────────────────────
    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        ar = self.active(run_id)
        if ar is not None:
            with ar.lock:
                ar.subscribers.add(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        ar = self.active(run_id)
        if ar is not None:
            with ar.lock:
                ar.subscribers.discard(q)
            # ★ 必须一起清：sub_state 用 id(q) 当键，留着就是"地址将被复用"的隐患
            #   （新队列可能落到同一地址，于是它一上来就被判成"已报过溢出"，
            #    真正的溢出反而报不出来 —— 又是一次静默丢事件）。
            ar.sub_state.pop(id(q), None)

    # ── 取消 ────────────────────────────────────────────────────
    def cancel(self, run_id: str, *, reason: str = "用户取消") -> dict:
        """杀掉整棵进程树，然后**由父进程补一条 ``run/cancel``**。

        ★ 这是契约"单写者"的**唯一豁免**（ICD §5.4）：子进程是被 ``taskkill /F``
          硬杀的，没有机会自己写收尾事件。补写的**前置条件是写者已死**（我们
          ``wait()`` 过），故不存在并发写。
        """
        ar = self.active(run_id)
        if ar is None:
            return {"ok": False, "error": f"没有活动的 run：{run_id}"}
        if not ar.alive:
            return {"ok": False, "error": f"run 已结束：{run_id}"}

        pid = ar.proc.pid
        if os.name == "nt":
            # /T 连带子树（cmake → ninja → gcc），/F 强制。
            # ★ 必须 errors="replace"：中文 Windows 上 taskkill 输出的是 GBK
            #   （"成功: 已终止 PID …"），按 UTF-8 解码会在 subprocess 的读线程里
            #   抛 UnicodeDecodeError —— 取消反而刷一屏假栈，掩盖真正的取消结果。
            #   本仓库其余的 subprocess 调用都带这个参数，这里不能例外。
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, errors="replace", check=False)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                ar.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass

        try:
            rc = ar.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = None

        appended = ev_mod.append_event(
            self.runs_dir, run_id, "run/cancel", actor="human",
            project=ar.project, by="human", reason=reason,
        )
        if appended is not None:
            ar.last_seq = appended["seq"]
            self._fanout(ar, appended)
        self.log(f"[cockpit] ■ 已取消 {run_id}（rc={rc}）")
        return {"ok": True, "run": run_id, "rc": rc, "pid": pid}


__all__ = [n for n in list(globals()) if not n.startswith("__")]
