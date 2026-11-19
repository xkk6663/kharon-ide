"""Zephyr 专属调试区（``elab.debug.zephyr``）离线单测。

    python -m unittest tests.test_zephyr_debug

覆盖：

- ``.config`` 解析（含 ``# ... is not set``）与 board/soc；
- 静态内核对象迭代段：``_k_<type>_list_start/end`` + ``sizeof`` + 字段偏移；
- 段为空 / 缺符号 / 缺类型信息三种降级；
- Zephyr 4.x 的两种线程表来源：``_kernel.threads`` 单链、
  ``_kernel.ready_q.runq`` 双向链（无 ``CONFIG_THREAD_MONITOR``）；
- 目标不是 Zephyr → ``ZephyrUnavailable``。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from elab import zephyr as zephyr_mod                        # noqa: E402
from elab import rtos as rtos_mod                            # noqa: E402


class FakeReader:
    def __init__(self, truth=None, mem=None):
        self.truth = dict(truth or {})
        self.mem_map = dict(mem or {})

    def ev(self, expr):
        return self.truth.get(expr)

    def mem(self, addr, size):
        return bytes(self.mem_map.get(addr + i, 0) for i in range(size))


def _put(mem, addr, data):
    for i, b in enumerate(data):
        mem[addr + i] = b


def _ptr(mem, addr, value):
    _put(mem, addr, int(value).to_bytes(4, "little"))


# ── 配置事实 ────────────────────────────────────────────────────

class ConfigFactsTest(unittest.TestCase):
    def test_parses_flags_board_and_soc(self):
        with tempfile.TemporaryDirectory() as tmp:
            zdir = Path(tmp) / "zephyr"
            zdir.mkdir()
            (zdir / ".config").write_text(
                "# comment\n"
                "CONFIG_THREAD_MONITOR=y\n"
                "# CONFIG_THREAD_NAME is not set\n"
                "CONFIG_INIT_STACKS=y\n"
                "# CONFIG_OBJ_CORE is not set\n"
                'CONFIG_BOARD_TARGET="blackpill_f411ce"\n'
                'CONFIG_SOC="stm32f411xe"\n',
                encoding="utf-8",
            )
            facts = zephyr_mod.config_facts(tmp)
        self.assertTrue(facts["ok"])
        self.assertEqual(facts["board"], "blackpill_f411ce")
        self.assertEqual(facts["soc"], "stm32f411xe")
        self.assertIs(facts["flags"]["THREAD_MONITOR"], True)
        self.assertIs(facts["flags"]["THREAD_NAME"], False)
        self.assertIs(facts["flags"]["OBJ_CORE"], False)

    def test_missing_config_is_not_an_error(self):
        facts = zephyr_mod.config_facts("/definitely/not/here")
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["flags"], {})


# ── 内核对象 ────────────────────────────────────────────────────

def _sem_world():
    mem: dict[int, int] = {}
    start, size = 0x20001000, 20
    truth = {
        "(long)&_k_sem_list_start": hex(start),
        "(long)&_k_sem_list_end": hex(start + size * 2),
        "sizeof(struct k_sem)": str(size),
        "(long)&((struct k_sem*)0)->count": "8",
        "sizeof(((struct k_sem*)0)->count)": "4",
        "(long)&((struct k_sem*)0)->limit": "12",
        "sizeof(((struct k_sem*)0)->limit)": "4",
        # 段非空但 ELF 缺类型信息
        "(long)&_k_msgq_list_start": hex(0x20002000),
        "(long)&_k_msgq_list_end": hex(0x20002090),
        # 段为空
        "(long)&_k_timer_list_start": hex(0x20003000),
        "(long)&_k_timer_list_end": hex(0x20003000),
        # _k_mutex_list_* 故意缺失 → supported=False
    }
    _put(mem, start + 8, (3).to_bytes(4, "little"))
    _put(mem, start + 12, (5).to_bytes(4, "little"))
    _put(mem, start + size + 8, (0).to_bytes(4, "little"))
    _put(mem, start + size + 12, (1).to_bytes(4, "little"))
    return FakeReader(truth, mem)


class KernelObjectsTest(unittest.TestCase):
    def test_lists_static_objects_with_fields(self):
        rows, groups, notes = zephyr_mod._read_objects(_sem_world())
        sems = [r for r in rows if r["type"] == "sem"]
        self.assertEqual(len(sems), 2)
        self.assertEqual(sems[0]["fields"], {"count": 3, "limit": 5})
        self.assertEqual(sems[1]["fields"], {"count": 0, "limit": 1})

        by_type = {g["type"]: g for g in groups}
        self.assertEqual(by_type["sem"]["count"], 2)
        self.assertEqual(by_type["timer"]["count"], 0)
        self.assertIsNone(by_type["msgq"]["count"])
        self.assertFalse(by_type["mutex"]["supported"])
        self.assertTrue(any("k_msgq" in n["text"] for n in notes))

    def test_missing_bounds_marks_group_unsupported(self):
        _, groups, _ = zephyr_mod._read_objects(FakeReader())
        self.assertTrue(all(not g["supported"] for g in groups))
        self.assertEqual({g["count"] for g in groups}, {0})


# ── Zephyr 4.x 线程表 ───────────────────────────────────────────

THREAD = {
    "dlist": 0, "next": 4, "name": 8, "prio": 24, "state": 25,
    "stack": 28, "size": 32,
}
CUR, IDLE = 0x20001000, 0x20001100
STACK_CUR, STACK_IDLE = 0x20002000, 0x20002100


def _thread_offsets(*, qnode=True, dlist=True, next_thread=True):
    truth: dict[str, str] = {
        "(long)&((struct k_thread*)0)->base.qnode_dlist": str(THREAD["dlist"]),
        "(long)&((struct k_thread*)0)->name": str(THREAD["name"]),
        "sizeof(((struct k_thread*)0)->name)": "16",
        "(long)&((struct k_thread*)0)->base.prio": str(THREAD["prio"]),
        "(long)&((struct k_thread*)0)->base.thread_state": str(THREAD["state"]),
        "(long)&((struct k_thread*)0)->stack_info.start": str(THREAD["stack"]),
        "(long)&((struct k_thread*)0)->stack_info.size": str(THREAD["size"]),
        "sizeof(void*)": "4",
        "_kernel.cpus[0].current": hex(CUR),
    }
    if qnode:
        truth["(long)&((struct k_thread*)0)->base.qnode"] = "0"
    if dlist:
        truth["(long)&((struct k_thread*)0)->base.qnode_dlist"] = str(THREAD["dlist"])
    if next_thread:
        truth["(long)&((struct k_thread*)0)->next_thread"] = str(THREAD["next"])
    return truth


def _thread(mem, addr, name, prio, state, stack, size):
    _put(mem, addr + THREAD["name"], name.encode() + b"\x00")
    _put(mem, addr + THREAD["prio"], int(prio).to_bytes(1, "little", signed=True))
    _put(mem, addr + THREAD["state"], bytes([state]))
    _ptr(mem, addr + THREAD["stack"], stack)
    _ptr(mem, addr + THREAD["size"], size)


class Zephyr4ThreadsTest(unittest.TestCase):
    def test_kernel_threads_singly_linked(self):
        mem: dict[int, int] = {}
        truth = _thread_offsets()
        truth["_kernel.threads"] = hex(CUR)
        _thread(mem, CUR, "main", 0, 0, STACK_CUR, 0x100)
        _thread(mem, IDLE, "idle", 15, 0x04, STACK_IDLE, 0x80)
        _ptr(mem, CUR + THREAD["next"], IDLE)
        _ptr(mem, IDLE + THREAD["next"], 0)
        _put(mem, STACK_CUR, b"\xAA" * 96)
        _put(mem, STACK_IDLE, b"\xAA" * 16)

        out = rtos_mod.collect(FakeReader(truth, mem))
        self.assertEqual(out["thread_source"], "monitor")
        self.assertEqual(out["count"], 2)
        by_name = {t["name"]: t for t in out["tasks"]}
        self.assertEqual(set(by_name), {"main", "idle"})
        self.assertEqual(by_name["main"]["free_bytes"], 96)
        self.assertEqual(by_name["idle"]["state"], "suspended")

    def test_ready_queue_fallback_without_thread_monitor(self):
        mem: dict[int, int] = {}
        truth = _thread_offsets(qnode=False, dlist=True, next_thread=False)
        sentinel = 0x200000A0
        truth["_kernel.ready_q.runq.head"] = hex(CUR)
        truth["(long)&_kernel.ready_q.runq"] = hex(sentinel)
        _thread(mem, CUR, "main", 0, 0, STACK_CUR, 0x100)
        _thread(mem, IDLE, "idle", 15, 0, STACK_IDLE, 0x80)
        _ptr(mem, CUR + THREAD["dlist"], IDLE)
        _ptr(mem, IDLE + THREAD["dlist"], sentinel)

        out = rtos_mod.collect(FakeReader(truth, mem))
        self.assertEqual(out["thread_source"], "readyq")
        self.assertEqual({t["name"] for t in out["tasks"]}, {"main", "idle"})
        self.assertIn("THREAD_MONITOR", out["thread_note"])


# ── 入口 ────────────────────────────────────────────────────────

class CollectTest(unittest.TestCase):
    def test_rejects_non_zephyr_target(self):
        with self.assertRaises(zephyr_mod.ZephyrUnavailable):
            zephyr_mod.collect(FakeReader())

    def test_kernel_overview(self):
        truth = {
            "_kernel.cpus[0].current": hex(CUR),
            "(long)_kernel.ready_q.cache": hex(IDLE),
            "(long)_kernel.cpus[0].idle_thread": hex(IDLE),
            "(long)_kernel.cpus[0].irq_stack": hex(0x20003000),
            "_kernel.cpus[0].nested": "1",
            "sizeof(_kernel.cpus)/sizeof(_kernel.cpus[0])": "1",
        }
        kernel = zephyr_mod.collect_kernel(FakeReader(truth))
        self.assertEqual(kernel["current"], hex(CUR))
        self.assertEqual(kernel["ready_cache"], hex(IDLE))
        self.assertEqual(kernel["idle"], hex(IDLE))
        self.assertEqual(kernel["nested"], 1)
        self.assertEqual(kernel["cpu_count"], 1)

    def test_collect_merges_threads_objects_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            zdir = Path(tmp) / "zephyr"
            zdir.mkdir()
            (zdir / ".config").write_text(
                "# CONFIG_THREAD_MONITOR is not set\n"
                "# CONFIG_INIT_STACKS is not set\n"
                "# CONFIG_OBJ_CORE is not set\n",
                encoding="utf-8",
            )
            mem: dict[int, int] = {}
            truth = _thread_offsets(qnode=False, dlist=True, next_thread=False)
            truth["_kernel.ready_q.runq.head"] = hex(CUR)
            truth["(long)&_kernel.ready_q.runq"] = hex(0x200000A0)
            _thread(mem, CUR, "main", 0, 0, STACK_CUR, 0x100)

            out = zephyr_mod.collect(FakeReader(truth, mem), work_dir=tmp)

        self.assertEqual(out["rtos"], "zephyr")
        self.assertEqual(out["thread_source"], "readyq")
        # INIT_STACKS=n → 不做 0xAA 扫描，避免必然误报的 "100% 已用"
        self.assertIsNone(out["threads"][0]["free_words"])
        levels = [n["level"] for n in out["notes"]]
        self.assertIn("warn", levels)
        self.assertTrue(any("INIT_STACKS" in n["text"] for n in out["notes"]))
        self.assertTrue(out["object_groups"])


if __name__ == "__main__":
    unittest.main()
