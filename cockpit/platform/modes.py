"""cockpit.platform.modes —— host/profile 模式切换（进程级环境）。
"""

from __future__ import annotations

import mimetypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parents[1]
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "services") not in sys.path:
    sys.path.insert(0, str(_ROOT / "services"))

from elab import adapt as adapt_mod                      # noqa: E402
from elab import project_admin as project_admin_mod      # noqa: E402
from elab import flash as flash_mod                      # noqa: E402
from elab import debugsession as dbgsess_mod           # noqa: E402
from elab.plan import plan_for                          # noqa: E402
from elab.flow import KERNEL_PLANS, STEP_META           # noqa: E402  流程清单（唯一事实源）
from elab import run as run_mod                      # noqa: E402
from elab._winproc import adopt as adopt_proc        # noqa: E402
from elab import provider as provider_mod            # noqa: E402
from elab import zephyr_meta as zephyr_meta_mod      # noqa: E402
from elab import westws as westws_mod                # noqa: E402
from elab import serialcodec as serialcodec_mod      # noqa: E402
from elab import serialconsole as serialconsole_mod  # noqa: E402
from elab import serialcontrol as serialcontrol_mod  # noqa: E402
from elab import logdecode as ld                      # noqa: E402
from elab import serialframes as sf                   # noqa: E402
from elab import framelearn as framelearn_mod         # noqa: E402
from elab import serialmon as serialmon_mod          # noqa: E402
from elab import serialport as serialport_mod        # noqa: E402
from elab import serialtransport as serialtransport_mod  # noqa: E402
from elab import serialterm as serialterm_mod        # noqa: E402
from elab import serialwidgets as serialwidgets_mod  # noqa: E402
from elab import modbus as modbus_mod                # noqa: E402
from elab import modbusmaster as modbusmaster_mod    # noqa: E402
from elab.config import Config, ElabError, Host, to_fwd     # noqa: E402
from elab.kernel import events as ev_mod              # noqa: E402
from elab import pkg as pkg_mod                       # noqa: E402
from elab import setup as setup_mod                   # noqa: E402
from elab import _yaml as yaml_mod                    # noqa: E402


from cockpit.core import *  # noqa: F401,F403


#: 进程启动时的 ``ELAB_HOST``（切换模式时"没声明 host 的模式"要能回到它）。
#: ★ 只作**回落基线**记一次：运行期改的是 ``os.environ``（给派生进程看的），
#:   基线本身不动 —— 否则切到 zephyr 再切回来就找不回"我是怎么启动的"。
_LAUNCH_HOST_ENV = os.environ.get("ELAB_HOST", "")

#: 模式切换的互斥锁：它改的是**进程级**环境（ELAB_HOST / ELAB_PROFILE），
#: 两个请求同时进来会切出"host 是 A、profile 是 B"的杂交态。
_MODE_LOCK = threading.Lock()


def _profiles_dir(cfg: Config) -> Path:
    """装配清单目录（``<ELAB_ROOT>/cockpit/profiles``）。"""
    return Path(cfg.root) / "cockpit" / "profiles"


def _profile_files(cfg: Config) -> list[Path]:
    """全部装配清单，按文件名排序（排序 = 模式表的呈现顺序，稳定可预期）。"""
    d = _profiles_dir(cfg)
    return sorted(d.glob("*.yaml")) if d.is_dir() else []


def _read_profile(path: Path) -> dict:
    """读一份装配清单；**坏文件返回空 dict 而不是抛**。

    一份写坏的 yaml 只该让它自己那个按钮不可用，不该把整张模式表打挂 ——
    模式表是"选哪个工作区"的唯一入口，它挂了就一个都切不了。
    """
    try:
        return yaml_mod.load_file(path) or {}
    except Exception:                                 # noqa: BLE001
        return {}


