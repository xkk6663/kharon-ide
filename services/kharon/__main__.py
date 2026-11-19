"""kharon 命令行：``deps`` / ``where`` / ``bootstrap`` + 其余转发给 ``elab``。

``deps`` 现在是**只读校验**（2026-11-18，板子 module 化之后）
-----------------------------------------------------------
以前自研板的真相在 west 树里（``west/zephyr/boards/<vendor>/<board>``），仓库只留
归档副本，``kharon deps`` 负责把副本**拷回**树内。代价是"两份存在"，而且
``west update`` 一跑就冲掉 —— 别人 clone 之后永远复现不了。

现在反过来：板子住在仓库里的 ``zephyr_modules/elab-boards/``，作为 **Zephyr module**
经 ``EXTRA_ZEPHYR_MODULES`` 接入（不需要进 west 清单、不需要新建 git 仓）。
于是 ``deps`` 的职责只剩**证明它真的接上了**：

1. ``zephyr/module.yml`` 存在且 ``name`` 正确；
2. ``west boards`` 里能看到归档板（说明 module 的 board_root 生效）；
3. 树内不许再有同名副本（两份同名板会让 ``west boards`` 直接 RuntimeError）。

它不做的事（刻意）：
  * 不 ``west update``（联网、改模块状态，是人的决定）；
  * 不装主机工具（SDK / cmake / ninja 由人装，路径写进 host.yaml）；
  * 不写芯片包（memory / core 那些事实归 dts 与 Kconfig）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

#: module 根的相对位置（与 provider.yaml 的 EXTRA_ZEPHYR_MODULES 同源）
MODULE_REL = Path("zephyr_modules") / "elab-boards"
#: module 自报的名字（写错 name 时 cmake 期才报，不如这里先说）
MODULE_NAME = "elab_boards"
#: 应用公共层 module（与 provider 的 EXTRA_ZEPHYR_MODULES 第二项同源）
APP_MODULE_REL = Path("zephyr_modules") / "elab-app-common"
APP_MODULE_NAME = "elab_app_common"


def _root() -> Path:
    return Path(os.environ.get("ELAB_ROOT") or Path(__file__).resolve().parents[2])


def _host_yaml() -> dict:
    """读主机事实源（P1：``ELAB_HOST=kharon`` → profile + host.local.yaml 合并）。

    只取本命令要用的几处路径。统一走 ``elab.config.Config``，避免 kharon
    自己再实现一遍文件选择 / 变量插值（否则 profile 分层对它不生效）。
    """
    from elab.config import Config, ElabError

    root = _root()
    try:
        cfg = Config(root)
        data = cfg.host
        path = cfg.host_path
    except ElabError as exc:
        raise SystemExit(f"[kharon] {exc}") from exc
    # ★ 主机事实源是**整份文件**：`host:` 只放 os/exec_ext，工具全在平级 `tools:`
    #   （与 Config.host 的语义一致；写成 data["host"]["tools"] 会静默取到空表，
    #   路径全退化成仓库根 —— 实测踩到）。
    tools = data.get("tools") or {}
    z = tools.get("zephyr") or {}
    subst = {"ELAB_ROOT": root.as_posix()}

    def res(v) -> Path:
        s = str(v or "")
        for k, val in subst.items():
            s = s.replace("${" + k + "}", val)
        p = Path(s)
        return p if p.is_absolute() else (root / p)

    return {
        "host_file": path,
        "workspace": res(z.get("workspace")),
        "base": res(z.get("base")),
        "sdk": res(z.get("sdk")),
        "boards_local": res(z.get("boards_local") or (root / "boards")),
    }


def _board_vendor(board_dir: Path) -> str:
    """``board.yml`` 里的 vendor（读不到返回空，不抛错，`deps` 要能用）。"""
    try:
        from elab import _yaml

        data = _yaml.load_file(board_dir / "board.yml") or {}
        vendor = ((data.get("board") or {}).get("vendor") or "").strip()
        return vendor
    except Exception:                              # noqa: BLE001
        return ""


def _module_root() -> Path:
    return _root() / MODULE_REL


def _west_boards(base: Path) -> list[str]:
    """跑一次 ``west boards``（cwd = 工作区），失败返回空列表。"""
    from elab import _winproc

    h = _host_yaml()
    ws = h["workspace"]
    venv = Path(_root()) / ".deps" / "venv" / "Scripts" / "python.exe"
    py = str(venv) if venv.is_file() else sys.executable
    env = dict(os.environ)
    env["ZEPHYR_BASE"] = str(base)
    env["EXTRA_ZEPHYR_MODULES"] = f"{_module_root().as_posix()};{( _root() / APP_MODULE_REL).as_posix()}"
    try:
        proc = subprocess.run([py, "-m", "west", "boards"], cwd=str(ws), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=90,
                              **_winproc.proc_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def cmd_deps(args) -> int:
    """自研板 module 的**只读校验**（不再拷贝任何文件）。"""
    from elab import _yaml

    h = _host_yaml()
    archive = h["boards_local"]
    base = h["base"]
    module_root = _module_root()
    print(f"[kharon] 主机事实源：{h['host_file']}")
    print(f"[kharon] west 工作区：{h['workspace']}")
    print(f"[kharon] ZEPHYR_BASE：{base}")
    print(f"[kharon] SDK        ：{h['sdk']}")
    print(f"[kharon] 板子 module：{module_root}")

    bad = 0
    # ① module.yml 存在且名字正确（写错 name 时 cmake 期才报，贵）
    mod_yml = module_root / "zephyr" / "module.yml"
    if not mod_yml.is_file():
        print(f"[kharon] ✗ 缺少 {mod_yml}（板子 module 的入口）")
        bad += 1
    else:
        try:
            from elab import _yaml
            data = _yaml.load_file(mod_yml) or {}
            name = str(data.get("name") or "").strip()
            root_setting = (((data.get("build") or {}).get("settings") or {})
                            .get("board_root"))
        except Exception as exc:                   # noqa: BLE001
            name, root_setting = "", None
            print(f"[kharon] ✗ {mod_yml} 解析失败：{exc}")
            bad += 1
        if name != MODULE_NAME:
            print(f"[kharon] ✗ module.yml 的 name={name!r}，应为 {MODULE_NAME!r}")
            bad += 1
        if str(root_setting or "") != ".":
            print(f"[kharon] ✗ module.yml 缺 build.settings.board_root: .（现在：{root_setting!r}）")
            bad += 1

    # ①b 应用公共层 module（与板子 module 同属 EXTRA_ZEPHYR_MODULES）
    app_mod = _root() / APP_MODULE_REL
    app_yml = app_mod / "zephyr" / "module.yml"
    if not app_yml.is_file():
        print(f"[kharon] ✗ 缺少 {app_yml}（应用公共层 module 的入口）")
        bad += 1
    else:
        try:
            data = _yaml.load_file(app_yml) or {}
            app_name = str(data.get("name") or "").strip()
        except Exception as exc:                   # noqa: BLE001
            app_name = ""
            print(f"[kharon] ✗ {app_yml} 解析失败：{exc}")
            bad += 1
        if app_name and app_name != APP_MODULE_NAME:
            print(f"[kharon] ✗ module.yml 的 name={app_name!r}，应为 {APP_MODULE_NAME!r}")
            bad += 1
        print(f"[kharon] ✓ 应用公共层 module：{app_mod}")

    # ② 归档板存在，且 west boards 能看到（说明 board_root 真的生效）
    if not archive.is_dir():
        print(f"[kharon] ✗ 板目录不存在：{archive}")
        return 2
    boards = sorted(p for p in archive.glob("*/*") if (p / "board.yml").is_file())
    if not boards:
        print(f"[kharon] （{archive} 下没有 board.yml）")
    listed: list[str] = []
    if base.is_dir():
        listed = _west_boards(base)
    names = set()
    for b in listed:
        names.add(b.split("/")[0])
    for src in boards:
        name = src.name
        if not listed:
            print(f"[kharon] ! {name}：拿不到 west boards（工作区/SDK 未就绪？）")
            bad += 1
        elif name in names:
            print(f"[kharon] ✓ {name}：module 已接入（west boards 可见）")
        else:
            print(f"[kharon] ✗ {name}：west boards 看不到 —— 检查 EXTRA_ZEPHYR_MODULES")
            bad += 1

    # ③ 树内不许再有同名副本（两份同名板会让 west boards 直接 RuntimeError）
    for src in boards:
        vendor = _board_vendor(src) or "espressif"
        stale = base / "boards" / vendor / src.name
        if stale.exists():
            print(f"[kharon] ✗ 树内残留旧副本：{stale}（module 化后它必须删掉，"
                  f"否则 `west boards` 报 defined multiple times）")
            bad += 1

    if bad:
        print(f"[kharon] ✗ {bad} 项未通过")
        return 1
    print("[kharon] ✓ 自研板 module 校验通过（只读，未拷贝任何文件）")
    return 0


def cmd_where(_args) -> int:
    h = _host_yaml()
    for k in ("host_file", "workspace", "base", "sdk", "boards_local"):
        print(f"{k:14s} {h[k]}")
    return 0


def cmd_cockpit(args: list[str]) -> int:
    """起 Kharon 驾驶舱（同内核 + kharon 主机事实源，默认 8334 与 classic 并列）。

    为什么不让 classic 的 8333 复用：两个产品要能**同时开着**（同一个内核服务
    两个 host.yaml → 一个进程只能有一份 cfg）。端口分开是最便宜的做法。
    """
    import subprocess

    port = "8334"
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = args[i + 1]
            i += 2
            continue
        if a.startswith("--port="):
            port = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    root = _root()
    env = dict(os.environ)
    pypath = [str(root / "services"), str(root)]
    if env.get("PYTHONPATH"):
        pypath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pypath)
    env["ELAB_ROOT"] = str(root)
    print(f"[kharon] 驾驶舱 → http://127.0.0.1:{port}/  （按 Ctrl+C 结束）")
    return subprocess.call([sys.executable, "-m", "cockpit.server",
                            "--port", str(port), *rest], cwd=str(root), env=env)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # ★ 主机事实源：只在用户**没显式指定**时兜底，绝不覆盖（CI 里常显式传）。
    #   P1 起用 profile 名而不是文件名：config/host.kharon.profile.yaml + host.local.yaml。
    os.environ.setdefault("ELAB_HOST", "kharon")

    cmd = argv[0] if argv and not argv[0].startswith("-") else ""
    if cmd == "deps":
        # ★ 只在自己的子命令里解析自有参数：`kharon pkg index --check` 里的
        #   --check 属于 elab，不能被这里吞掉（转发时逐字还原）。
        tail = argv[1:]
        if "--force" in tail:
            print("[kharon] --force 已退休：板子 module 化之后 deps 只做校验，"
                  "不再拷贝任何文件", file=sys.stderr)
        ns = argparse.Namespace(check="--check" in tail)
        extra = [a for a in tail if a not in ("--check", "--force")]
        if extra:
            raise SystemExit(f"[kharon] deps 不认识这些参数：{' '.join(extra)}")
        return cmd_deps(ns)
    if cmd == "bootstrap":
        # Zephyr 工作区引导：只补 venv / workspace / 模块，不替人跑 west update。
        from kharon.bootstrap import main as bootstrap_main

        return bootstrap_main(argv[1:])
    if cmd == "where":
        return cmd_where(None)
    if cmd == "env":
        # T9：host yaml + provider defaults → 等效 pwsh 环境脚本
        from elab.envgen import cmd_env

        return cmd_env(argv[1:])
    if cmd == "adapt":
        # T12：识别 Zephyr app（不被 classic adapt 的历史包袱拖住）
        from elab.kharon_probe import cmd_adapt

        return cmd_adapt(argv[1:])
    if cmd == "cockpit":
        return cmd_cockpit(argv[1:])
    if cmd in ("-h", "--help"):
        print(__doc__ or "")
        print("子命令：deps [--check] [--force] / bootstrap [--check|--offline|--write-local] / "
              "where / env [--out P] / adapt APP [--write] / cockpit [--port N]；其余同 `elab --help`")
        return 0

    from elab.__main__ import main as elab_main

    return elab_main(argv)


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
