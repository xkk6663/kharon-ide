"""elab.rtos —— RTOS 运行态内省（P3 RTOS 页签）。

只做**栈水位检测**（用户要求），不做 LiveWatch / RTT。

两套实现，同一个输出契约（``{tasks, count, current_tcb, rtos}``）：

**FreeRTOS**
1. ``pxCurrentTCB`` 存在 → 认为 FreeRTOS 可用（会话 open 时已探过一次）。
2. 用 ``&((TCB_t *)0)->field`` 取结构体字段偏移（**不**解引用空指针）。
3. 遍历 ``pxReadyTasksLists`` / ``xDelayedTaskList1`` / ``xDelayedTaskList2`` /
   ``xPendingReadyList`` / ``xSuspendedTaskList``（缺哪个跳哪个），
   每个 ListItem 的 ``pvOwner`` 就是 TCB。
4. 水位优先级：TCB ``usStackHighWaterMark``（若存在）→ 否则从 ``pxStack``
   向上扫 FreeRTOS 填充字节 ``0xA5``。
5. 栈总大小只在 ``pxEndOfStack`` 存在时能算准（Cortex-M 默认不记录），
   否则 ``stack_size_bytes/used_pct`` 返回 null —— **不猜**。

**Zephyr（T13）**
1. ``_kernel.cpus[0].current``（旧版 ``_kernel.current``）存在 → Zephyr 可用。
2. ``_kernel.threads``（``sys_slist_t``，需 ``CONFIG_THREAD_MONITOR``）从 ``head``
   沿 ``sys_snode_t.next`` 走；节点地址减 ``offsetof(k_thread, base.qnode)``
   即线程地址。
3. 名称读 ``thread->name`` 指针后的 C 串；优先级/状态读 ``base.prio`` /
   ``base.thread_state`` 的字节；栈范围读 ``stack_info.start/size``。
4. 水位：``CONFIG_INIT_STACKS`` 用 ``0xAA`` 填满整栈，从 ``start`` 向上数连续
   ``0xAA`` 字；没开该选项时 ``scanned`` 为真但数值可能是 0 —— **不猜**。
5. 没有 ``CONFIG_THREAD_MONITOR`` 时至少把当前线程报出来（不静默返回空表）。

``collect(reader)`` 是纯逻辑；``reader`` 只需实现 ``ev(expr)`` / ``mem(addr, size)``，
单测用 fake reader（见 ``tests/test_rtos.py`` / ``tests/test_rtos_zephyr.py``）。
"""

from __future__ import annotations

FILL_BYTE = 0xA5
#: Zephyr 的 ``CONFIG_INIT_STACKS`` 填充字节
Z_FILL_BYTE = 0xAA

#: 水位阈值（FreeRTOS 的 high water mark = 历史最小剩余**字**数）
WARN_FREE_WORDS = 16
CRIT_FREE_WORDS = 4

_MAX_ITEMS = 64


class RtosUnavailable(RuntimeError):
    """ELF 里没有可用的 FreeRTOS 符号 / 调试类型。"""


