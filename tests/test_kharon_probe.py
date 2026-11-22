"""``kharon adapt``（T12）识别/渲染单测。

    python -m unittest tests.test_kharon_probe

不依赖真实芯片包：用 ``SimpleNamespace(root=..., all_chips=...)`` 模拟
``Config`` 的最小读口，验证「CMakeLists 判定 / boards 反解芯片 / 多板歧义 /
yaml 形状」。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from elab import _yaml                                          # noqa: E402
from elab.platform import kharon_probe                          # noqa: E402
from elab.core.config import ElabError                          # noqa: E402

CHIPS = {
    "zephyr/blackpill_f411ce": {"id": "blackpill_f411ce", "target": "blackpill_f411ce",
                                "debug": {"openocd_config": "x.cfg",
                                          "gdb_key": "gdb_zephyr_arm"}},
    "blackpill_f411ce": {"id": "blackpill_f411ce", "target": "blackpill_f411ce",
                         "debug": {"openocd_config": "x.cfg"}},
    "zephyr/esp32s3_devkitc": {"id": "esp32s3_devkitc",
                               "target": "esp32s3_devkitc/esp32s3/procpu",
                               "debug": {"gdb_key": "gdb_zephyr_xtensa"}},
    "esp32s3_devkitc": {"id": "esp32s3_devkitc",
                        "target": "esp32s3_devkitc/esp32s3/procpu",
                        "debug": {"gdb_key": "gdb_zephyr_xtensa"}},
}


class KharonProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 同上：CI 的 TEMP 是 8.3 短名，产品侧返回 resolve() 后的长名。
        self.root = Path(self.tmp.name).resolve()
        self.cfg = SimpleNamespace(root=self.root, all_chips=CHIPS)
        self.app = self.root / "applications" / "stm32f4" / "f411_breath"
        self.app.mkdir(parents=True)
        (self.app / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20.0)\n"
            "project(f411_breath)\n"
            "find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})\n",
            encoding="utf-8")
        (self.app / "prj.conf").write_text("CONFIG_LOG=y\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _board(self, stem: str):
        d = self.app / "boards"
        d.mkdir(exist_ok=True)
        (d / f"{stem}.overlay").write_text("/ { };\n", encoding="utf-8")

    def test_single_board_is_resolved_without_guess(self):
        self._board("blackpill_f411ce")
        p = kharon_probe.probe(self.cfg, self.app)
        self.assertEqual(p["name"], "f411_breath")
        self.assertEqual(p["chip"], "blackpill_f411ce")
        self.assertEqual(p["root"], "applications/stm32f4/f411_breath")
        self.assertEqual(p["work_dir"], "build/stm32f4/f411_breath/default")
        self.assertEqual(p["probe"], "dap")
        self.assertEqual(p["ambiguities"], [])

    def test_multiple_boards_require_explicit_choice(self):
        self._board("blackpill_f411ce")
        self._board("esp32s3_devkitc_esp32s3_procpu")
        p = kharon_probe.probe(self.cfg, self.app)
        self.assertEqual(p["chip"], "")
        self.assertTrue(any("多个芯片候选" in a for a in p["ambiguities"]))

        p2 = kharon_probe.probe(self.cfg, self.app, board="esp32s3_devkitc")
        self.assertEqual(p2["chip"], "esp32s3_devkitc")
        self.assertEqual(p2["probe"], "")      # Xtensa 无整份 openocd cfg

    def test_west_top_is_detected(self):
        (self.root / ".west").mkdir()
        (self.root / ".west" / "config").write_text("[manifest]\n", encoding="utf-8")
        self._board("blackpill_f411ce")
        p = kharon_probe.probe(self.cfg, self.app)
        self.assertEqual(p["west_top"], str(self.root).replace("\\", "/"))

    def test_render_yaml_roundtrips(self):
        self._board("blackpill_f411ce")
        p = kharon_probe.probe(self.cfg, self.app)
        text = kharon_probe.render_yaml(p)
        data = _yaml.load(text)
        self.assertEqual(data["name"], "f411_breath")
        self.assertEqual(data["chip"], "blackpill_f411ce")
        self.assertEqual(data["archetype"], "zephyr")
        self.assertEqual(data["provider"], "zephyr/west@>=1 <2")
        self.assertEqual(data["build"]["work_dir"],
                         "${ELAB_ROOT}/build/stm32f4/f411_breath/default")
        self.assertEqual(data["debug"]["probe"], "dap")
        self.assertEqual(data["artifacts"]["elf"],
                         "${work_dir}/zephyr/zephyr.elf")
        self.assertIn("idle_timeout_s", data["monitor"])

    def test_non_zephyr_is_rejected(self):
        (self.app / "CMakeLists.txt").write_text("project(not_zephyr)\n",
                                                 encoding="utf-8")
        with self.assertRaises(ElabError):
            kharon_probe.probe(self.cfg, self.app)


if __name__ == "__main__":
    unittest.main()
