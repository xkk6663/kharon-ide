"""elab.westws —— west 工作区的**只读**快照（模块树 / board 清单 / SDK / 构建事实）。

为什么需要它
------------
``west`` 本身完全不给视图：``west list`` 只打印 70 行文本，没有一个地方告诉你
**"声明了 70 个模块，实际只拉取了 7 个，另外 63 个不存在但照样能构建"**。
换一块板才发现缺 ``hal_xtensa`` —— 这不是理论问题，是这个仓库的真实状态。

三条硬要求（都是实测踩出来的）
------------------------------
1. **一律 ``python -m west``**，永不 ``west.exe``：venv 的 console-script 内嵌了
   创建时的绝对路径，目录一搬就静默失效（exit 1、零输出）。
2. **CRLF 必须 strip**：west 在 Windows 上输出 ``\r\n``；不 strip，``[ -e "$p" ]``
   永远为假，模块"存在数"会算成 0。
3. **1206 个 board 不全量下发**：默认只给"本仓用到的 + 前 200 条"，其余按 ``q`` 过滤。

这个模块**只读**：不跑 ``west update``、不写工作区。依赖归 ``kharon deps``。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from elab.core.config import Config, ElabError
from elab.platform._winproc import proc_kwargs

#: 进程内缓存：工作区路径 → (时间戳, 快照)。board 清单要跑 ~1.5s，不值得每次轮询都重来。
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_S = 30.0

#: 默认下发的 board 条数（全量 1206 条灌进 UI = 页面卡死）
BOARD_LIMIT = 200


def _host(cfg: Config, key: str) -> str:
    from elab.workflow.run import _host_value
    try:
        return _host_value(cfg, key)
    except ElabError:
        return ""


def _facts(cfg: Config, app_dir: str = "", *, refresh: bool = False):
    """工作区事实（``wszephyr.discover`` 的薄包装）。

    ★ 本模块**不再自己判断**"工作区在哪"：那是 ``wszephyr`` 的唯一职责
      （``.west/config`` 是唯一契约，host 只是兜底之一）。这里只消费 Facts。
    """
    from elab.platform import wszephyr
    return wszephyr.discover(cfg, app_dir, refresh=refresh)


#: 进程内缓存：python 路径（探测一次就够；西部工具链很少中途换）。
_PY_CACHE: dict[str, str] = {}


def west_python_candidates(cfg: Config) -> list[str]:
    """west 解释器候选（按优先级；最后才是 PATH 上的 python）。

    顺序：host 显式配置 → ``.deps/venv``（bootstrap 的现役约定）→ ``.venv``
    （旧约定）→ PATH ``python``。**不下载、不安装**，只挑一个能 ``import west`` 的。
    """
    root = Path(cfg.root)
    exe = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    out: list[str] = []
    explicit = _host(cfg, "tools.west_python.path")
    if explicit:
        out.append(explicit)
    out.append(str(root / ".deps" / "venv" / exe))
    out.append(str(root / ".venv" / exe))
    out.append("python")
    # 去重且保序
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def west_python(cfg: Config) -> str:
    """挑一个**真能跑 west** 的 python（``-m west --version`` 验证后缓存）。

    为什么要验证：``.deps/venv`` 存在但 west 没装、或 host 里写了个失效路径时，
    直接返回候选会让后续每条命令都失败得莫名其妙。探测从 ``cfg.root`` 跑，
    避免工作区目录名恰好叫 ``west`` 时把它当成 namespace package（实测踩过）。
    """
    key = str(cfg.root)
    cached = _PY_CACHE.get(key)
    if cached is not None:
        return cached
    for cand in west_python_candidates(cfg):
        if cand != "python" and not Path(cand).is_file():
            continue
        try:
            proc = subprocess.run(
                [cand, "-m", "west", "--version"], cwd=str(cfg.root),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=25, **proc_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            _PY_CACHE[key] = cand
            return cand
    _PY_CACHE[key] = ""
    return ""


def _west(cfg: Config, args: list[str], *, workspace: str = "",
          timeout: int = 40) -> tuple[int, str]:
    """跑一条只读 west 命令（cwd = 显式 workspace 或探测到的 topdir）。

    ★ ``workspace`` 必须由调用方传：``snapshot`` 是按**工程**探测 topdir 的，
    若这里再 ``_facts(cfg)`` 一次（无 app）会得到"无工作区"→ 127，
    表现为「有 workspace 却列不出模块」（实测踩过）。
    """
    ws = workspace
    facts = _facts(cfg, ws) if ws else _facts(cfg)
    if not ws:
        ws = facts.topdir or _host(cfg, "tools.zephyr.workspace")
    py = west_python(cfg)
    if not ws or not py:
        return 127, ""
    env = dict(os.environ)
    base = facts.zephyr_base or _host(cfg, "tools.zephyr.base")
    if base:
        env["ZEPHYR_BASE"] = base
    sdk = _host(cfg, "tools.zephyr.sdk")
    if sdk:
        env["ZEPHYR_SDK_INSTALL_DIR"] = sdk
    try:
        proc = subprocess.run(
            [py, "-m", "west", *args], cwd=ws, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, **proc_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout or ""


def _modules(cfg: Config, workspace: str = "") -> list[dict]:
    """``west list`` → 模块清单（含"本地存不存在"这一列，west 自己不给）。"""
    ws = workspace or _facts(cfg).topdir or _host(cfg, "tools.zephyr.workspace")
    rc, out = _west(cfg, ["list", "-f", "{name}|{path}|{revision}"], workspace=ws)
    if rc != 0 or not ws:
        return []
    items: list[dict] = []
    for raw in out.splitlines():
        line = raw.rstrip("\r\n")            # ★ CRLF：不 strip 会把存在数算成 0
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, path, rev = parts[0], parts[1], parts[2]
        # ★ west 输出的是**相对工作区**的路径（zephyr / modules/hal/stm32）；
        #   直接 Path(path).exists() 会按进程 cwd 判断，得到"全都没拉取"。
        p = Path(path)
        full = p if p.is_absolute() else Path(ws) / p
        items.append({
            "name": name,
            "path": path,
            "revision": rev,
            "present": full.exists(),
        })
    return items


def _boards(cfg: Config, workspace: str = "") -> list[str]:
    rc, out = _west(cfg, ["boards"], workspace=workspace, timeout=60)
    if rc != 0:
        return []
    # west 1.5.0 不支持 `boards -f`（直接 traceback），只能按行取
    return [ln.rstrip("\r\n") for ln in out.splitlines() if ln.strip()]


def _sdk_targets(cfg: Config) -> list[dict]:
    sdk = _host(cfg, "tools.zephyr.sdk")
    if not sdk:
        return []
    root = Path(sdk) / "gnu"
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir():
            gdb = next(iter(sorted((p / "bin").glob("*-gdb.exe"))), None) \
                if (p / "bin").is_dir() else None
            out.append({"target": p.name, "gdb": gdb.name if gdb else ""})
    return out


def _local_targets(cfg: Config) -> list[str]:
    """本仓 projects 实际用到的 board（target 的第一段，去重）。"""
    from elab.platform import provider as provider_mod
    out: list[str] = []
    try:
        cards = cfg.projects
    except ElabError:
        return out
    for name in cards:
        try:
            proj = cfg.resolved_project(name)
            chip = cfg.chip_of(proj)
        except ElabError:
            continue
        target = str(chip.get("target") or chip.get("id") or "")
        board = target.split("/")[0] if target else ""
        if board and board not in out:
            out.append(board)
    return out


def _builds(cfg: Config) -> list[dict]:
    """最近构建事实：从每个工程 work_dir 的 ``build_info.yml`` 读（不跑构建）。"""
    from elab.platform import zephyr_meta
    out: list[dict] = []
    for name in sorted(cfg.projects):
        try:
            proj = cfg.resolved_project(name)
        except ElabError:
            continue
        work_dir = str((proj.get("build") or {}).get("work_dir") or "")
        if not work_dir:
            continue
        info = zephyr_meta.build_info(work_dir)
        if not info:
            continue
        cmake = info.get("cmake") or {}
        board = (cmake.get("board") or {})
        dts = ((cmake.get("devicetree") or {}).get("files") or [])
        conf = ((cmake.get("kconfig") or {}).get("files") or [])
        west = info.get("west") or {}
        out.append({
            "project": name,
            "board": board.get("name", ""),
            "board_path": (board.get("path") or [""])[0] if board.get("path") else "",
            "qualifiers": board.get("qualifiers", ""),
            "dts_files": dts,
            "kconfig_files": conf,
            "west_command": west.get("command", ""),
            "west_version": west.get("version", ""),
            "zephyr_version": ((cmake.get("zephyr") or {}).get("version", "")),
        })
    return out


def invalidate(workspace: str = "") -> None:
    """丢弃快照缓存（写动作之后必须调用，否则模块清单还是旧的）。

    ``workspace`` 为空 = 全清（``init`` 会改变工作区位置）。
    """
    if workspace:
        _CACHE.pop(workspace, None)
    else:
        _CACHE.clear()


def snapshot(cfg: Config, *, q: str = "", refresh: bool = False,
             app_dir: str = "") -> dict:
    """一次读全；30s 进程内缓存（``refresh=true`` 强制重读）。

    ``topology`` / ``manifest_*`` / ``app_role`` / ``boards_by_source`` 是
    「三形态 + 双拓扑」改造新增的字段（2026-11-18）——**旧键一个不少**，
    前端与 agent 的既有读取路径零改动。
    """
    from elab.platform import wszephyr

    facts = _facts(cfg, app_dir, refresh=refresh)
    ws = facts.topdir
    sdk_path = _host(cfg, "tools.zephyr.sdk")
    base_common = {
        "workspace": ws,
        "zephyr_base": facts.zephyr_base or _host(cfg, "tools.zephyr.base"),
        "sdk_path": sdk_path,
        "topology": facts.topology,
        "manifest_path": facts.manifest_path,
        "manifest_repo": facts.manifest_repo,
        "app_role": facts.app_role,
        "app_dir": facts.app_dir,
        "zephyr_version": facts.zephyr_version,
        "workspace_source": facts.source,
        "facts": facts.to_dict(),
    }
    if not ws:
        # 无工作区不是错误：三种应用类型里的 standalone 本来就可以没有 west。
        # ``ok`` 仍为 False（= "列不出模块"），但事实齐备，前端/agent 可照常解释。
        payload = dict(base_common)
        payload.update({
            "ok": False,
            "reason": ("这个工程不在任何 west 工作区内（无 .west/config），"
                       "也无 ZEPHYR_BASE —— 只能装工具链后再编"),
            "hint": "kharon bootstrap（或设置 ELAB_WORKSPACE / ZEPHYR_BASE）",
            "modules": [], "module_total": 0, "module_present": 0,
            "boards": [], "board_total": 0,
            "boards_by_source": {"pack": [], "west": [], "repo": [],
                                 "all": []},
            "sdk": _sdk_targets(cfg), "builds": _builds(cfg),
            "local_targets": _local_targets(cfg),
        })
        return _shape(payload, q)

    now = time.time()
    hit = _CACHE.get(ws)
    if hit and not refresh and now - hit[0] < CACHE_TTL_S:
        payload = hit[1]
    else:
        modules = _modules(cfg, ws)
        boards = _boards(cfg, ws)
        payload = dict(base_common)
        payload.update({
            "ok": bool(modules),
            "modules": modules,
            "module_total": len(modules),
            "module_present": sum(1 for m in modules if m.get("present")),
            "boards": boards,
            "board_total": len(boards),
            "boards_by_source": wszephyr.boards_by_source(
                cfg, facts, west_boards=boards),
            "sdk": _sdk_targets(cfg),
            "builds": _builds(cfg),
            "local_targets": _local_targets(cfg),
        })
        _CACHE[ws] = (now, payload)
    return _shape(payload, q)


def _shape(payload: dict, q: str) -> dict:
    """搜索/限额在缓存之后做（同一份数据，不同视图）。"""
    ql = (q or "").strip().lower()
    boards = payload["boards"]
    if ql:
        shown = [b for b in boards if ql in b.lower()][:BOARD_LIMIT]
    else:
        local = [b for b in boards if b.split("/")[0] in set(payload["local_targets"])]
        rest = [b for b in boards if b not in local][: max(0, BOARD_LIMIT - len(local))]
        shown = local + rest
    out = dict(payload)
    out.pop("boards", None)
    out["boards"] = shown
    out["boards_shown"] = len(shown)
    out["query"] = q
    return out
