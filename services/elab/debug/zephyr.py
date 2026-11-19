"""elab.debug.zephyr —— Zephyr 专属调试视图（内核概览 / 内核对象 / 配置事实）。

与 :mod:`elab.debug.rtos` 的分工：

- ``rtos`` 只负责**线程 + 栈水位**（FreeRTOS/Zephyr 同一契约）；
- 本模块补 Zephyr 独有的「内核态」信息，供调试区 ``zephyr`` 页签使用：
  ``_kernel`` 概览、线程表（复用 ``rtos``）、静态内核对象、``.config`` 能力。

数据源全部来自 gdb 表达式 + 目标内存读，**不猜**：

1. 内核对象来自 Zephyr 的 iterable sections：
   ``K_SEM_DEFINE`` 等静态定义会把对象放进 ``._k_sem.static.*`` 段，
   链接脚本导出 ``_k_<type>_list_start/end``；步长取
   ``sizeof(struct k_<type>)``，字段偏移取 ``&((struct k_<type> *)0)->field``。
   运行时动态创建的对象不在该段里 —— 需要 ``CONFIG_OBJ_CORE``，见 ``notes``。
2. 配置事实读构建目录的 ``zephyr/.config``；读不到就只给符号能证明的部分。
3. 线程表完全交给 :func:`elab.debug.rtos.collect`（已覆盖 3.x/4.x 两种布局）。

``collect(reader, work_dir=...)`` 是纯逻辑；``reader`` 只需 ``ev(expr)`` /
``mem(addr, size)``，单测用 fake reader（见 ``tests/test_zephyr_debug.py``）。
"""

from __future__ import annotations

from pathlib import Path

from elab.debug import rtos as rtos_mod

_MAX_PER_TYPE = 32
_MAX_OBJECTS = 128

#: 静态对象迭代段 → (C 类型, ((字段, 是否指针), ...))
OBJECT_GROUPS: tuple[tuple[str, str, tuple[tuple[str, bool], ...]], ...] = (
    ("sem", "k_sem", (("count", False), ("limit", False))),
    ("mutex", "k_mutex", (("owner", True), ("lock_count", False))),
    ("msgq", "k_msgq", (("msg_size", False), ("max_msgs", False),
                        ("used_msgs", False))),
    ("timer", "k_timer", (("status", False),)),
    ("mem_slab", "k_mem_slab", (("num_blocks", False), ("block_size", False),
                                ("num_used", False))),
    ("heap", "k_heap", ()),
    ("pipe", "k_pipe", (("size", False),)),
    ("stack", "k_stack", (("num_entries", False), ("entry_size", False))),
    ("condvar", "k_condvar", ()),
    ("event", "k_event", ()),
    ("fifo", "k_fifo", ()),
    ("lifo", "k_lifo", ()),
    ("queue", "k_queue", ()),
    ("mbox", "k_mbox", ()),
)

#: 会直接影响本视图可读性的内核选项（缺省视为"未知"，不硬判）
_CONFIG_KEYS = ("CONFIG_THREAD_MONITOR", "CONFIG_THREAD_NAME",
                "CONFIG_INIT_STACKS", "CONFIG_OBJ_CORE",
                "CONFIG_THREAD_STACK_INFO")


class ZephyrUnavailable(RuntimeError):
    """目标不是 Zephyr，或符号里没有可用的内核态信息。"""


def _num(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        try:
            return int(s.split()[0], 0)
        except ValueError:
            return None


def _ev(reader, expr: str) -> int | None:
    return _num(reader.ev(expr))


def _hex(v: int | None) -> str | None:
    return hex(v) if v else None


# ── 构建期配置事实 ──────────────────────────────────────────────

def config_facts(work_dir: str) -> dict:
    """读 ``<work_dir>/zephyr/.config``，只摘 Zephyr 调试视图关心的开关。"""
    root = str(work_dir or "").strip()
    if not root:
        return {"ok": False, "flags": {}, "board": "", "soc": ""}
    path = Path(root) / "zephyr" / ".config"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"ok": False, "file": str(path), "flags": {},
                "board": "", "soc": ""}

    flags: dict[str, bool] = {}
    board = ""
    soc = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and line.endswith(" is not set"):
            key = line[2:-11].strip()
            if key in _CONFIG_KEYS:
                flags[key[len("CONFIG_"):]] = False
            continue
        if line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if key in _CONFIG_KEYS:
            flags[key[len("CONFIG_"):]] = value not in ("n", "")
        elif key == "CONFIG_BOARD":
            # 短名优先；CONFIG_BOARD_TARGET 是带 SoC 限定的 "board/soc"
            board = value.strip('"')
        elif key == "CONFIG_BOARD_TARGET" and not board:
            board = value.strip('"')
        elif key == "CONFIG_SOC":
            soc = value.strip('"')
    return {"ok": True, "file": str(path), "flags": flags,
            "board": board, "soc": soc}


