"""工作区模式切换（classic ⇄ Zephyr）的守卫用例（stdlib unittest，无需 pytest）。

跑：``python tests/test_cockpit_mode.py``

## 守的是什么

顶栏那个 classic ⇄ Zephyr 开关看起来只是"换个列表"，实际动的是**进程级事实**：
``ELAB_HOST`` 决定工具链/探针/Zephyr 工作区，``ELAB_PROFILE`` 决定左栏可见范围与
右列工作区顺序。这里守四条，全都是"切错之后极难看出来"的那种：

1. **模式表来自装配清单本身**：每份 `cockpit/profiles/*.yaml` 就是一行，
   ``label`` / ``desc`` / ``host`` / ``order`` 全由数据给 —— 代码里不认厂商名。
2. **激活顺序**：``ELAB_PROFILE`` → ``host: profile`` → ``web`` → 第一份。
   运行期切换写的是 ELAB_PROFILE，所以它必须压过 host 里那份静态声明。
3. **失败必须回滚**：host 文件缺失时，环境变量要回到切换前的样子。
   半切状态（host 换了 profile 没换）会让左栏工程与右列工作区互相矛盾，
   而界面上没有任何一处能看出是怎么来的。
4. **没声明 host 的模式只换装配**：主机事实源退回**启动时**那份，
   而不是"上一次切到的"那份。
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
from elab.config import Config, ElabError            # noqa: E402


PROFILES = {
    # classic：不写 projects.archetype = 全可见
    "web.yaml": """
profile: web
label: classic
desc: 原生链
host: elab.host.yaml
order: 10
workspaces: [build, flash, serial]
""",
    # Zephyr：只显示 archetype: zephyr 的工程
    "kharon.yaml": """
profile: kharon
label: zephyr
desc: Kharon 链
host: kharon.host.yaml
order: 20
workspaces: [debug, build]
projects:
  archetype: [zephyr]
""",
    # 只换装配、不换 host 的模式（host 缺省）
    "solo.yaml": """
profile: solo
label: 精简
order: 30
workspaces: [build]
""",
    # 声明了一份不存在的主机文件 —— 这个模式必须"可见但不可用"
    "broken.yaml": """
