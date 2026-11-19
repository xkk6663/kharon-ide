"""Zephyr ``k_thread`` 线程栈水位内省（elab.rtos T13）离线单测。

    python -m unittest tests.test_rtos_zephyr

用 fake reader 模拟 Zephyr 的 ``_kernel`` / ``sys_slist_t`` / ``k_thread`` /
栈（``CONFIG_INIT_STACKS`` 的 0xAA 填充），验证：

- ``_kernel.threads.head`` 沿 ``base.qnode`` 节点链遍历；
- 名称（``thread->name`` 指针 → C 串）/ 优先级 / ``thread_state`` 位映射；
- ``stack_info.start/size`` + 0xAA 扫描 → 剩余字 / 使用率 / health；
- 无 ``CONFIG_THREAD_MONITOR``（没有 threads 表）时至少报当前线程；
- 两套 RTOS 都没有 → RtosUnavailable。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from elab import rtos                                         # noqa: E402

# ── fake k_thread 布局 ──────────────────────────────────────────
THREAD = {"qnode": 0, "name": 8, "prio": 16, "state": 17,
          "stack": 20, "size": 24}

CUR, IDLE, SYS = 0x20001000, 0x20001100, 0x20001200
STACK_CUR, STACK_IDLE = 0x20002000, 0x20002100
NAME_CUR, NAME_IDLE = 0x20004000, 0x20004020


class FakeReader:
    def __init__(self, mem, truth):
        self.mem_map = mem
        self.truth = truth

    def ev(self, expr):
        return self.truth.get(expr)

    def mem(self, addr, size):
        return bytes(self.mem_map.get(addr + i, 0) for i in range(size))


def _put(mem, addr, data):
    for i, b in enumerate(data):
        mem[addr + i] = b


def _ptr(mem, addr, value):
    _put(mem, addr, int(value).to_bytes(4, "little"))


def build_world(*, monitor=True):
    mem: dict[int, int] = {}
    truth: dict[str, str] = {
        "(long)&((struct k_thread*)0)->base.qnode": str(THREAD["qnode"]),
        "(long)&((struct k_thread*)0)->name": str(THREAD["name"]),
        "(long)&((struct k_thread*)0)->base.prio": str(THREAD["prio"]),
        "(long)&((struct k_thread*)0)->base.thread_state": str(THREAD["state"]),
        "(long)&((struct k_thread*)0)->stack_info.start": str(THREAD["stack"]),
        "(long)&((struct k_thread*)0)->stack_info.size": str(THREAD["size"]),
        "sizeof(void*)": "4",
        "_kernel.cpus[0].current": hex(CUR),
    }

    def thread(addr, name_ptr, prio, state, stack, size):
        _ptr(mem, addr + THREAD["qnode"], addr)     # sys_snode_t.next（稍后覆写）
        _ptr(mem, addr + THREAD["name"], name_ptr)
        _put(mem, addr + THREAD["prio"], int(prio).to_bytes(1, "little", signed=True))
        _put(mem, addr + THREAD["state"], bytes([state]))
        _ptr(mem, addr + THREAD["stack"], stack)
        _ptr(mem, addr + THREAD["size"], size)

    # 当前线程：state=0 → running；栈 256B，已用 96B（前 96B 是 0xAA）
    thread(CUR, NAME_CUR, 0, 0, STACK_CUR, 0x100)
    _put(mem, NAME_CUR, b"main\x00")
    _put(mem, STACK_CUR, b"\xAA" * 96)

    # idle：suspended（bit2）；栈 128B，仅剩 16B
    thread(IDLE, NAME_IDLE, 15, 0x04, STACK_IDLE, 0x80)
    _put(mem, NAME_IDLE, b"idle\x00")
    _put(mem, STACK_IDLE, b"\xAA" * 16)

    # 无名系统线程：prestart（bit5），无栈信息（stack/size=0）
    thread(SYS, 0, -1, 0x20, 0, 0)

    # 全局线程表：CUR → IDLE → SYS → NULL
    _ptr(mem, CUR + THREAD["qnode"], IDLE)
    _ptr(mem, IDLE + THREAD["qnode"], SYS)
    _ptr(mem, SYS + THREAD["qnode"], 0)
    if monitor:
        truth["_kernel.threads.head"] = hex(CUR)
    return FakeReader(mem, truth)


class ZephyrRtosTest(unittest.TestCase):
    def test_enumerates_threads_and_stack_watermarks(self):
        out = rtos.collect(build_world())
        self.assertEqual(out["rtos"], "zephyr")
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["current_tcb"], hex(CUR))

        by_name = {t["name"]: t for t in out["tasks"]}
        self.assertEqual(set(by_name), {"main", "idle", "(unnamed)"})

        cur = by_name["main"]
        self.assertTrue(cur["current"])
        self.assertEqual(cur["state"], "running")
        self.assertEqual(cur["priority"], 0)
        self.assertEqual(cur["stack_size_bytes"], 0x100)
        self.assertEqual(cur["free_words"], 24)
        self.assertEqual(cur["free_bytes"], 96)
        self.assertEqual(cur["used_bytes"], 0xA0)
        self.assertEqual(cur["used_pct"], 62.5)
        self.assertEqual(cur["health"], "ok")

        idle = by_name["idle"]
        self.assertFalse(idle["current"])
        self.assertEqual(idle["state"], "suspended")
        self.assertEqual(idle["free_words"], 4)
        self.assertEqual(idle["used_pct"], 87.5)
        self.assertEqual(idle["health"], "warn")

        sys_thread = by_name["(unnamed)"]
        self.assertEqual(sys_thread["state"], "prestart")
        self.assertIsNone(sys_thread["stack_size_bytes"])
        self.assertIsNone(sys_thread["free_words"])
        self.assertEqual(sys_thread["health"], "unknown")

    def test_no_thread_monitor_reports_current_only(self):
        out = rtos.collect(build_world(monitor=False))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["tasks"][0]["name"], "main")
        self.assertTrue(out["tasks"][0]["current"])

    def test_no_rtos_symbols(self):
        with self.assertRaises(rtos.RtosUnavailable):
            rtos.collect(FakeReader({}, {}))


if __name__ == "__main__":
    unittest.main()