def _active_profile_name(cfg: Config, files: list[Path]) -> str:
    """当前生效的装配清单（文件名 stem）。

    优先级：``ELAB_PROFILE``（运行期切换写入）→ ``<host>: profile`` →
    ``web`` → 排序第一份。

    ★ 运行期切换**只活在进程内**（改环境变量），重启后仍由 host 文件说了算：
      ``kharon.cmd`` 与 ``cockpit.cmd`` 各自的默认不该被上一次点击改掉 ——
      否则"我明明用 kharon 启动的，怎么是 classic"这类幽灵故障迟早出现。
    """
    if not files:
        return ""
    # ★ 三个来源要**逐个试**，不能先合成一个 wanted 再校验：合成之后
    #   "ELAB_PROFILE 指向一个已被删掉的名字"会直接跳到 web，把 host 里
    #   那份显式声明白扔掉 —— 正好落在"我刚删了清单，怎么整个产品变回 classic"。
    def _pick(name: str) -> str:
        return name if any(p.stem == name for p in files) else ""

    picked = _pick((os.environ.get("ELAB_PROFILE") or "").strip())
    if picked:
        return picked
    try:
        picked = _pick(str(((cfg.host or {}).get("profile") or "")).strip())
    except ElabError:
        picked = ""
    return picked or next((p.stem for p in files if p.stem == "web"), files[0].stem)


def _mode_host_path(cfg: Config, data: dict) -> str:
    """装配清单的 ``host:`` → 绝对路径（空串 = 只换装配、不换主机事实源）。"""
    raw = str(data.get("host") or "").strip()
    if not raw:
        return ""
    resolved = cfg.resolve(raw, {"ELAB_ROOT": to_fwd(cfg.root)})
    p = Path(resolved)
    if not p.is_absolute():
        p = Path(cfg.root) / p
    return to_fwd(p)


def _mode_record(cfg: Config, path: Path, active: str) -> dict:
    """一份装配清单 → 模式表的一行（前端「模式」按钮的全部依据）。"""
    data = _read_profile(path)
    projs = data.get("projects") or {}
    host = _mode_host_path(cfg, data)
    try:
        order = int(data.get("order") or 999)
    except (TypeError, ValueError):
        order = 999
    return {
        "id": path.stem,
        "label": str(data.get("label") or path.stem),
        "desc": str(data.get("desc") or ""),
        # 按钮顺序（yaml `order:`，缺省 999 = 排在最后，按文件名兜底）：
        # 文件名排序会让 kharon 跑到 web 左边 —— 按钮顺序是**产品顺序**，
        # 不该由字母表决定。
        "order": order,
        # 空 host = 这个模式只换"看哪些工程 / 右列怎么排"，主机事实源沿用当前那份
        "host": host,
        "host_exists": (Path(host).is_file() if host else True),
        "active": path.stem == active,
        "workspaces": [str(x) for x in (data.get("workspaces") or []) if str(x)],
        "archetype": [str(x) for x in (projs.get("archetype") or []) if str(x)],
    }


def _profile_summary(cfg: Config) -> dict | None:
    """读出驾驶舱 profile（R7 / P1-4 的最小装配）。

    文件位置：``<ELAB_ROOT>/cockpit/profiles/*.yaml``。激活顺序见
    :func:`_active_profile_name`；一份都没有时返回 None（前端回退到内置集合）。

    装配三件事 —— **左栏工程可见范围**、**右列工作区顺序**、**项目工具启停**：
    ``workspaces`` 为空 = 全部（内置三件套 + 工具页）；``tools.include``
    非空 = 只启用列出的工具，``tools.exclude`` 优先剔除。

    ★ ``modes`` 是**同一份数据**的另一种投影（每份清单一行，带上自己的
      ``host``）：前端顶栏那个 classic ⇄ Zephyr 开关就渲染它，点一下打
      ``POST /api/mode``。不另设"模式注册表"—— 装配清单本身就是模式。
    """
    files = _profile_files(cfg)
    if not files:
        return None
    active = _active_profile_name(cfg, files)
    chosen = next((p for p in files if p.stem == active), files[0])
    data = _read_profile(chosen)
    tools = data.get("tools") or {}
    projs = data.get("projects") or {}
    return {
        "id": chosen.stem,
        "name": str(data.get("profile") or chosen.stem),
        "label": str(data.get("label") or chosen.stem),
        "desc": str(data.get("desc") or ""),
        "path": to_fwd(chosen),
        # 本模式自己的主机事实源（切换时写进 ELAB_HOST；空 = 不换）
        "host": _mode_host_path(cfg, data),
        "available": [p.stem for p in files],
        # 模式表按 `order:` 排（同一份数据，另一种投影）；缺省 999 = 殿后
        "modes": sorted((_mode_record(cfg, p, active) for p in files),
                        key=lambda m: (m["order"], m["id"])),
        "workspaces": [str(x) for x in (data.get("workspaces") or []) if str(x)],
        "tools": {"include": [str(x) for x in (tools.get("include") or []) if str(x)],
                  "exclude": [str(x) for x in (tools.get("exclude") or []) if str(x)]},
        # ★ 工程可见范围（两个产品并列的落地点）：
        #   classic 的 web.yaml 不写这一节 = 全可见（行为逐字不变）；
        #   Kharon 的 kharon.yaml 写 `projects.archetype: [zephyr]` → 左栏只剩
        #   Zephyr 工程。**不靠前端按名字猜**，也不在代码里判断厂商。
        "projects": {"archetype": [str(x) for x in (projs.get("archetype") or [])
                                   if str(x)]},
    }