profile: broken
label: 坏装配
host: nope.host.yaml
order: 40
""",
}


class ModeCase(unittest.TestCase):
    """搭一棵最小配置树：两份 host + 四份装配清单。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "cockpit" / "profiles").mkdir(parents=True)
        for name, text in PROFILES.items():
            (root / "cockpit" / "profiles" / name).write_text(text, encoding="utf-8")
        (root / "elab.host.yaml").write_text("version: 1\nhost: {os: auto}\n",
                                             encoding="utf-8")
        (root / "kharon.host.yaml").write_text(
            "version: 1\nprofile: kharon\nhost: {os: windows}\n", encoding="utf-8")
        self.root = root
        self.cfg = Config(root)
        # 环境是全局的：用例之间必须互不影响
        self._saved = {k: os.environ.get(k) for k in ("ELAB_HOST", "ELAB_PROFILE")}
        self._saved_launch = srv._LAUNCH_HOST_ENV

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv._LAUNCH_HOST_ENV = self._saved_launch
        self._tmp.cleanup()

    # ── 模式表 ──────────────────────────────────────────────────
    def test_mode_table_comes_from_data(self):
        s = srv._profile_summary(self.cfg)
        self.assertIsNotNone(s)
        ids = [m["id"] for m in s["modes"]]
        # order 优先：web(10) → kharon(20) → solo(30) → broken(40)
        self.assertEqual(ids, ["web", "kharon", "solo", "broken"])
        by = {m["id"]: m for m in s["modes"]}
        self.assertEqual(by["web"]["label"], "classic")
        self.assertEqual(by["kharon"]["label"], "zephyr")
        self.assertEqual(by["web"]["archetype"], [])
        self.assertEqual(by["kharon"]["archetype"], ["zephyr"])
        # 相对路径按 ELAB_ROOT 解析成绝对路径
        self.assertTrue(by["web"]["host"].endswith("/elab.host.yaml"))
        self.assertTrue(by["web"]["host_exists"])
        # 声明了不存在的主机文件 → 可见但不可用（按钮该置灰，而不是切过去才报错）
        self.assertFalse(by["broken"]["host_exists"])
        # 没声明 host 的模式：host 为空 = 只换装配
        self.assertEqual(by["solo"]["host"], "")

    def test_active_profile_priority(self):
        # ★ 每条都要**新建 Config**：`Config.host` 是惰性缓存的（一份配置对应一份
        #   host 事实源），复用同一个实例就测不出"换了 ELAB_HOST 之后听谁的"。
        #   服务端切换时正是 `switch_mode` 内部新建 Config —— 与这里同构。
        def summary():
            return srv._profile_summary(Config(self.root))

        # ① 默认：没有 ELAB_PROFILE、host 也没写 profile → web
        os.environ.pop("ELAB_PROFILE", None)
        self.assertEqual(summary()["id"], "web")
        # ② host 里写了 profile → 听 host 的
        os.environ["ELAB_HOST"] = str(self.root / "kharon.host.yaml")
        self.assertEqual(summary()["id"], "kharon")
        # ③ 运行期切换写的 ELAB_PROFILE 必须压过 host 的静态声明
        os.environ["ELAB_PROFILE"] = "solo"
        self.assertEqual(summary()["id"], "solo")
        # ④ 指向不存在的名字 → 回落到 host 声明（不是崩，也不是空界面）
        os.environ["ELAB_PROFILE"] = "ghost"
        self.assertEqual(summary()["id"], "kharon")

    def test_no_profiles_dir_returns_none(self):
        """一份清单都没有 = 前端回退内置集合（不能因此起不来）。"""
        with TemporaryDirectory() as td:
            self.assertIsNone(srv._profile_summary(Config(td)))

    # ── 切换 ────────────────────────────────────────────────────
    def test_switch_swaps_host_and_profile(self):
        os.environ.pop("ELAB_PROFILE", None)
        os.environ["ELAB_HOST"] = str(self.root / "elab.host.yaml")
        new_cfg, summary = srv.switch_mode(self.cfg, "kharon")
        self.assertEqual(summary["id"], "kharon")
        self.assertEqual(summary["projects"]["archetype"], ["zephyr"])
        # 主机事实源跟着换（不是只换界面）
        self.assertEqual(Path(new_cfg.host_path).name, "kharon.host.yaml")
        self.assertEqual(Path(os.environ["ELAB_HOST"]).name, "kharon.host.yaml")
        self.assertEqual(os.environ["ELAB_PROFILE"], "kharon")
        # 切回来
        back_cfg, summary2 = srv.switch_mode(new_cfg, "web")
        self.assertEqual(summary2["id"], "web")
        self.assertEqual(summary2["projects"]["archetype"], [])
        self.assertEqual(Path(back_cfg.host_path).name, "elab.host.yaml")

    def test_switch_unknown_mode_is_rejected(self):
        os.environ["ELAB_PROFILE"] = "web"
        with self.assertRaises(ElabError) as cm:
            srv.switch_mode(self.cfg, "nope")
        self.assertIn("未知模式", str(cm.exception))
        # 环境一动不动
        self.assertEqual(os.environ["ELAB_PROFILE"], "web")

    def test_switch_rolls_back_when_host_missing(self):
        """host 文件缺失 → 抛错且环境回到切换前（不许出现半切状态）。"""
        os.environ["ELAB_HOST"] = str(self.root / "elab.host.yaml")
        os.environ["ELAB_PROFILE"] = "web"
        with self.assertRaises(ElabError) as cm:
            srv.switch_mode(self.cfg, "broken")
        self.assertIn("主机文件不存在", str(cm.exception))
        self.assertEqual(Path(os.environ["ELAB_HOST"]).name, "elab.host.yaml")
        self.assertEqual(os.environ["ELAB_PROFILE"], "web")
        # 失败不是"静默成功"：再读一次，生效的仍是原来那份
        self.assertEqual(srv._profile_summary(self.cfg)["id"], "web")

    def test_hostless_mode_falls_back_to_launch_host(self):
        """没写 host 的模式：主机事实源退回**启动时**那份，不是上一次切到的。"""
        srv._LAUNCH_HOST_ENV = str(self.root / "elab.host.yaml")
        os.environ["ELAB_HOST"] = str(self.root / "kharon.host.yaml")
        os.environ["ELAB_PROFILE"] = "kharon"
        new_cfg, summary = srv.switch_mode(self.cfg, "solo")
        self.assertEqual(summary["id"], "solo")
        self.assertEqual(Path(new_cfg.host_path).name, "elab.host.yaml")
        self.assertEqual(os.environ["ELAB_PROFILE"], "solo")

    def test_active_flag_is_exclusive(self):
        srv._LAUNCH_HOST_ENV = ""
        os.environ["ELAB_PROFILE"] = "kharon"
        s = srv._profile_summary(self.cfg)
        actives = [m["id"] for m in s["modes"] if m["active"]]
        self.assertEqual(actives, ["kharon"])
        self.assertEqual(s["id"], "kharon")


if __name__ == "__main__":
    unittest.main(verbosity=2)
