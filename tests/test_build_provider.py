"""test_build_provider —— ``elab build`` 的归属判定回归。

背景（2026-11-18 实测，见执行计划 §0.5）：
    同一个工程两条构建口径 —— ``elab build`` 走 ``plan.py`` 出 cmake 直编
    （还带着空的 ``-DELAB_ARM_GCC_ROOT=`` / ``-DELAB_CPU=``），
    ``elab run --steps build`` 走 provider 出 ``west build``。agent 挑哪个都"看起来对"。

本文件锁住的规则：**工程绑定的 provider 声明了 build 步骤 → build 的归属就是它**，
不再由 ``plan.py`` 猜；没有 provider / 原生链（builtin）时才回落旧路径。

运行：``python -m unittest tests.test_build_provider``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))

from elab import __main__ as main_mod                # noqa: E402
from elab import provider as provider_mod           # noqa: E402
from elab.config import ElabError                   # noqa: E402


def _tc(*, source="provider", name="zephyr/west", steps=("build",)):
    """造一个 ResolvedToolchain 形状的对象（只带被测代码用到的字段）。"""
    return SimpleNamespace(
        provider=SimpleNamespace(source=source, name=name),
        steps={s: object() for s in steps},
    )


def _args(**kw):
    base = dict(dry_run=False, clean=False, jobs=None, json=True, verbose=False)
    base.update(kw)
    return SimpleNamespace(**base)


class BuildRoutingTest(unittest.TestCase):
    # ── 走 provider 的两种情形 ──────────────────────────────────
    def test_provider_project_dry_run_goes_provider(self):
        with mock.patch.object(provider_mod, "resolve", return_value=_tc()), \
                mock.patch.object(main_mod.run_mod, "preview",
                                  return_value={"project": "p", "steps": []}) as pv:
            out = main_mod._provider_build(object(), "p", _args(dry_run=True))
        self.assertIsNotNone(out, "绑了 provider 的工程不该回落到 plan.py")
        self.assertEqual(out["via"], "provider")
        self.assertEqual(out["status"], "dry-run")
        self.assertEqual(out["build_tool"], "zephyr/west")
        pv.assert_called_once()
        # 预览必须只跑 build 这一步（不要把 doctor/flash 带进来）
        self.assertEqual(pv.call_args.kwargs["steps"], ["build"])

    def test_provider_project_real_build_goes_provider(self):
        with mock.patch.object(provider_mod, "resolve", return_value=_tc()), \
                mock.patch.object(main_mod.run_mod, "run",
                                  return_value={"ok": True, "steps": []}) as rn:
            out = main_mod._provider_build(object(), "p", _args())
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(rn.call_args.kwargs["steps"], ["build"])

    def test_failed_provider_build_reports_failed(self):
        with mock.patch.object(provider_mod, "resolve", return_value=_tc()), \
                mock.patch.object(main_mod.run_mod, "run",
                                  return_value={"ok": False, "steps": []}):
            out = main_mod._provider_build(object(), "p", _args())
        self.assertEqual(out["status"], "failed")

    # ── 必须回落旧路径的三种情形 ────────────────────────────────
    def test_native_chain_returns_none(self):
        """没有 provider（builtin）= A/B-class 工程 → 交回 plan.py。"""
        with mock.patch.object(provider_mod, "resolve",
                               return_value=_tc(source="builtin", name="elab/native")):
            self.assertIsNone(main_mod._provider_build(object(), "p", _args()))
            self.assertIsNone(main_mod._provider_build(object(), "p", _args(dry_run=True)))

    def test_provider_without_build_step_returns_none(self):
        with mock.patch.object(provider_mod, "resolve",
                               return_value=_tc(steps=("flash", "debug_verify"))):
            self.assertIsNone(main_mod._provider_build(object(), "p", _args()))

    def test_unresolvable_provider_returns_none(self):
        """provider 未安装/版本不符 → 不在这里报错，回落让旧路径说清原因。"""
        with mock.patch.object(provider_mod, "resolve",
                               side_effect=ElabError("未安装 provider")):
            self.assertIsNone(main_mod._provider_build(object(), "p", _args()))


if __name__ == "__main__":
    unittest.main()
