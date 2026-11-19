"""``/api/runs/export`` 的 Markdown 渲染（T7）单测。

     python -m unittest tests.test_events_export

验收判据（方案 §5.3）：每行结论都能追溯到事件 ``#seq``，数字全部抄自
事件流（不重算）。这里用 ``EventLog`` 现造一个失败 run，逐项核对。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from elab.core.kernel import events as ev                        # noqa: E402


class ExportMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rd = Path(self.tmp.name)
        log = ev.EventLog(self.rd, run_id="r-export", project="demo")
        self.log = log
        log.start(["build", "report"], actor=ev.ACTOR_AGENT)        # #1
        log.step_enter("build", 1, 2)                               # #2
        log.proc("build", "$ ninja -C build")                       # #3
        log.proc("build", "ninja: error: undefined symbol", stream="stderr")  # #4
        log.step_exit(                                              # #5
            "build", False, "ninja: error: undefined symbol",
            duration_s=1.234,
            memory={"FLASH": {"used": 1024, "region": 2048, "pct": 50.0}},
            memory_source="elf",
        )
        log.end(False, steps_ok=[], steps_failed=["build"], verdict="failed")  # #6

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_every_conclusion_carries_seq(self):
        md = ev.export_markdown(self.rd, "r-export")
        self.assertIn("# 证据导出：r-export", md)
        self.assertIn("| 工程 | `demo` | `#1` `run/start` |", md)
        self.assertIn("**failed**", md)
        # 步骤结论：数值/说明全部来自 #5
        self.assertIn("| `build` | ✗ failed | ninja: error: undefined symbol | "
                      "1.234s | `#5` |", md)
        # 内存：不重算，直接抄 #5 的 memory
        self.assertIn("| FLASH | 1.0 KB | 2.0 KB | 50.0% | `elf` | `#5` |", md)
        # 尾部日志：逐行带 seq + 流名
        self.assertIn("#3 [out] $ ninja -C build", md)
        self.assertIn("#4 [err] ninja: error: undefined symbol", md)
        # 命令行事件没带 → 明确说"不现算"
        self.assertIn("导出不现算命令", md)
        # 事实源文件都在
        self.assertIn("r-export.jsonl", md)
        self.assertIn("r-export.proc.jsonl", md)

    def test_missing_run_raises(self):
        with self.assertRaises(FileNotFoundError):
            ev.export_markdown(self.rd, "nope")

    def test_tail_limit(self):
        md = ev.export_markdown(self.rd, "r-export", tail_lines=1)
        self.assertNotIn("#3 [out] $ ninja -C build", md)
        self.assertIn("#4 [err]", md)


if __name__ == "__main__":
    unittest.main()
