"""elab.westexec —— west 的**受控执行**（agent ``elab_west`` 的内核半）。

与 :mod:`elab.platform.westws` 的分工（一句话）：

- ``westws`` 只读：看工作区（``.west/config`` / 模块 / 板子 / 构建事实）；
- ``westexec`` 执行：跑 ``west`` 的**固定子命令**，写动作（``update`` / ``init``）
  必须由调用方显式携带 ``allow_agent_write`` + ``confirm``。

安全边界（不可放宽）：

1. **永不拼 shell**：``[python, "-m", "west", *args]``，模块名走正则白名单，
   以 ``-`` 开头的参数无法注入。
2. **一律 ``python -m west``**：与 westws 同纪律，绕开 venv console-script 的
   绝对路径陷阱。
3. ``init`` 只做本地 ``-l``（manifest 目录必须已存在），不联网 clone；
   目标目录默认必须落在 elab 仓库根内，越界需 ``ELAB_WEST_ALLOW_OUTSIDE=1``。
4. 超时杀进程**树**（Windows 用 ``taskkill /T``），避免 git 子进程残留。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from elab.core.config import Config, ElabError, to_fwd
from elab.platform._winproc import proc_kwargs

#: 允许的模块/工程名（west update 的参数）。首字符必须是字母数字，杜绝 "-flag"。
_MODULE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

DEFAULT_UPDATE_TIMEOUT_S = 900
DEFAULT_INIT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 1800

#: 返回给 agent 的日志尾部上限（行数 / 单行字符 / 总字符）。
_TAIL_LINES = 240
_TAIL_LINE_CHARS = 400
_TAIL_TOTAL_CHARS = 24000


def _host(cfg: Config, key: str) -> str:
    from elab.workflow.run import _host_value
    try:
        return _host_value(cfg, key)
    except ElabError:
        return ""


def _facts(cfg: Config, app_dir: str = ""):
    from elab.platform import wszephyr
    return wszephyr.discover(cfg, app_dir)


def _python(cfg: Config) -> str:
    """与 westws 共用同一条「能跑 west 的 python」解析链（host → .deps → .venv → PATH）。"""
    from elab.platform import westws
    return westws.west_python(cfg)


def _workspace(cfg: Config, facts) -> str:
    return facts.topdir or _host(cfg, "tools.zephyr.workspace")


def _default_workspace(cfg: Config, facts) -> str:
    """没给 app 时找一个默认工作区：env/host → ``<root>/west`` → ``<root>``。

    只检查**是否真有 `.west/config`**（不按目录名猜拓扑），所以 ``<root>/west``
    只是 bootstrap 的候选位置，命中仍以 west 自己的配置文件为准。
    """
    ws = _workspace(cfg, facts)
    if ws:
        return ws
    for cand in (Path(cfg.root) / "west", Path(cfg.root)):
        if (cand / ".west" / "config").is_file():
            return to_fwd(cand)
    return ""


def _env(cfg: Config, facts) -> dict:
    env = dict(os.environ)
    base = facts.zephyr_base or _host(cfg, "tools.zephyr.base")
    if base:
        env["ZEPHYR_BASE"] = base
    sdk = _host(cfg, "tools.zephyr.sdk")
    if sdk:
        env["ZEPHYR_SDK_INSTALL_DIR"] = sdk
    return env


def _tail(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    lines = [ln[:_TAIL_LINE_CHARS] for ln in lines[-_TAIL_LINES:]]
    out = "\n".join(lines)
    return out[-_TAIL_TOTAL_CHARS:]


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, **proc_kwargs())
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(py: str, args: list[str], *, cwd: str, env: dict,
         timeout_s: int) -> dict:
    """跑一条 west 命令；返回 ``{rc, stdout, stderr, duration_s, timed_out}``。"""
    started = time.time()
    try:
        proc = subprocess.Popen(
            [py, "-m", "west", *args], cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **proc_kwargs(),
        )
    except OSError as exc:
        return {"rc": 127, "stdout": "", "stderr": f"无法启动 west：{exc}",
                "duration_s": 0.0, "timed_out": False}
    try:
        out, err = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        timed_out = True
    return {
        "rc": proc.returncode if proc.returncode is not None else -1,
        "stdout": out or "",
        "stderr": err or "",
        "duration_s": round(time.time() - started, 2),
        "timed_out": timed_out,
    }


def _command_label(args: list[str]) -> str:
    return "python -m west " + " ".join(args)


def status(cfg: Config, *, app_dir: str = "") -> dict:
    """west 可用性 + 工作区事实（只读；会真跑一次 ``west --version``）。"""
    facts = _facts(cfg, app_dir)
    py = _python(cfg)
    ws = _default_workspace(cfg, facts)
    if ws and not facts.topdir:
        # 没给 app 时用默认工作区补齐拓扑/内核版本（否则 topology 会误报 none）。
        facts = _facts(cfg, ws)
    cwd = ws if ws and Path(ws).is_dir() else cfg.root
    payload = {
        "ok": False,
        "west_python": py,
        "version": "",
        "workspace": ws,
        "zephyr_base": facts.zephyr_base or _host(cfg, "tools.zephyr.base"),
        "sdk_path": _host(cfg, "tools.zephyr.sdk"),
        "topology": facts.topology,
        "app_role": facts.app_role,
        "zephyr_version": facts.zephyr_version,
        "cwd": to_fwd(cwd),
        "hint": "",
    }
    if not py:
        from elab.platform import westws
        tried = westws.west_python_candidates(cfg)
        payload["hint"] = (
            "找不到能跑 west 的 python。候选：" + " / ".join(tried)
            + "；安装：``python -m pip install west``，或跑 kharon bootstrap 生成 .deps/venv")
        return payload
    run = _run(py, ["--version"], cwd=cwd, env=_env(cfg, facts), timeout_s=30)
    payload["version"] = (run["stdout"] + run["stderr"]).strip().splitlines()[0] \
        if (run["stdout"] or run["stderr"]).strip() else ""
    payload["ok"] = run["rc"] == 0
    if not payload["ok"]:
        payload["hint"] = (f"`python -m west --version` 失败（rc={run['rc']}）："
                           f"{_tail(run['stderr']) or '无输出'}；先跑 kharon deps 或检查 python")
    elif not ws:
        payload["hint"] = "west 可用，但当前不在 west 工作区（无 .west/config）；可用 action=init 初始化"
    return payload


def read_snapshot(cfg: Config, action: str, *, q: str = "",
                  refresh: bool = False, limit: int = 200) -> dict:
    """``list`` / ``boards``：复用 westws 的只读快照（同一份缓存与口径）。"""
    from elab.platform import westws
    facts = _facts(cfg)
    snap = westws.snapshot(cfg, q=q, refresh=refresh,
                           app_dir=_default_workspace(cfg, facts))
    if action == "list":
        return {
            "ok": snap.get("ok") is True,
            "workspace": snap.get("workspace", ""),
            "topology": snap.get("topology", ""),
            "zephyr_base": snap.get("zephyr_base", ""),
            "module_total": snap.get("module_total", 0),
            "module_present": snap.get("module_present", 0),
            "modules": snap.get("modules", []),
            "reason": snap.get("reason", ""),
            "hint": snap.get("hint", ""),
        }
    shown = max(1, min(2000, int(limit or 200)))
    return {
        "ok": snap.get("ok") is True,
        "workspace": snap.get("workspace", ""),
        "board_total": snap.get("board_total", 0),
        "boards": snap.get("boards", [])[:shown],
        "boards_shown": min(shown, len(snap.get("boards", []))),
        "query": q,
        "hint": snap.get("hint", ""),
    }


def _normalize_modules(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = [x.strip() for x in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise ElabError("modules 必须是逗号分隔字符串或字符串数组")
    out: list[str] = []
    for name in items:
        if not name:
            continue
        if not _MODULE_RE.match(name):
            raise ElabError(f"非法的 west 模块名：{name!r}（只允许字母数字 _ . -）")
        if name not in out:
            out.append(name)
    if len(out) > 64:
        raise ElabError("一次最多更新 64 个模块")
    return out


def update(cfg: Config, *, modules=None, timeout_s: int = 0) -> dict:
    """``west update [modules...]``（写：只碰工作区，不碰硬件）。"""
    from elab.platform import westws
    facts = _facts(cfg)
    py = _python(cfg)
    ws = _default_workspace(cfg, facts)
    if not py:
        raise ElabError("找不到能跑 west 的 python（候选：host tools.west_python.path / "
                        ".deps/venv / .venv / PATH；安装：python -m pip install west）")
    if not ws:
        raise ElabError("当前不在 west 工作区（无 .west/config）；先 action=init 或 kharon bootstrap")
    if not (Path(ws) / ".west" / "config").is_file():
        raise ElabError(f"{to_fwd(ws)} 下没有 .west/config，不是已初始化的工作区")
    names = _normalize_modules(modules)
    timeout = max(30, min(MAX_TIMEOUT_S, int(timeout_s or DEFAULT_UPDATE_TIMEOUT_S)))
    args = ["update", *names]
    run = _run(py, args, cwd=ws, env=_env(cfg, facts), timeout_s=timeout)
    westws.invalidate(ws)
    return {
        "ok": run["rc"] == 0 and not run["timed_out"],
        "action": "update",
        "cwd": to_fwd(ws),
        "modules": names,
        "command": _command_label(args),
        "rc": run["rc"],
        "duration_s": run["duration_s"],
        "timed_out": run["timed_out"],
        "stdout_tail": _tail(run["stdout"]),
        "stderr_tail": _tail(run["stderr"]),
        "hint": ("超时已终止（west 进程树）" if run["timed_out"] else
                 ("检查网络 / west manifest" if run["rc"] != 0 else "")),
    }


def _allowed_root(cfg: Config) -> tuple[Path | None, str]:
    if os.environ.get("ELAB_WEST_ALLOW_OUTSIDE", "").strip() in ("1", "true", "yes"):
        return None, "ELAB_WEST_ALLOW_OUTSIDE=1（允许仓库外）"
    return Path(cfg.root).resolve(), "默认限制在 elab 仓库根内"


def init(cfg: Config, *, directory: str = "", manifest: str = "zephyr",
         timeout_s: int = 0) -> dict:
    """``west init -l <manifest>``（本地初始化；manifest 目录必须已存在）。"""
    from elab.platform import westws
    py = _python(cfg)
    if not py:
        raise ElabError("找不到能跑 west 的 python（候选：host tools.west_python.path / "
                        ".deps/venv / .venv / PATH；安装：python -m pip install west）")
    target_raw = (directory or "").strip()
    if not target_raw:
        raise ElabError("action=init 需要 dir（要初始化成 west 工作区的目录绝对路径）")
    if not os.path.isabs(target_raw):
        raise ElabError("dir 必须是绝对路径")
    target = Path(target_raw).resolve()
    root, why = _allowed_root(cfg)
    if root is not None:
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ElabError(
                f"dir 越界：{to_fwd(target)} 不在仓库根 {to_fwd(root)} 内（{why}）；"
                f"确需仓库外请显式设置 ELAB_WEST_ALLOW_OUTSIDE=1") from exc

    manifest_rel = (manifest or "zephyr").strip().replace("\\", "/")
    if (manifest_rel.startswith("/") or ":" in manifest_rel
            or ".." in manifest_rel.split("/")):
        raise ElabError("manifest 必须是工作区内的相对目录（如 zephyr）")
    manifest_dir = (target / manifest_rel).resolve()
    if not manifest_dir.is_dir():
        raise ElabError(f"manifest 目录不存在：{to_fwd(manifest_dir)}"
                        f"（本方案只做本地 init，不联网 clone）")

    if (target / ".west" / "config").is_file():
        return {"ok": True, "action": "init", "already": True,
                "cwd": to_fwd(target),
                "workspace": to_fwd(target),
                "hint": "该目录已经是 west 工作区，无需重复 init"}

    target.mkdir(parents=True, exist_ok=True)
    timeout = max(10, min(MAX_TIMEOUT_S, int(timeout_s or DEFAULT_INIT_TIMEOUT_S)))
    facts = _facts(cfg)
    args = ["init", "-l", to_fwd(manifest_dir)]
    run = _run(py, args, cwd=str(target), env=_env(cfg, facts), timeout_s=timeout)
    westws.invalidate()
    return {
        "ok": run["rc"] == 0 and not run["timed_out"],
        "action": "init",
        "already": False,
        "cwd": to_fwd(target),
        "workspace": to_fwd(target),
        "manifest": to_fwd(manifest_dir),
        "command": _command_label(args),
        "rc": run["rc"],
        "duration_s": run["duration_s"],
        "timed_out": run["timed_out"],
        "stdout_tail": _tail(run["stdout"]),
        "stderr_tail": _tail(run["stderr"]),
        "hint": "" if run["rc"] == 0 else "manifest 目录需是 zephyr 源码树（含 west.yml）",
    }


__all__ = ["status", "read_snapshot", "update", "init"]
