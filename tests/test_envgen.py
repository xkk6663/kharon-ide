"""``kharon env``（T9）单测：脚本与 host/provider 事实源逐项一致。

    python -m unittest tests.test_envgen

- 变量集合 == provider ``defaults.env`` 的键（跳过项单独列出）；
- 展开后不残留 ``${...}``，路径全是正斜杠；
- ``west`` 所在目录必须出现在 PATH 前置列表里（T9 验收）；
- 渲染出的 pwsh 含自检段（``Get-Command west``）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from elab import _yaml                                          # noqa: E402
from elab.config import Config                                  # noqa: E402
from elab.platform import envgen                                # noqa: E402

PROVIDER = ROOT / "providers" / "official" / "zephyr" / "west" / "provider.yaml"


class EnvGenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old = os.environ.get("ELAB_HOST")
        os.environ["ELAB_HOST"] = "kharon"
        cls.cfg = Config(ROOT)
        cls.info = envgen.collect(cls.cfg)

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("ELAB_HOST", None)
        else:
            os.environ["ELAB_HOST"] = cls._old

    def test_matches_provider_defaults(self):
        defaults = (_yaml.load_file(PROVIDER) or {}).get("defaults") or {}
        want = set((defaults.get("env") or {}).keys())
        got = set(self.info["env"].keys())
        skipped_env = {k.split("env.", 1)[1] for k in self.info["skipped"]
                       if k.startswith("env.")}
        self.assertEqual(got | skipped_env, want)
        self.assertEqual(self.info["provider"], "zephyr/west")

        # PATH 前置条数：provider 声明的数量减去未配置的
        n_path = len(defaults.get("env_prepend_path") or [])
        skipped_path = sum(1 for k in self.info["skipped"]
                           if k.startswith("env_prepend_path"))
        self.assertEqual(len(self.info["prepend"]), n_path - skipped_path)

    def test_all_values_are_expanded_and_posix(self):
        for k, v in self.info["env"].items():
            self.assertNotIn("${", v, k)
            self.assertNotIn("\\", v, k)
            self.assertTrue(Path(v).is_absolute(), f"{k}={v}")
        for p in self.info["prepend"]:
            self.assertNotIn("${", p)
            self.assertNotIn("\\", p)
            self.assertTrue(Path(p).is_absolute(), p)

    def test_west_dir_is_prepended(self):
        z = (self.cfg.host.get("tools") or {}).get("zephyr") or {}
        venv = str(z.get("venv_scripts") or "")
        self.assertTrue(venv)
        venv = venv.replace("\\", "/").replace("${ELAB_ROOT}", str(ROOT).replace("\\", "/"))
        self.assertIn(venv, self.info["prepend"])

    def test_render_pwsh(self):
        text = envgen.render_pwsh(self.info)
        self.assertIn("$env:ZEPHYR_BASE = '", text)
        self.assertIn("$env:PATH = (($__kharon_prepend -join ';')", text)
        self.assertIn("Get-Command west", text)
        self.assertIn("kharon-env.ps1", text)
        self.assertIn("# provider：zephyr/west", text)

    def test_render_marks_skipped(self):
        info = dict(self.info)
        info["skipped"] = ["env.ZEPHYR_SDK_INSTALL_DIR"]
        info["env"] = {k: v for k, v in info["env"].items()
                       if k != "ZEPHYR_SDK_INSTALL_DIR"}
        text = envgen.render_pwsh(info)
        self.assertIn("⚠ 未配置，已跳过：env.ZEPHYR_SDK_INSTALL_DIR", text)
        self.assertNotIn("$env:ZEPHYR_SDK_INSTALL_DIR", text)

    def test_ps_quote_escape(self):
        self.assertEqual(envgen._ps("a'b"), "'a''b'")


if __name__ == "__main__":
    unittest.main()