def switch_mode(cfg: Config, mode_id: str) -> tuple[Config, dict]:
    """把**本进程**切到另一份装配清单（classic ⇄ Zephyr）。

    只动两处进程级事实，别的什么都不碰：

    ================  =========================================================
    ``ELAB_HOST``     换整份主机事实源（工具链 / 探针 / Zephyr 工作区 / 串口默认）
    ``ELAB_PROFILE``  换装配清单（左栏可见范围 + 右列工作区顺序）
    ================  =========================================================

    ★ 为什么走环境变量而不是"给每条链路各传一份 Config"：run 子进程
      （``elab run``）、调试会话（openocd/gdb）、west、modbus 主站都是
      **派生进程**，环境变量天然被继承 —— 少一处传递就少一处忘传。
      这也正是 ``kharon.cmd`` 当初的做法，运行期切换只是把它从"启动时一次性"
      变成"随时可改"。

    ★ 失败必须**回滚**：环境是全局的，"host 换了但 profile 没换"这种半切状态
      会让左栏工程与右列工作区互相矛盾，且用户看不出是怎么来的。

    返回 ``(新 Config, 新 profile summary)``；调用方负责把 handler/mgr/dbg
    手里的旧 Config 换掉（那才是真正"生效"的地方）。
    """
    files = _profile_files(cfg)
    fid = str(mode_id or "").strip()
    path = next((p for p in files if p.stem == fid), None)
    if path is None:
        known = "、".join(p.stem for p in files) or "(无)"
        raise ElabError(f"未知模式：{fid!r}（可用：{known}）")
    data = _read_profile(path)
    host = _mode_host_path(cfg, data)

    old_host = os.environ.get("ELAB_HOST")
    old_prof = os.environ.get("ELAB_PROFILE")
    with _MODE_LOCK:
        try:
            if host:
                if not Path(host).is_file():
                    raise ElabError(f"模式 {fid} 声明的主机文件不存在：{host}")
                os.environ["ELAB_HOST"] = host
            elif _LAUNCH_HOST_ENV:
                # 没声明 host = 只换装配 → 主机事实源退回启动时那份
                os.environ["ELAB_HOST"] = _LAUNCH_HOST_ENV
            else:
                os.environ.pop("ELAB_HOST", None)
            os.environ["ELAB_PROFILE"] = fid
            new_cfg = Config(cfg.root)
            new_cfg.host                    # 坏 host 在这里就炸，别等切完才炸
            summary = _profile_summary(new_cfg)
        except Exception:
            if old_host is None:
                os.environ.pop("ELAB_HOST", None)
            else:
                os.environ["ELAB_HOST"] = old_host
            if old_prof is None:
                os.environ.pop("ELAB_PROFILE", None)
            else:
                os.environ["ELAB_PROFILE"] = old_prof
            raise
    if summary is None:                     # 切完却没清单 = 目录被删，视同失败
        raise ElabError(f"切换后读不到装配清单（{_profiles_dir(cfg)}）")
    return new_cfg, summary


__all__ = [n for n in list(globals()) if not n.startswith("__")]
