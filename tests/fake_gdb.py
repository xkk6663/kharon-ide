"""可编程的 gdb MI 替身：让调试会话的业务状态机脱离真 gdb / 真探针跑通。

为什么是「假 gdb」而不是「假 openocd」：

    xrdap 的 ``xrdap-sim`` 模拟的是**探针**（协议层），因为它的主机侧边界是
    CMSIS-DAP 帧。我们的边界在**上一层** —— gdb 之上全是我们的代码
    （debugsession / debugdata / cockpit），gdb 之下才是 openocd 与 SWD。
    所以等价物是假 MI：少解析一层 RSP 协议，覆盖同一段业务状态机，
    而且 CI 上不需要装 arm-none-eabi-gdb。

★ 关键不是只跑通顺利路径：**能注入故障**（命令失败 / 超时 / 断点设不上 /
  启动即死）才让"降级与报错"可测。只测 happy path 的替身价值有限。

用法::

    fake = FakeGdb()
    fake.fail("-break-insert", MiError("no symbol"))   # 注入一次失败
    sess = FakeSession(root, fake)
    sess.open()
"""

from __future__ import annotations

import re
import threading

from elab.gdbmi import MiError

#: 假的寄存器名（Cortex-M3 内核寄存器，与真机页签一致的子集）
REG_NAMES = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9",
             "r10", "r11", "r12", "sp", "lr", "pc", "xpsr"]