def _num(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        # gdb 有时回 ``1 '\\001'`` 之类，取第一段
        try:
            return int(s.split()[0], 0)
        except ValueError:
            return None


def _off(reader, field: str, type_name: str = "TCB_t") -> int | None:
    # ★ 表达式里**不能有空格**（多词类型也不行）：gdb MI 会按空格切参数（实测 2026-11-03）
    return _num(reader.ev(f"(long)&(({type_name}*)0)->{field}"))


def _ptr_n(reader, addr: int, size: int) -> int | None:
    if not addr or size <= 0:
        return None
    data = reader.mem(addr, size)
    if not data or len(data) < size:
        return None
    return int.from_bytes(data[:size], "little")


def _ptr(reader, addr: int) -> int | None:
    return _ptr_n(reader, addr, 4)


def _u32(reader, addr: int) -> int | None:
    return _ptr(reader, addr)


def _u16(reader, addr: int) -> int | None:
    data = reader.mem(addr, 2)
    if not data or len(data) < 2:
        return None
    return int.from_bytes(data[:2], "little")


def _task_name(reader, addr: int) -> str:
    data = reader.mem(addr, 16)
    if not data:
        return ""
    raw = bytes(data[:16])
    cut = raw.find(b"\x00")
    if cut >= 0:
        raw = raw[:cut]
    text = raw.decode("latin-1", "replace").strip()
    return text or "(unnamed)"


def _scan_fill(reader, stack: int, fill: int,
               cap_words: int = 256) -> tuple[int, bool]:
    """从栈底向上数连续 ``fill`` 字；返回 (free_words, 是否触顶)。

    ★ 按 256B 块读：逐字 ``mem()`` 在真机上等于每 4 字节一次 MI 往返，
      32 个线程 × 1KB 栈能把一次刷新拖到十几秒。
    """
    free = 0
    while free < cap_words:
        want = min(64, cap_words - free) * 4
        chunk = reader.mem(stack + free * 4, want)
        if not chunk:
            break
        stop = len(chunk)
        for i, b in enumerate(chunk):
            if b != fill:
                stop = i
                break
        free += stop // 4
        if stop < len(chunk) or len(chunk) < want:
            break
    return free, free >= cap_words


def _scan_free_words(reader, stack: int, cap_words: int = 256) -> tuple[int, bool]:
    """FreeRTOS：从栈底向上数连续 0xA5 字。"""
    return _scan_fill(reader, stack, FILL_BYTE, cap_words)


def _health(free_words: int, used_pct: float | None) -> str:
    if used_pct is not None:
        if used_pct >= 90.0:
            return "critical"
        if used_pct >= 75.0:
            return "warn"
        return "ok"
    if free_words <= CRIT_FREE_WORDS:
        return "critical"
    if free_words <= WARN_FREE_WORDS:
        return "warn"
    return "ok"


def _list_offsets(reader) -> dict | None:
    """结构体偏移只算一次（每个 GDB 表达式 ~几十 ms，32 张就绪表不能重复问）。"""
    off_items = _off(reader, "uxNumberOfItems", "List_t")
    off_index = _off(reader, "pxIndex", "List_t")
    off_next = _off(reader, "pxNext", "ListItem_t")
    off_owner = _off(reader, "pvOwner", "ListItem_t")
    if None in (off_items, off_index, off_next, off_owner):
        return None
    return {"items": off_items, "index": off_index,
            "next": off_next, "owner": off_owner,
            "list_size": _num(reader.ev("sizeof(List_t)")) or 20}


def _u32_at(buf: bytes, off: int) -> int | None:
    if not buf or len(buf) < off + 4:
        return None
    return int.from_bytes(buf[off:off + 4], "little")


def _list_tasks(reader, header: bytes, state: str,
                offs: dict | None) -> list[tuple[int, str]]:
    """遍历一个 FreeRTOS List_t（header 已读），回 [(tcb_addr, state)]。"""
    if offs is None:
        return []
    n = _u32_at(header, offs["items"]) or 0
    if n <= 0:
        return []
    n = min(n, _MAX_ITEMS)
    item = _u32_at(header, offs["index"])
    if not item:
        return []
    # pxIndex 在延迟表 / 只有一个成员的链表里可能指向哨兵 xListEnd
    # （pvOwner = 0）。从哨兵起按 n 次遍历会少一个真实项 —— 先跳到第一项。
    if not _ptr(reader, item + offs["owner"]):
        item = _ptr(reader, item + offs["next"])
        if not item:
            return []
    out, seen = [], set()
    for _ in range(n):
        if item in seen:
            break
        seen.add(item)
        owner = _ptr(reader, item + offs["owner"])
        if owner:
            out.append((owner, state))
        nxt = _ptr(reader, item + offs["next"])
        if not nxt:
            break
        item = nxt
    return out


def _tcb_fields(reader) -> dict:
    return {"name": _off(reader, "pcTaskName"),
            "prio": _off(reader, "uxPriority"),
            "stack": _off(reader, "pxStack"),
            "hwm": _off(reader, "usStackHighWaterMark"),
            "end": _off(reader, "pxEndOfStack")}


def _task_record(reader, tcb: int, state: str, current: int,
                 fields: dict) -> dict:
    off_name = fields["name"]
    off_prio = fields["prio"]
    off_stack = fields["stack"]
    off_hwm = fields["hwm"]
    off_end = fields["end"]
    if off_stack is None:
        raise RtosUnavailable("TCB 缺少 pxStack（调试信息不完整）")

    name = _task_name(reader, tcb + off_name) if off_name is not None else ""
    prio = _u32(reader, tcb + off_prio) if off_prio is not None else None
    stack = _ptr(reader, tcb + off_stack)
    if not stack:
        return {}
    end = _ptr(reader, tcb + off_end) if off_end is not None else None

    free_words = _u16(reader, tcb + off_hwm) if off_hwm is not None else None
    scanned = False
    if free_words is None:
        free_words, scanned = _scan_free_words(reader, stack)

    size = None
    if end and end > stack:
        size = end - stack + 4
    free_bytes = free_words * 4
    used_bytes = None
    used_pct = None
    if size:
        used_bytes = max(0, size - free_bytes)
        used_pct = round(used_bytes * 100.0 / size, 1)
    return {
        "name": name,
        "priority": prio,
        "state": state,
        "current": tcb == current,
        "tcb": hex(tcb),
        "stack_start": hex(stack),
        "stack_end": hex(end) if end else None,
        "stack_size_bytes": size,
        "free_words": free_words,
        "free_bytes": free_bytes,
        "high_water_mark": free_words,
        "used_bytes": used_bytes,
        "used_pct": used_pct,
        "health": _health(free_words, used_pct),
        "scanned": scanned,
    }


def collect(reader, *, config: dict | None = None) -> dict:
    """枚举任务/线程 + 栈水位；两套 RTOS 都没有 → :class:`RtosUnavailable`。

    ``config`` 是 ``elab.debug.zephyr.config_facts()`` 的形状（可选）：
    ``CONFIG_INIT_STACKS=n`` 时 Zephyr 栈里没有 0xAA 填充，扫描结果会是
    "全部已用"——这种情况直接不出水位数字，而不是给一个必然误报的值。
    """
    # ★ 先看 FreeRTOS：很多工程同时带 pxCurrentTCB 与 _kernel（双核/兼容层罕见，
    #   但谁先声明谁就是"这个工程在用的 RTOS"）。顺序固定，避免结果随链接布局漂移。
    cur_raw = reader.ev("pxCurrentTCB")
    if cur_raw is not None:
        return _collect_freertos(reader, cur_raw)
    return _collect_zephyr(reader, config=config)


def _collect_freertos(reader, cur_raw) -> dict:
    """FreeRTOS：原 ``collect`` 主体（pxCurrentTCB 已确认存在）。"""
    cur = _num(cur_raw) or 0
    if not cur:
        # 符号存在但还没调度（停在 main / 调度器未启动）：不是错误，只是没有任务
        return {"tasks": [], "count": 0, "current_tcb": "0x0", "rtos": "freertos",
                "note": "调度器尚未启动（pxCurrentTCB = 0）"}

    tasks: list[tuple[str, int]] = []
    offs = _list_offsets(reader)
    stride = offs["list_size"] if offs else 0

    # 就绪表：pxReadyTasksLists[configMAX_PRIORITIES] —— 整块读一次（最多 32×20B）
    size_ready = _num(reader.ev("sizeof(pxReadyTasksLists)"))
    ready_addr = _num(reader.ev("&pxReadyTasksLists"))
    if size_ready and stride and ready_addr and offs:
        block = reader.mem(ready_addr, size_ready)
        if block:
            for i in range(min(size_ready // stride, 32)):
                hdr = block[i * stride:(i + 1) * stride]
                for tcb, _st in _list_tasks(reader, hdr, "ready", offs):
                    tasks.append(("ready", tcb))

    for sym, state in (("xDelayedTaskList1", "delayed"),
                       ("xDelayedTaskList2", "delayed"),
                       ("xPendingReadyList", "pending"),
                       ("xSuspendedTaskList", "suspended"),
                       ("xTasksWaitingTermination", "terminated")):
        addr = _num(reader.ev(f"&{sym}"))
        if not addr or not offs:
            continue
        hdr = reader.mem(addr, stride or 20)
        for tcb, _st in _list_tasks(reader, hdr or b"", state, offs):
            tasks.append((state, tcb))

    # 去重（同一 TCB 只属于一个表）；当前任务的 state 以所在表为准
    uniq: dict[int, str] = {}
    for state, tcb in tasks:
        uniq.setdefault(tcb, state)
    if cur not in uniq:
        uniq[cur] = "running"

    fields = _tcb_fields(reader)
    rows = []
    for tcb, state in sorted(uniq.items(), key=lambda kv: (
            0 if kv[0] == cur else 1, kv[1], kv[0])):
        rec = _task_record(reader, tcb, state, cur, fields)
        if rec:
            rows.append(rec)
    return {"tasks": rows, "count": len(rows), "current_tcb": hex(cur),
            "rtos": "freertos"}


# ── Zephyr（T13）────────────────────────────────────────────────
#: ``k_thread.base.thread_state`` 的位（Zephyr ``kernel_structs.h``）
_Z_THREAD_PENDING = 1 << 1
_Z_THREAD_SUSPENDED = 1 << 2
_Z_THREAD_DEAD = 1 << 3
_Z_THREAD_PRESTART = 1 << 5


def _z_off(reader, field: str) -> int | None:
    # ★ 必须带 ``struct``：4.x 的 ELF 里没有 ``k_thread`` 这个 typedef，
    #   裸 ``k_thread`` 在真机上 "No symbol"（2026-11-19 F411 实测）。
    return _num(reader.ev(f"(long)&((struct k_thread*)0)->{field}"))


def _z_current(reader) -> int | None:
    """Zephyr 3.x/4.x 用 ``cpus[0].current``；2.x 用 ``_kernel.current``。"""
    for expr in ("_kernel.cpus[0].current", "_kernel.current"):
        v = _num(reader.ev(expr))
        if v is not None:
            return v
    return None


def _z_state(bits: int | None, current: bool) -> str:
    if bits is None:
        return "running" if current else "unknown"
    if bits & _Z_THREAD_DEAD:
        return "dead"
    if bits & _Z_THREAD_SUSPENDED:
        return "suspended"
    if bits & _Z_THREAD_PENDING:
        return "pending"
    if bits & _Z_THREAD_PRESTART:
        return "prestart"
    return "running" if current else "ready"


def _cstr(reader, addr: int, cap: int = 64) -> str:
    """读 C 字符串；``addr`` 为 0 或读不到 → 空串。"""
    if not addr:
        return ""
    out = b""
    while len(out) < cap:
        chunk = reader.mem(addr + len(out), min(16, cap - len(out)))
        if not chunk:
            break
        out += bytes(chunk)
        if b"\x00" in out:
            break
    raw = out.split(b"\x00", 1)[0]
    return raw.decode("latin-1", "replace").strip()


def _scan_free_bytes(reader, stack: int,
                     cap_words: int = 256) -> tuple[int, bool]:
    """Zephyr：从栈底向上数连续 0xAA 字。"""
    return _scan_fill(reader, stack, Z_FILL_BYTE, cap_words)


def _z_thread_name(reader, thread: int, psz: int, offs: dict) -> str:
    """Zephyr 3.x ``char *name`` 与 4.x ``char name[]`` 都支持。

    区分方式：``sizeof(thread->name)`` 等于指针宽度 → 指针；否则是内联数组。
    两代符号都缺 → ``(unnamed)``。
    """
    off = offs.get("name")
    if off is None:
        return "(unnamed)"
    nsize = offs.get("name_size")
    if nsize and nsize != psz:
        data = reader.mem(thread + off, min(int(nsize), 64))
        if not data:
            return "(unnamed)"
        raw = bytes(data).split(b"\x00", 1)[0]
        text = raw.decode("latin-1", "replace").strip()
        return text or "(unnamed)"
    name_ptr = _ptr_n(reader, thread + off, psz)
    return _cstr(reader, name_ptr or 0) or "(unnamed)"


def _z_thread_record(reader, thread: int, cur: int, psz: int,
                     offs: dict, *, measure: bool = True) -> dict:
    """单个 ``k_thread`` → 与 FreeRTOS 对齐的行（字段缺了就 null）。"""
    name = _z_thread_name(reader, thread, psz, offs)

    prio = None
    if offs["prio"] is not None:
        data = reader.mem(thread + offs["prio"], 1)
        if data:
            prio = int.from_bytes(data[:1], "little", signed=True)

    bits = None
    if offs["state"] is not None:
        data = reader.mem(thread + offs["state"], 1)
        if data:
            bits = data[0]

    stack = None
    if offs["stack"] is not None:
        stack = _ptr_n(reader, thread + offs["stack"], psz)
    size = None
    if offs["size"] is not None:
        size = _ptr_n(reader, thread + offs["size"], psz)
    if size == 0:
        size = None

    free_words, scanned = (None, False)
    if stack and measure:
        cap = min(max((size or 0) // 4, 1), 2048) if size else 256
        free_words, scanned = _scan_free_bytes(reader, stack, cap)

    free_bytes = free_words * 4 if free_words is not None else None
    used_bytes = None
    used_pct = None
    if size and free_bytes is not None and free_bytes <= size:
        used_bytes = size - free_bytes
        used_pct = round(used_bytes * 100.0 / size, 1)

    is_cur = thread == cur
    return {
        "name": name,
        "priority": prio,
        "state": _z_state(bits, is_cur),
        "current": is_cur,
        "tcb": hex(thread),
        "stack_start": hex(stack) if stack else None,
        "stack_end": hex(stack + size) if stack and size else None,
        "stack_size_bytes": size,
        "free_words": free_words,
        "free_bytes": free_bytes,
        "high_water_mark": free_words,
        "used_bytes": used_bytes,
        "used_pct": used_pct,
        "health": (_health(free_words, used_pct)
                   if free_words is not None else "unknown"),
        "scanned": scanned,
    }


def _z_walk_threads(reader, psz: int, offs: dict) -> tuple[list[int], str]:
    """枚举线程地址，返回 (threads, source)。

    三代布局，优先级从"全"到"少"：

    1. Zephyr 4.x ``_kernel.threads``：``struct k_thread *``，沿
       ``thread->next_thread`` 单链（需 ``CONFIG_THREAD_MONITOR``）。
    2. Zephyr 3.x ``_kernel.threads``：``sys_slist_t``，沿
       ``base.qnode.next`` 走，节点减 ``offsetof(base.qnode)`` 得线程。
    3. 4.x ``_kernel.ready_q.runq``：``sys_dlist_t``，节点就是
       ``&thread->base.qnode_dlist``。**不需要** ``CONFIG_THREAD_MONITOR``，
       代价是只看得到就绪线程（阻塞/挂起的不在表里）。
    """
    def walk(node: int, next_of, to_thread) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        while node and node not in seen and len(out) < _MAX_ITEMS:
            seen.add(node)
            thread = to_thread(node)
            if thread:
                out.append(thread)
            node = next_of(node) or 0
        return out

    # 1) 4.x k_thread 链表
    raw = reader.ev("_kernel.threads")
    off_next = offs.get("next")
    if raw is not None and off_next is not None:
        head = _num(raw) or 0
        return walk(head, lambda n: _ptr_n(reader, n + off_next, psz),
                    lambda n: n), "monitor"

    # 2) 3.x sys_slist_t
    head_raw = reader.ev("_kernel.threads.head")
    off_node = offs.get("node")
    if head_raw is not None and off_node is not None:
        head = _num(head_raw) or 0
        return walk(head, lambda n: _ptr_n(reader, n, psz),
                    lambda n: n - off_node), "monitor"

    # 3) 4.x 就绪队列 sys_dlist_t
    head_raw = reader.ev("_kernel.ready_q.runq.head")
    off_dlist = offs.get("dlist")
    if head_raw is not None and off_dlist is not None:
        sentinel = _num(reader.ev("(long)&_kernel.ready_q.runq")) or 0
        out: list[int] = []
        seen: set[int] = set()
        node = _num(head_raw) or 0
        while (node and node != sentinel and node not in seen
               and len(out) < _MAX_ITEMS):
            seen.add(node)
            out.append(node - off_dlist)
            node = _ptr_n(reader, node, psz) or 0
        return out, "readyq"

    return [], "current"


def _collect_zephyr(reader, *, config: dict | None = None) -> dict:
    """Zephyr：线程表遍历 + 栈水位。"""
    cur = _z_current(reader)
    if cur is None:
        inner = getattr(reader, "last_error", "")
        raise RtosUnavailable(
            "找不到 pxCurrentTCB，也没有 Zephyr 的 _kernel（工程未使用 "
            "FreeRTOS/Zephyr，或调试符号被裁剪）"
            + (f"：{inner}" if inner else ""))
    if not cur:
        return {"tasks": [], "count": 0, "current_tcb": "0x0", "rtos": "zephyr",
                "note": "调度器尚未启动（_kernel current = 0）"}

    offs = {
        "node": _z_off(reader, "base.qnode"),
        "dlist": _z_off(reader, "base.qnode_dlist"),
        "next": _z_off(reader, "next_thread"),
        "name": _z_off(reader, "name"),
        "name_size": _num(reader.ev("sizeof(((struct k_thread*)0)->name)")),
        "prio": _z_off(reader, "base.prio"),
        "state": _z_off(reader, "base.thread_state"),
        "stack": _z_off(reader, "stack_info.start"),
        "size": _z_off(reader, "stack_info.size"),
    }
    psz = _num(reader.ev("sizeof(void*)")) or 4
    measure = ((config or {}).get("flags") or {}).get("INIT_STACKS") is not False

    threads, source = _z_walk_threads(reader, psz, offs)
    # 就绪队列里没有当前线程 / idle / 下一个可运行线程：补上，表才完整可读。
    for expr in ("_kernel.cpus[0].current", "(long)_kernel.ready_q.cache",
                 "(long)_kernel.cpus[0].idle_thread"):
        want = _num(reader.ev(expr))
        if want and want not in threads:
            threads.append(want)

    rows = [_z_thread_record(reader, t, cur, psz, offs, measure=measure)
            for t in threads[:_MAX_ITEMS]]
    rows.sort(key=lambda r: (0 if r["current"] else 1,
                             r["priority"] if r["priority"] is not None else 999,
                             r["tcb"]))

    out = {"tasks": rows, "count": len(rows), "current_tcb": hex(cur),
           "rtos": "zephyr", "thread_source": source}
    if source == "readyq":
        out["thread_note"] = (
            "CONFIG_THREAD_MONITOR 未开启：线程表取自就绪队列，"
            "阻塞/挂起线程不会出现")
    elif source == "current":
        out["thread_note"] = (
            "未找到可枚举的线程表：仅显示当前线程；"
            "开启 CONFIG_THREAD_MONITOR=y 可看完整线程视图")
    return out

