"""test_wszephyr —— Zephyr 工作区「三形态 + 双拓扑」探测层回归。

运行：``python -m unittest tests.test_wszephyr``

覆盖（``docs/design/执行计划_Zephyr工作区三形态适配_2026-11-18.md`` §3.2 / §3.3 / §6）：
1. ``discover`` 六种假工作区：T1 / T2 / 无工作区 / repo 应用 / workspace 应用 / 独立应用
2. 两个**负例**（§3.2.1⑤）：有 ``zephyr/`` 目录但没有 ``.west`` 不算工作区；
   T2 的清单仓/内核可以不在 topdir 内，仍须判出 ``t2``
3. ``topdir`` 来源优先级：``ELAB_WORKSPACE`` → 向上探测 → host 声明
4. 三形态的 ``app_arg`` / ``cwd`` / ``build_dir``
5. ``${ws:*}`` 模板真的进了 ``_tool_subs``，且三形态 argv 逐字不同
6. ``modules_for`` 的 family 判定（裸 board 名靠 ``board.yml`` 的 socs 反查）
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))

from elab import provider as prov                         # noqa: E402
from elab import run as run_mod                           # noqa: E402
from elab import wszephyr                                 # noqa: E402
from elab.config import Config                            # noqa: E402

VERSION = """\
VERSION_MAJOR = 4
VERSION_MINOR = 5
PATCHLEVEL = 99
VERSION_TWEAK = 0
EXTRAVERSION =
"""

HOST_YAML = """\
version: 1
host:
  os: windows
  exec_ext: .exe
tools:
  west_python: { path: ${ELAB_ROOT}/.deps/venv/Scripts/python.exe }
  zephyr:
    workspace: ${ELAB_ROOT}/ws
    base: ${ELAB_ROOT}/ws/zephyr
    sdk: ${ELAB_ROOT}/sdk
probes:
  list: {}
  default: ""
"""


def _proj(name: str, root: str, work_dir: str) -> str:
    return f"""\
name: {name}
root: {root}
chip: ""
build:
  work_dir: {work_dir}
artifacts:
  elf: ${{work_dir}}/zephyr/zephyr.elf
serial:
  port: COM1
  baud: 115200