def _notes(config: dict, thread_source: str) -> list[dict]:
    """把「为什么这里看不到 X」翻译成可执行的 prj.conf 建议。"""
    out: list[dict] = []
    flags = config.get("flags") or {}
    if not config.get("ok"):
        out.append({"level": "info",
                    "text": "未找到 zephyr/.config：内核配置未知，线程/对象视图按符号能力降级。"})
        return out

    if flags.get("THREAD_MONITOR") is False:
        out.append({"level": "warn",
                    "text": "CONFIG_THREAD_MONITOR 未开启：线程表只能从就绪队列推导，"
                            "阻塞/挂起线程不可见；prj.conf 加 CONFIG_THREAD_MONITOR=y 解锁完整线程视图。"})
    if flags.get("THREAD_NAME") is False:
        out.append({"level": "info",
                    "text": "CONFIG_THREAD_NAME 未开启：k_thread 不保存名字，线程显示为 (unnamed)。"})
    if flags.get("INIT_STACKS") is False:
        out.append({"level": "warn",
                    "text": "CONFIG_INIT_STACKS 未开启：栈没有 0xAA 填充，栈水位不可测（使用率显示 —）。"})
    if flags.get("OBJ_CORE") is False:
        out.append({"level": "info",
                    "text": "CONFIG_OBJ_CORE 未开启：内核对象只列静态定义（K_SEM_DEFINE 等），"
                            "运行时 k_sem_init/k_mutex_init 创建的对象不在迭代段中。"})
    return out


# ── 内核概览 ────────────────────────────────────────────────────

def collect_kernel(reader) -> dict:
    cur = _ev(reader, "_kernel.cpus[0].current")
    if cur is None:
        cur = _ev(reader, "_kernel.current")
    return {
        "current": _hex(cur),
        "ready_cache": _hex(_ev(reader, "(long)_kernel.ready_q.cache")),
        "idle": _hex(_ev(reader, "(long)_kernel.cpus[0].idle_thread")),
        "irq_stack": _hex(_ev(reader, "(long)_kernel.cpus[0].irq_stack")),
        "nested": _ev(reader, "_kernel.cpus[0].nested"),
        "cpu_count": _ev(reader, "sizeof(_kernel.cpus)/sizeof(_kernel.cpus[0])"),
    }


# ── 内核对象 ────────────────────────────────────────────────────

def _bounds(reader, section: str) -> tuple[int | None, int | None]:
    return (_ev(reader, f"(long)&_k_{section}_list_start"),
            _ev(reader, f"(long)&_k_{section}_list_end"))


def _field_offsets(reader, ctype: str,
                   fields) -> dict[str, tuple[int, int, bool]]:
    """字段 → (offset, size, is_ptr)；拿不到的字段直接不出现。"""
    out: dict[str, tuple[int, int, bool]] = {}
    for name, is_ptr in fields:
        off = _ev(reader, f"(long)&((struct {ctype}*)0)->{name}")
        if off is None:
            continue
        size = _ev(reader, f"sizeof(((struct {ctype}*)0)->{name})") or 4
        out[name] = (off, size, is_ptr)
    return out


def _read_objects(reader) -> tuple[list[dict], list[dict], list[dict]]:
    rows: list[dict] = []
    groups: list[dict] = []
    notes: list[dict] = []
    for section, ctype, fields in OBJECT_GROUPS:
        start, end = _bounds(reader, section)
        if start is None or end is None:
            groups.append({"type": section, "count": 0, "supported": False})
            continue
        if end <= start:
            groups.append({"type": section, "count": 0, "supported": True})
            continue
        size = _ev(reader, f"sizeof(struct {ctype})")
        if not size:
            groups.append({"type": section, "count": None, "supported": True,
                           "note": "ELF 缺少该类型调试信息"})
            notes.append({"level": "info",
                          "text": f"{ctype}：迭代段存在但 ELF 没有类型信息，无法展开对象。"})
            continue
        total = (end - start) // size
        groups.append({"type": section, "count": total, "supported": True})
        if total * size != end - start:
            notes.append({"level": "info",
                          "text": f"{ctype}：段长度 {end - start}B 不是 sizeof 的整数倍，"
                                  f"可能缺少对齐信息（按 {total} 个解析）。"})

        offs = _field_offsets(reader, ctype, fields) if fields else {}
        for i in range(min(total, _MAX_PER_TYPE)):
            addr = start + i * size
            buf = reader.mem(addr, size)
            item: dict = {"type": section, "addr": hex(addr), "fields": {}}
            if buf:
                for name, (off, width, is_ptr) in offs.items():
                    if len(buf) < off + width:
                        continue
                    value = int.from_bytes(buf[off:off + width], "little")
                    item["fields"][name] = hex(value) if is_ptr else value
            rows.append(item)
            if len(rows) >= _MAX_OBJECTS:
                notes.append({"level": "info",
                              "text": f"内核对象超过 {_MAX_OBJECTS} 个，已截断显示。"})
                return rows, groups, notes
    return rows, groups, notes


# ── 入口 ────────────────────────────────────────────────────────

def collect(reader, *, work_dir: str = "") -> dict:
    """Zephyr 调试区数据；目标不是 Zephyr → :class:`ZephyrUnavailable`。"""
    config = config_facts(work_dir)
    try:
        threads = rtos_mod.collect(reader, config=config)
    except rtos_mod.RtosUnavailable as exc:
        raise ZephyrUnavailable(str(exc)) from exc
    if threads.get("rtos") != "zephyr":
        raise ZephyrUnavailable("目标 RTOS 不是 Zephyr")

    kernel = collect_kernel(reader)
    for task in threads.get("tasks") or []:
        if task.get("current"):
            kernel["current_name"] = task.get("name") or ""
            break

    objects, groups, obj_notes = _read_objects(reader)
    source = threads.get("thread_source") or ""
    out = {
        "rtos": "zephyr",
        "kernel": kernel,
        "threads": threads.get("tasks") or [],
        "thread_source": source,
        "objects": objects,
        "object_groups": groups,
        "config": config,
        "notes": obj_notes + _notes(config, source),
    }
    for key in ("thread_note", "note"):
        if threads.get(key):
            out[key] = threads[key]
    return out
