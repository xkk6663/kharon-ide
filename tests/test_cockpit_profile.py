"""装配清单（profile）读取用例（stdlib unittest，无需 pytest）。

跑：``python tests/test_cockpit_profile.py``

## 守的是什么

本仓只保留一份生效装配清单（`kharon.yaml`），classic ⇄ Zephyr 的运行期模式切换
已经删除。这里守三件事，都是“界面看起来正常、实际读错文件”的那种：

1. **摘要来自装配清单本身**：`label` / `desc` / `host` / `workspaces` /
   `projects.archetype` 全由 yaml 给，代码里不认厂商名。
2. **激活顺序**：`ELAB_PROFILE` → `host: profile` → 第一份；指向不存在的名字时
   回落 host 声明，而不是崩或空界面。
3. **不再输出模式表**：`_profile_summary` 没有 `modes` 键；`POST /api/mode` 已删，
   前端不应再依赖模式列表。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from cockpit import server as srv                    # noqa: E402
from elab.config import Config                       # noqa: E402


PROFILES = {
    "kharon.yaml": """
profile: kharon
label: zephyr
desc: Kharon Zephyr 单链
host: kharon.host.yaml
workspaces: [arch, debug, build, dash, flash, serial, modbus, matrix, tools]
projects:
  archetype: [zephyr]
""",
    "solo.yaml": """
profile: solo
label: 精简
workspaces: [build]
""",
}


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "cockpit" / "profiles").mkdir(parents=True)
        for name, text in PROFILES.items():
            (root / "cockpit" / "profiles" / name).write_text(text, encoding="utf-8")
        (root / "kharon.host.yaml").write_text(
            "version: 1\nprofile: kharon\nhost: {os: windows}\n", encoding="utf-8")
        self.root = root
        self.cfg = Config(root)
        self._saved = {k: os.environ.get(k) for k in ("ELAB_HOST", "ELAB_PROFILE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_profile_summary_comes_from_data(self):
        s = srv._profile_summary(self.cfg)
        self.assertIsNotNone(s)
        self.assertEqual(s["id"], "kharon")
        self.assertEqual(s["label"], "zephyr")
        self.assertEqual(s["projects"]["archetype"], ["zephyr"])
        self.assertIn("arch", s["workspaces"])
        self.assertTrue(s["host"].endswith("/kharon.host.yaml"))
        self.assertEqual(s["available"], ["kharon", "solo"])
        self.assertNotIn("modes", s)

    def test_active_profile_priority(self):
        def summary():
            return srv._profile_summary(Config(self.root))

        os.environ.pop("ELAB_PROFILE", None)
        os.environ.pop("ELAB_HOST", None)
        self.assertEqual(summary()["id"], "kharon")
        os.environ["ELAB_PROFILE"] = "solo"
        self.assertEqual(summary()["id"], "solo")
        os.environ["ELAB_PROFILE"] = "ghost"
        self.assertEqual(summary()["id"], "kharon")

    def test_no_profiles_dir_returns_none(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(srv._profile_summary(Config(td)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