"""


class WsBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="elab-test-ws-")
        # resolve() 让临时目录统一成长路径：CI 的 TEMP 是 8.3 短名
        # （C:/Users/RUNNER~1/...），而产品侧返回的是 resolve() 后的长名，
        # 不做这一步在 Windows runner 上会因 RUNNER~1 != runneradmin 全红。
        self.root = Path(self._td.name).resolve()
        self._old_ws = os.environ.pop("ELAB_WORKSPACE", None)
        self._old_base = os.environ.pop("ZEPHYR_BASE", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, val in (("ELAB_WORKSPACE", self._old_ws),
                         ("ZEPHYR_BASE", self._old_base)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._td.cleanup()

    def cfg(self) -> Config:
        return Config(self.root)

    def mkws(self, name: str = "ws", *, manifest_path: str = "zephyr",
             with_zephyr: bool = True, zephyr_rel: str = "zephyr") -> Path:
        """造一个假工作区（只写 ``.west/config`` 与 ``VERSION``，不跑 west）。"""
        ws = self.root / name
        (ws / ".west").mkdir(parents=True)
        (ws / ".west" / "config").write_text(
            f"[manifest]\npath = {manifest_path}\nfile = west.yml\n\n"
            f"[zephyr]\nbase = {zephyr_rel}\n", encoding="utf-8")
        if with_zephyr:
            zb = ws / zephyr_rel
            zb.mkdir(parents=True, exist_ok=True)
            (zb / "VERSION").write_text(VERSION, encoding="utf-8")
        return ws

    def host(self):
        (self.root / "elab.host.yaml").write_text(HOST_YAML, encoding="utf-8")


class TestDiscover(WsBase):
    def test_t1_standalone_app(self):
        """T1 + app 在工作区之外 = 本仓真实形态（standalone）。"""
        self.host()
        self.mkws()
        app = self.root / "apps" / "a"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertTrue(f.present)
        self.assertEqual(f.topology, "t1")
        self.assertEqual(f.manifest_repo, "zephyr")
        self.assertEqual(f.app_role, "standalone")
        self.assertEqual(f.source, "host")           # app 不在工作区内 → host 兜底
        self.assertEqual(f.zephyr_version, "4.5.99")
        self.assertEqual(Path(f.app_arg), app.resolve())
        self.assertEqual(Path(f.build_dir), app.resolve() / "build")
        self.assertEqual(Path(f.cwd), (self.root / "ws").resolve())
        self.assertTrue(f.west_usable and f.cmake_usable)

    def test_repo_app(self):
        self.host()
        ws = self.mkws()
        app = ws / "zephyr" / "samples" / "hello_world"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertEqual(f.app_role, "repo")
        self.assertEqual(f.app_arg, "samples/hello_world")
        self.assertEqual(Path(f.cwd), (ws / "zephyr").resolve())
        # 官方默认会把 build 落进内核仓；我们改落平级 build/<app>
        self.assertEqual(Path(f.build_dir), (ws / "build" / "hello_world").resolve())
        self.assertEqual(f.source, "detect")          # app 在工作区内 → 向上找到 .west

    def test_workspace_app(self):
        self.host()
        ws = self.mkws()
        app = ws / "applications" / "app"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertEqual(f.app_role, "workspace")
        self.assertEqual(f.app_arg, "applications/app")
        self.assertEqual(Path(f.cwd), ws.resolve())
        self.assertEqual(Path(f.build_dir), (ws / "build").resolve())

    def test_t2_topology(self):
        self.host()
        ws = self.mkws(manifest_path="my-manifest")
        (ws / "my-manifest").mkdir(parents=True, exist_ok=True)
        app = ws / "app"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertEqual(f.topology, "t2")
        self.assertEqual(f.manifest_repo, "my-manifest")
        self.assertIn("my-manifest", f.manifest_path)

    def test_t2_manifest_repo_nested_elsewhere(self):
        """负例②：清单仓/内核可以在 topdir 内别的子目录，甚至不叫 zephyr。"""
        self.host()
        ws = self.mkws(manifest_path="manifest", with_zephyr=False)
        (ws / "manifest").mkdir(parents=True, exist_ok=True)
        (ws / "sdk-zephyr").mkdir(parents=True, exist_ok=True)
        (ws / ".west" / "config").write_text(
            "[manifest]\npath = manifest\nfile = west.yml\n\n"
            "[zephyr]\nbase = sdk-zephyr\n", encoding="utf-8")
        (ws / "sdk-zephyr" / "VERSION").write_text(VERSION, encoding="utf-8")
        app = ws / "app"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertEqual(f.topology, "t2")
        self.assertEqual(Path(f.zephyr_base), (ws / "sdk-zephyr").resolve())
        self.assertEqual(f.app_role, "workspace")

    def test_no_workspace(self):
        """没有工作区也不是错误：standalone + 可降级（有 base）或不可用。"""
        self.host()                                   # host 指向的 ws 根本不存在
        app = self.root / "solo"
        app.mkdir()
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertFalse(f.present)
        self.assertEqual(f.topology, "none")
        self.assertEqual(f.app_role, "standalone")
        self.assertFalse(f.west_usable)
        self.assertFalse(f.cmake_usable)
        self.assertEqual(Path(f.build_dir), app.resolve() / "build")

    def test_no_workspace_but_env_base(self):
        self.host()
        zb = self.root / "elsewhere-zephyr"
        zb.mkdir()
        (zb / "VERSION").write_text(VERSION, encoding="utf-8")
        os.environ["ZEPHYR_BASE"] = str(zb)
        app = self.root / "solo"
        app.mkdir()
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertFalse(f.present)
        self.assertTrue(f.cmake_usable)
        self.assertEqual(Path(f.zephyr_base), zb.resolve())

    def test_negative_zephyr_dir_without_west(self):
        """负例①：有 ``zephyr/`` 目录但没有 ``.west`` —— 不是工作区。"""
        self.host()
        fake = self.root / "proj"
        (fake / "zephyr").mkdir(parents=True)
        (fake / "app").mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(fake / "app"), refresh=True)
        self.assertFalse(f.present)
        self.assertEqual(f.topology, "none")

    def test_env_overrides_detection(self):
        """``ELAB_WORKSPACE`` 最高优先级（CI / 多工作区共存用）。"""
        self.host()
        self.mkws("ws")
        other = self.mkws("ws2")
        os.environ["ELAB_WORKSPACE"] = str(other)
        app = self.root / "apps" / "a"
        app.mkdir(parents=True)
        f = wszephyr.discover(self.cfg(), str(app), refresh=True)
        self.assertEqual(f.source, "env")
        self.assertEqual(Path(f.topdir), other.resolve())


class TestTemplates(WsBase):
    """``${ws:*}`` 必须真的进 subs，且三形态 argv 逐字不同。"""

    def _subs(self, proj_name: str) -> dict:
        cfg = self.cfg()
        proj = cfg.resolved_project(proj_name)
        return run_mod._tool_subs(cfg, proj_name, proj, ["${ws:app_arg}"])

    def test_subs_three_forms(self):
        self.host()
        ws = self.mkws()
        (self.root / "projects").mkdir()
        # ① 仓库应用
        (self.root / "projects" / "repo_app.yaml").write_text(
            _proj("repo_app", str(ws / "zephyr" / "samples" / "hello"),
                  str(self.root / "build" / "repo_app")), encoding="utf-8")
        (ws / "zephyr" / "samples" / "hello").mkdir(parents=True, exist_ok=True)
        subs = self._subs("repo_app")
        self.assertEqual(subs["ws:app_arg"], "samples/hello")
        self.assertEqual(Path(subs["ws:cwd"]), (ws / "zephyr").resolve())
        self.assertEqual(subs["ws:app_role"], "repo")
        self.assertEqual(subs["ws:topology"], "t1")

        # ② 工作区应用
        (self.root / "projects" / "ws_app.yaml").write_text(
            _proj("ws_app", str(ws / "applications" / "app"),
                  str(self.root / "build" / "ws_app")), encoding="utf-8")
        (ws / "applications" / "app").mkdir(parents=True, exist_ok=True)
        subs = self._subs("ws_app")
        self.assertEqual(subs["ws:app_arg"], "applications/app")
        self.assertEqual(Path(subs["ws:cwd"]), ws.resolve())

        # ③ 独立应用（本仓形态）：app_arg 就是工程绝对路径
        (self.root / "projects" / "solo.yaml").write_text(
            _proj("solo", str(self.root / "apps" / "solo"),
                  str(self.root / "build" / "solo")), encoding="utf-8")
        (self.root / "apps" / "solo").mkdir(parents=True, exist_ok=True)
        subs = self._subs("solo")
        self.assertEqual(Path(subs["ws:app_arg"]), (self.root / "apps" / "solo").resolve())
        self.assertEqual(subs["ws:app_role"], "standalone")


class TestProviderForms(WsBase):
    """provider 侧：``when_no_workspace`` 校验 + 降级 StepDecl。"""

    def test_when_no_workspace_enum(self):
        ok = {"cmd": ["x"], "when_no_workspace": "native-cmake"}
        self.assertEqual(prov._check_cmd("build", ok), [])
        bad = {"cmd": ["x"], "when_no_workspace": "make"}
        self.assertTrue(any("native-cmake" in p for p in prov._check_cmd("build", bad)))

    def test_post_cmd_validation(self):
        self.assertEqual(prov._check_cmd("build", {"cmd": ["x"], "post_cmd": ["y", "z"]}), [])
        bad = prov._check_cmd("build", {"cmd": ["x"], "post_cmd": [""]})
        self.assertTrue(any("post_cmd" in p for p in bad))

    def test_requires_any_of_validation(self):
        man = {
            "schema_version": 1, "provider_api": 1, "name": "a/b", "version": "1.0.0",
            "license": "Apache-2.0",
            "requires": {"kernel_api": ">=1 <2", "host": [
                {"any_of": [{"key": "k1", "check": "dir"}, {"key": "k2", "check": "dir"}]},
            ]},
            "steps": {"doctor": {"cmd": ["x"]}},
        }
        self.assertEqual(prov.validate_manifest(man), [])
        man["requires"]["host"] = [{"any_of": []}]
        self.assertTrue(any("any_of" in p for p in prov.validate_manifest(man)))

    def test_fallback_decl_has_two_commands(self):
        """无工作区 + 有 ZEPHYR_BASE → 配置 + 构建两条命令（不是一条）。

        ``plan_for`` 在这里被替身顶掉：本用例只验"降级 StepDecl 的形状"，
        不验 cmake 参数怎么算（那是 plan 自己的测试）。
        """
        from types import SimpleNamespace

        self.host()
        zb = self.root / "sdk-zephyr"
        zb.mkdir()
        (zb / "VERSION").write_text(VERSION, encoding="utf-8")
        os.environ["ZEPHYR_BASE"] = str(zb)
        (self.root / "projects").mkdir()
        (self.root / "apps" / "solo").mkdir(parents=True)
        (self.root / "projects" / "solo.yaml").write_text(
            _proj("solo", str(self.root / "apps" / "solo"),
                  str(self.root / "build" / "solo")), encoding="utf-8")
        cfg = self.cfg()
        proj = cfg.resolved_project("solo")
        facts = wszephyr.discover(cfg, str(self.root / "apps" / "solo"), refresh=True)
        self.assertTrue(facts.cmake_usable, "ZEPHYR_BASE 在，应该能降级")
        decl = prov.StepDecl(step="build", kind="external", tool_name="build",
                             cmd=("west", "build"), cwd=".", timeout_s=60,
                             provider="zephyr/west")

        fake_plan = SimpleNamespace(
            source_dir=str(self.root / "apps" / "solo"),
            work_dir=str(self.root / "build" / "solo"),
            configure_cmd=lambda: ["cmake", "-S", "src", "-B", "wd", "-G", "Ninja"],
            build_cmd=lambda: ["cmake", "--build", "wd"],
        )
        from elab import plan as plan_mod
        old = plan_mod.plan_for
        plan_mod.plan_for = lambda *_a, **_k: fake_plan
        try:
            out = prov._cmake_fallback(cfg, proj, "build", decl, facts)
        finally:
            plan_mod.plan_for = old
        self.assertNotEqual(out.cmd, decl.cmd)
        self.assertIn("-S", out.cmd)
        self.assertTrue(out.post_cmd, "降级后必须有第二条 --build 命令")
        self.assertIn("--build", out.post_cmd)


class TestModules(WsBase):
    def test_family_from_vendor(self):
        info = wszephyr.modules_for("espressif/esp32s31", ["hal_espressif"])
        self.assertEqual(info["family"], "espressif")
        self.assertEqual(info["missing"], [])

    def test_family_from_board_yml(self):
        """裸 board 名（``blackpill_f411ce``）靠 board.yml 的 socs 反查。"""
        zb = self.root / "zephyr"
        (zb / "boards" / "weact" / "blackpill_f411ce").mkdir(parents=True)
        (zb / "boards" / "weact" / "blackpill_f411ce" / "board.yml").write_text(
            "board:\n  name: blackpill_f411ce\n  vendor: weact\n  socs:\n"
            "    - name: stm32f411xe\n", encoding="utf-8")
        info = wszephyr.modules_for("blackpill_f411ce", ["hal_stm32"],
                                    zephyr_base=str(zb))
        self.assertEqual(info["family"], "stm32")
        self.assertEqual(info["needed"], ["hal_stm32", "cmsis_6"])
        self.assertEqual(info["missing"], ["cmsis_6"])
        self.assertEqual(info["fix"], ["python -m west update cmsis_6"])

    def test_unknown_family_is_silent(self):
        info = wszephyr.modules_for("some_unknown_board", ["hal_x"])
        self.assertEqual(info["needed"], [])
        self.assertEqual(info["missing"], [])


class TestSysbuild(WsBase):
    """``sysbuild.conf`` 事实 → ``${ws:sysbuild_flag}`` 与裸模板空展开丢弃。

    背景：west 不会因 sysbuild.conf 自动开 sysbuild（只认 ``--sysbuild`` 旗标与
    工作区全局的 build.sysbuild 配置），MCUboot 双镜像工程必须拿到 ``--sysbuild``，
    普通工程则一个多余参数都不能有 —— 同一份 provider 模板，靠事实说话。
    """

    def _mk(self, name: str, *, with_conf: bool) -> Path:
        self.host()
        self.mkws()
        app = self.root / "apps" / name
        app.mkdir(parents=True)
        if with_conf:
            (app / "sysbuild.conf").write_text(
                "SB_CONFIG_MCUBOOT_IMG_MANAGER=y\n", encoding="utf-8")
        (self.root / "projects").mkdir(exist_ok=True)
        (self.root / "projects" / f"{name}.yaml").write_text(
            _proj(name, str(app), str(self.root / "build" / name)),
            encoding="utf-8")
        return app

    def _subs(self, name: str) -> dict:
        cfg = self.cfg()
        proj = cfg.resolved_project(name)
        return run_mod._tool_subs(cfg, name, proj, ["${ws:sysbuild_flag}"])

    def test_flag_with_sysbuild_conf(self):
        self._mk("sb", with_conf=True)
        self.assertEqual(self._subs("sb")["ws:sysbuild_flag"], "--sysbuild")

    def test_flag_without_sysbuild_conf_is_empty(self):
        self._mk("plain", with_conf=False)
        self.assertEqual(self._subs("plain")["ws:sysbuild_flag"], "")

    def _argv(self, name: str) -> list:
        cfg = self.cfg()
        decl = prov.StepDecl(step="build", kind="external",
                             cmd=("echo", "${ws:sysbuild_flag}", "arg"))
        argv, _cwd, _label, _proj, _subs, _post = run_mod._decl_command(
            cfg, name, decl)
        return argv

    def test_bare_template_empty_element_dropped(self):
        self._mk("plain2", with_conf=False)
        argv = self._argv("plain2")
        self.assertEqual(argv, ["echo", "arg"])

    def test_bare_template_flag_kept_when_nonempty(self):
        self._mk("sb2", with_conf=True)
        argv = self._argv("sb2")
        self.assertEqual(argv, ["echo", "--sysbuild", "arg"])


if __name__ == "__main__":
    unittest.main()