class FakeGdb:
    """一个够用的 MI 端：寄存器 / 内存 / 断点 / 栈帧 / 变量，外加故障注入。"""

    def __init__(self, *, mi_async: bool = True):
        self.on_async = None
        self.on_stream = None
        self.started = False
        self.closed = False
        self.mi_async = mi_async
        self.commands: list[str] = []
        self.regs = {n: 0x2000_0000 + i * 4 for i, n in enumerate(REG_NAMES)}
        self.regs["pc"] = 0x0800_0100
        self.regs["sp"] = 0x2000_5000
        self.memory: dict[int, bytearray] = {}
        self.vars: dict[str, tuple[str, str]] = {
            "motor.kp": ("1.0", "float"),
            "mode": ("RUN", "enum Mode"),
            "g_counter": ("17", "uint32_t"),
        }
        #: 断点：gdb_id → {addr, func, times}
        self.breakpoints: dict[str, dict] = {}
        self._bp_seq = 0
        #: 故障注入：命令前缀 → [异常 or None]，None 表示"这一次正常"
        self._faults: dict[str, list] = {}
        #: continue 后是否立刻回调 *stopped（True = open 的 run-to-main 能同步收敛）
        self.auto_stop = True
        self._lock = threading.Lock()

    # ── 故障注入 ────────────────────────────────────────────────
    def fail(self, prefix: str, error: Exception | None = None, *, times: int = 1) -> None:
        """让匹配该前缀的下 N 次命令抛错（``times=0`` 表示一直抛）。

        ``error=None`` 时用 ``MiError("injected failure")`` —— 有些路径只关心
        "失败了怎么降级"，不关心具体文案。
        """
        err = error if error is not None else MiError("injected failure")
        with self._lock:
            slot = self._faults.setdefault(prefix, [])
            slot.extend([err] * (times if times > 0 else 10_000))

    def heal(self) -> None:
        with self._lock:
            self._faults.clear()

    # ── MiSession 兼容面 ────────────────────────────────────────
    def start(self, timeout: float = 10.0) -> None:
        self.started = True

    @property
    def alive(self) -> bool:
        return self.started and not self.closed

    def close(self) -> None:
        self.closed = True

    def send(self, cmd: str) -> None:
        self.commands.append(cmd)

    def command(self, cmd: str, timeout: float = 5.0) -> dict:
        self.commands.append(cmd)
        with self._lock:
            for prefix, queue in self._faults.items():
                if cmd.startswith(prefix) and queue:
                    raise queue.pop(0)
        return self._dispatch(cmd)

    # ── 命令分发 ────────────────────────────────────────────────
    def _dispatch(self, cmd: str) -> dict:
        if cmd.startswith(("-gdb-set", "-enable-pretty-printing")):
            return {"results": {}}
        if cmd.startswith("-target-select"):
            return {"results": {}}
        if cmd.startswith("-target-download"):
            # 假装下载成功，并把 RAM 首字节写成非零（证明链路真的走通了）
            self.memory[0x2000_0000] = bytearray(b"\x5a\x00\x00\x00")
            return {"results": {}}
        if cmd.startswith("-interpreter-exec"):
            return {"results": {}}
        if cmd.startswith("-data-list-register-names"):
            return {"results": {"register-names": list(REG_NAMES)}}
        if cmd.startswith("-data-list-register-values"):
            return {"results": {"register-values": [
                {"number": i, "value": hex(self.regs[n])}
                for i, n in enumerate(REG_NAMES)]}}
        if cmd.startswith("-thread-info"):
            return {"results": {
                "threads": [{"id": "1", "target-id": "Thread 1", "name": "main",
                             "state": "stopped",
                             "frame": {"func": "main", "file": "main.c",
                                       "fullname": "/fake/main.c", "line": "12",
                                       "addr": hex(self.regs["pc"])}}],
                "current-thread-id": "1"}}
        if cmd.startswith("-thread-select"):
            return {"results": {}}
        if cmd.startswith("-symbol-info-variables"):
            # ★ 形状必须与真 gdb 一致：文件名挂在**父层** filename 上，
            #   符号自己只有 name/type。写成"符号自带 file"会让 statics 过滤
            #   的 bug 藏起来（真机上踩过一次）。
            return {"results": {"symbols": {"debug": [
                {"filename": "/fake/main.c", "symbols": [
                    {"name": n, "type": t} for n, (_v, t) in self.vars.items()]},
                {"filename": "/fake/other.c", "symbols": [
                    {"name": "other_counter", "type": "uint8_t"}]},
            ]}}}
        if cmd.startswith("-file-list-exec-source-files"):
            return {"results": {"files": [{"file": "main.c", "fullname": "/fake/main.c"}]}}
        if cmd.startswith("-stack-list-frames"):
            return {"results": {"stack": [
                {"level": "0", "addr": hex(self.regs["pc"]), "func": "main",
                 "file": "main.c", "fullname": "/fake/main.c", "line": "12"},
            ]}}
        if cmd.startswith("-stack-list-variables"):
            return {"results": {"variables": [
                {"name": "local_a", "value": "3", "type": "int"},
            ]}}
        if cmd.startswith("-stack-list-arguments"):
            return {"results": {"stack-args": [
                {"level": "0", "args": [{"name": "argc", "value": "1", "type": "int"}]},
            ]}}
        if cmd.startswith("-stack-select-frame"):
            return {"results": {}}
        if cmd.startswith("-break-insert"):
            return {"results": {"bkpt": self._new_bp(cmd)}}
        if cmd.startswith("-break-delete"):
            gid = cmd.split()[-1]
            self.breakpoints.pop(gid, None)
            return {"results": {}}
        if cmd.startswith("-break-enable") or cmd.startswith("-break-disable"):
            gid = cmd.split()[-1]
            return {"results": {"bkpt": self.breakpoints.get(gid, self._new_bp(""))}}
        if cmd.startswith("-break-list"):
            return {"results": {"BreakpointTable": {
                "nr_rows": str(len(self.breakpoints)),
                "body": [{"bkpt": dict(v, number=k)}
                         for k, v in self.breakpoints.items()]}}}
        if cmd.startswith("-exec-continue"):
            # 真 gdb 是**异步**的：continue 先返回，过一会儿才 *stopped。
            # 这里默认立刻停（为了 open 的 run-to-main 能在同一线程收敛），
            # 需要观察 running 态的测试把它关掉。
            if self.auto_stop:
                self._report_stop("breakpoint-hit")
            return {"results": {}}
        if cmd.startswith("-exec-interrupt"):
            self._report_stop("signal-received")
            return {"results": {}}
        if cmd.startswith("-data-evaluate-expression"):
            return {"results": self._evaluate(cmd.split(" ", 1)[1].strip())}
        if cmd.startswith("-var-create"):
            # -var-create - * <expr>；expr 可能被 _mi_expr 加了引号
            exp = cmd.split("*", 1)[1].strip() if "*" in cmd else ""
            exp = exp.strip('"')
            if exp not in self.vars:
                raise MiError(f"No symbol {exp!r}")
            return {"results": {"name": f"var_{len(self.vars)}", "numchild": "0",
                                "value": self.vars[exp][0], "type": self.vars[exp][1]}}
        if cmd.startswith("-var-list-children"):
            return {"results": {}}
        if cmd.startswith("-var-delete"):
            return {"results": {}}
        if cmd.startswith("-data-read-memory-bytes"):
            addr, count = self._parse_mem_args(cmd)
            return {"results": {"memory": [{"begin": hex(addr), "offset": "0",
                                            "end": hex(addr + count),
                                            "contents": self._read(addr, count).hex()}]}}
        if cmd.startswith("-data-write-memory-bytes"):
            m = re.search(r"(-?0x[0-9a-fA-F]+)\s+\"([0-9a-fA-F]*)\"", cmd)
            if not m:
                raise MiError("bad write-memory")
            addr = int(m.group(1), 16)
            data = bytes.fromhex(m.group(2))
            self._write(addr, data)
            return {"results": {}}
        if cmd.startswith("-file-list-exec-source-file"):
            return {"results": {"file": "main.c", "fullname": "/fake/main.c", "line": "12"}}
        raise AssertionError(f"FakeGdb 未覆盖的命令：{cmd!r}")

    # ── 小工具 ──────────────────────────────────────────────────
    def _new_bp(self, cmd: str) -> dict:
        self._bp_seq += 1
        gid = str(self._bp_seq)
        loc = cmd.rsplit(" ", 1)[-1] if " " in cmd else "main"
        bp = {"addr": hex(self.regs["pc"]), "func": loc.lstrip("-t "), "times": "0",
              "enabled": "y", "number": gid}
        self.breakpoints[gid] = bp
        return bp

    def _evaluate(self, expr: str) -> dict:
        if expr.startswith("(") and expr.endswith(")") and "=" in expr:
            name, val = expr[1:-1].split("=", 1)
            ty = self.vars.get(name, ("", "int"))[1]
            self.vars[name] = (val, ty)
            return {"value": val, "type": ty}
        if expr in self.vars:
            v, ty = self.vars[expr]
            return {"value": v, "type": ty}
        raise MiError(f"No symbol {expr!r}")

    @staticmethod
    def _parse_mem_args(cmd: str) -> tuple[int, int]:
        parts = cmd.split()
        return int(parts[-2], 16), int(parts[-1])

    def _read(self, addr: int, count: int) -> bytes:
        out = bytearray()
        for i in range(count):
            blk = self.memory.get((addr + i) & ~0x3)
            out.append(blk[(addr + i) & 0x3] if blk and len(blk) > ((addr + i) & 0x3) else 0)
        return bytes(out)

    def _write(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            a = (addr + i) & ~0x3
            blk = self.memory.setdefault(a, bytearray(4))
            blk[(addr + i) & 0x3] = b

    def _report_stop(self, reason: str) -> None:
        """把 ``*stopped`` 异步事件送给会话（同线程回调，避免测试里出现竞态）。"""
        if self.on_async is None:
            return
        self.on_async({"kind": "exec", "cls": "stopped", "results": {
            "reason": reason,
            "frame": {"func": "main", "file": "main.c", "fullname": "/fake/main.c",
                      "line": "12", "addr": hex(self.regs["pc"])},
            "bkptno": "1",
        }})
