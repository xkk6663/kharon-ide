"""调试会话状态机 —— 用假 gdb 跑通完整链路（C3，无硬件、无真 gdb）。

覆盖的不只是"能开起来"：**失败与降级路径**才是这条链路的真风险点
（断点设不上要降级到复位、连不上要给出可读原因、写变量要能读回）。

    python -m unittest tests.test_debugsession_fake
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "tests"))

from elab.debugsession import DebugError, DebugSession       # noqa: E402
from elab.gdbmi import MiError                              # noqa: E402
from fake_gdb import FakeGdb                                # noqa: E402


class _Cfg:
    def __init__(self, root):
        self.root = root


class _Plan:
    """最小 plan：只提供会话构造与配置存储要用的字段。"""

    def __init__(self, root):
        self.name = "fake-proj"
        self.source_dir = str(root)
        self.cfg = _Cfg(root)

    def debug_profile(self):
        return {"load": False}      # 不下载，避免依赖 ELF 解析


class _FakeSession(DebugSession):
    """把 openocd/gdb 的**进程**替换掉，其余逻辑全部走真实实现。"""

    def __init__(self, root, fake: FakeGdb):
        super().__init__(_Plan(root), emit=lambda t, d: None)
        self._fake = fake

    def _validate(self) -> None:
        return

    def _start_openocd(self) -> None:
        self.port = 3333

    def _start_gdb(self) -> None:
        self._gdb = self._fake
        self._gdb.on_async = self._on_async
        self._gdb.on_stream = self._on_stream


class SessionFakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects").mkdir(parents=True, exist_ok=True)
        (self.root / "main.c").write_text("int main(){}" + chr(10), encoding="utf-8")
        self.fake = FakeGdb()
        self.sess = _FakeSession(self.root, self.fake)

    def tearDown(self):
        try:
            self.sess.close()
        except Exception:                       # noqa: BLE001
            pass
        self.tmp.cleanup()

    # ── 顺利路径 ────────────────────────────────────────────────
    def test_open_lands_on_main(self):
        snap = self.sess.open()
        self.assertEqual(self.sess.state, "stopped")
        self.assertEqual(snap["stop"]["frame"]["func"], "main")
        # 真的走完了约定的一串 MI：连 server → reset halt → 建临时断点 → continue
        joined = "\n".join(self.fake.commands)
        self.assertIn("-target-select extended-remote", joined)
        self.assertIn("monitor reset halt", joined)
        self.assertIn("-break-insert -t main", joined)
        self.assertIn("-exec-continue", joined)

    def test_continue_then_interrupt(self):
        self.sess.open()
        # 真 gdb 是异步的：这里关掉"立刻停"，才是真实的 running 语义
        self.fake.auto_stop = False
        self.sess.control("continue")
        self.assertEqual(self.sess.state, "running")
        self.sess.control("pause")
        self.assertEqual(self.sess.state, "stopped")

    def test_write_var_through_full_session(self):
        self.sess.open()
        out = self.sess.write_var(name="motor.kp", value="2.5")
        self.assertTrue(out["ok"])
        self.assertEqual(out["before"], "1.0")
        self.assertEqual(out["after"], "2.5")
        # 类型不能是空串：写 3.7 进 int 变量而不报类型，等于让用户自己猜截断
        self.assertEqual(out["type"], "float")

    def test_evaluate_reports_type_without_persisting(self):
        self.sess.open()
        out = self.sess.evaluate(expr="g_counter")
        self.assertEqual(out["value"], "17")
        self.assertEqual(out["type"], "uint32_t")
        # 一次算完不落盘：监视列表必须仍然是空的
        self.assertEqual(self.sess.watch()["watches"], [])

    def test_evaluate_bad_expression_is_readable(self):
        self.sess.open()
        with self.assertRaises(DebugError) as ctx:
            self.sess.evaluate(expr="no_such_symbol")
        self.assertEqual(ctx.exception.code, "debug-bad-request")

    def test_registers_and_memory_roundtrip(self):
        self.sess.open()
        addr = 0x2000_0000
        self.sess.write_memory(addr=hex(addr), value="deadbeef")
        out = self.sess.memory(addr=hex(addr), count=4)
        self.assertEqual(out["bytes"].lower().replace(" ", ""), "deadbeef")

    def test_globals_and_statics_carry_file_from_parent_node(self):
        """文件名继承：真 gdb 把 filename 放在父层，符号自己没有 file。

        只看符号自身的 file → 全部为空串 → statics 的"按当前文件过滤"把整表
        滤空（真机上表现为「全局有、static 永远没有」）。
        """
        self.sess.open()
        g = self.sess.variables(scope="globals")
        names = {n["name"]: n["file"] for n in g["nodes"]}
        self.assertEqual(names.get("g_counter"), "/fake/main.c")
        self.assertEqual(names.get("other_counter"), "/fake/other.c")
        # statics 只留当前停住的文件（main.c）
        st = self.sess.variables(scope="statics")
        got = {n["name"] for n in st["nodes"]}
        self.assertIn("g_counter", got)
        self.assertNotIn("other_counter", got)

    def test_globals_empty_carries_reason_not_silent(self):
        """老 gdb 没有 -symbol-info-variables 时：空表 + 原因，不能是裸空数组。

        「这个工程没有全局变量」和「这个 gdb 不支持枚举」的下一步动作完全相反，
        合并成同一个 ``nodes: []`` 就是把两种结论伪装成一种。
        """
        self.sess.open()
        self.fake.fail("-symbol-info-variables", MiError("Undefined MI command"))
        out = self.sess.variables(scope="globals")
        self.assertEqual(out["nodes"], [])
        self.assertIn("不支持符号枚举", out["notice"])
        # 有符号时 notice 必须为空（否则正常工程也会挂一条警告）
        self.fake.heal()
        ok = self.sess.variables(scope="globals")
        self.assertTrue(ok["nodes"])
        self.assertEqual(ok["notice"], "")

    # ── 故障注入 ────────────────────────────────────────────────
    def test_gdb_server_unreachable_is_reported(self):
        self.fake.fail("-target-select", MiError("Connection refused"))
        with self.assertRaises(DebugError) as ctx:
            self.sess.open()
        self.assertEqual(ctx.exception.code, "debug-gdb-missing")
        # 失败后会话不能停在"半开"状态（资源要回收，状态要如实）
        self.assertEqual(self.sess.state, "error")

    def test_breakpoint_insert_failure_degrades_to_reset(self):
        # main 符号缺失是真实世界里最常见的降级场景：不能因此开不了会话
        self.fake.fail("-break-insert", MiError("No symbol table"))
        snap = self.sess.open()
        self.assertEqual(self.sess.state, "stopped")
        self.assertIn("warn", snap["stop"])
        self.assertIn("main", snap["stop"]["warn"])

    def test_reset_failure_is_fatal_with_readable_reason(self):
        # 真实命令形如 -interpreter-exec console "monitor reset halt"，
        # 前缀必须带上 -interpreter-exec 才匹配得上（这一条本身也是"注入要打在
        # 实际命令形状上"的教训）
        self.fake.fail('-interpreter-exec console "monitor reset halt"',
                       MiError("reset failed"))
        # monitor 失败会退回 `reset halt`；两条都失败才报错
        self.fake.fail('-interpreter-exec console "reset halt"',
                       MiError("reset failed"))
        with self.assertRaises(DebugError):
            self.sess.open()

    def test_close_is_idempotent(self):
        self.sess.open()
        self.sess.close()
        self.assertEqual(self.sess.state, "closed")
        self.sess.close()                       # 再关一次不该炸


if __name__ == "__main__":
    unittest.main(verbosity=2)
