"""elab.wszephyr —— Zephyr 工作区的**事实模型**（只读、零副作用、不跑 west）。

为什么要单独一层
----------------
``westws.py`` 把"工作区"当成一个**已知路径**（从 host.yaml 读一个字符串），
于是只有"我这台机器"这一种世界成立：换台机器路径不同、别人给一个 T2 工作区
就全错、没有工作区时整条链判死。而 Zephyr 官方明确允许**三种应用类型 ×
两种清单拓扑**，形状由 ``.west/config`` 与清单决定，不由目录名决定。

这一层只回答一个问题：**"这个工程落在什么形状里"**，答案是不可变的
:class:`WorkspaceFacts`。其余组件（``westws`` / ``run`` / ``doctor``）只消费
它，不重复实现探测 —— 这条纪律见 ``docs/design/执行计划_Zephyr工作区三形态适配_2026-11-18.md`` §3.2.1⑤。

事实从哪来（唯一契约，禁止按目录名猜）
--------------------------------------
1. ``topdir/.west/config`` 一定存在 → ``[manifest] path`` + ``file`` 指向清单仓与清单文件；
2. ``[zephyr] base``（或 ``$ZEPHYR_BASE``）指向内核；
3. 路径比较**一律** ``Path.resolve()`` 后比较（Windows junction / symlink 会让字符串前缀骗过判断）。

三种应用类型（官方定义，只看 ``<app>`` 的位置）
-----------------------------------------------
===========  ================================  =====================
``app_role``  官方名称                           ``<app>`` 在哪
===========  ================================  =====================
``repo``      仓库应用 repository application   在 ``zephyr`` 仓库内部
``workspace`` 工作区应用 workspace application  west 工作区内、内核仓之外
``standalone`` 独立应用 standalone application  工作区之外
===========  ================================  =====================

两种拓扑（``topology``）：``t1`` = 清单在内核仓（``[manifest] path = zephyr``）；
``t2`` = 清单仓另有其人（``example-application`` 式）。

**禁止的探测方式**（写在这里防后来者踩）
------------------------------------------
* ❌ 看"当前目录下有没有 ``zephyr/``"判工作区 —— 漏 T2，也把 vendored 布局判错；
* ❌ 假设 topdir 叫 ``zephyrproject`` / 清单仓叫 ``zephyr`` —— T2 时都不成立；
* ❌ 假设 app 一定在 ``applications/`` 下 —— 本仓就在 ``applications/``，官方还允许仓外；
* ❌ 用字符串前缀比较 app 与 topdir —— 一律 ``Path.resolve()``；
* ✅ 唯一入口 :func:`discover`。
"""

from __future__ import annotations

import configparser
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from elab.core.config import Config, ElabError, to_fwd

#: 探测结果的进程内缓存：(topdir, app_dir, source) → (时间戳, facts)。
#: 纯文件系统读取很便宜，但 ``run`` 每步都要取一次、面板每秒轮询 —— 30s 足够。
_CACHE: dict[tuple, tuple[float, "WorkspaceFacts"]] = {}
CACHE_TTL_S = 30.0

#: ``.west/config`` 相对 topdir 的位置（west 自己的固定契约）
_WEST_CONFIG = Path(".west") / "config"

#: 没有 ``[manifest] path`` 时的官方缺省
DEFAULT_MANIFEST_PATH = "zephyr"
DEFAULT_MANIFEST_FILE = "west.yml"


@dataclass(frozen=True)
class WorkspaceFacts:
    """一个工程所在工作区的形状（全部为**绝对正斜杠**路径或枚举值）。"""

    present: bool          # 是否存在 west 工作区（有 .west/config）
    topdir: str            # .west/config 所在目录；无工作区为空
    topology: str          # "t1" | "t2" | "none"
    manifest_path: str     # 清单文件绝对路径（topdir/<path>/<file>）
    manifest_repo: str     # 清单仓相对 topdir 的目录名（T1 时 = "zephyr"）
    zephyr_base: str       # [zephyr] base → topdir/zephyr → $ZEPHYR_BASE → ""
    zephyr_version: str    # 读 <zephyr_base>/VERSION（4.5.99 之类）
    app_dir: str           # 工程根（project.yaml: root），可能为空
    app_role: str          # "repo" | "workspace" | "standalone"
    app_arg: str           # 喂给 `west build` 的位置参数
    build_dir: str         # 官方口径的默认构建目录
    cwd: str               # 该形态下 `west build` 应在哪个目录跑
    source: str            # "env" | "detect" | "host" | "none"（topdir 谁给的）

    @property
    def west_usable(self) -> bool:
        """能不能走 ``west build``：有工作区即可（内核仓/清单都要在）。"""
        return self.present and bool(self.topdir)

    @property
    def cmake_usable(self) -> bool:
        """能不能降级 cmake 直编：只要 ``ZEPHYR_BASE`` 在就够。"""
        return bool(self.zephyr_base)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["west_usable"] = self.west_usable
        d["cmake_usable"] = self.cmake_usable
        return d


# ── 读取单点事实 ────────────────────────────────────────────────
def _read_west_config(topdir: Path) -> tuple[str, str, str]:
    """``.west/config`` → (manifest_path, manifest_file, zephyr_base_raw)。"""
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(topdir / _WEST_CONFIG, encoding="utf-8")
    except (OSError, configparser.Error):
        return DEFAULT_MANIFEST_PATH, DEFAULT_MANIFEST_FILE, ""
    mp = (cfg.get("manifest", "path", fallback="") or "").strip() or DEFAULT_MANIFEST_PATH
    mf = (cfg.get("manifest", "file", fallback="") or "").strip() or DEFAULT_MANIFEST_FILE
    zb = (cfg.get("zephyr", "base", fallback="") or "").strip()
    return mp, mf, zb


def _zephyr_version(base: Path) -> str:
    """读 ``<zephyr>/VERSION`` → ``4.5.99``（读不到返回空，不猜）。"""
    f = base / "VERSION"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    vals: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()
    major = vals.get("VERSION_MAJOR")
    minor = vals.get("VERSION_MINOR")
    patch = vals.get("PATCHLEVEL")
    if not (major and minor and patch):
        return ""
    ver = f"{major}.{minor}.{patch}"
    extra = vals.get("EXTRAVERSION") or ""
    return ver + (extra if extra else "")


def _host_workspace(cfg: Config) -> str:
    """``kharon.host.yaml: tools.zephyr.workspace``（读不到返回空）。"""
    try:
        from elab.workflow.run import _host_value
        return to_fwd(_host_value(cfg, "tools.zephyr.workspace") or "")
    except ElabError:
        return ""


def _host_base(cfg: Config) -> str:
    try:
        from elab.workflow.run import _host_value
        return to_fwd(_host_value(cfg, "tools.zephyr.base") or "")
    except ElabError:
        return ""


def _find_topdir(start: Path) -> Path | None:
    """从 ``start`` 向上找最近的含 ``.west/config`` 的目录（找不到 None）。"""
    try:
        cur = start.resolve()
    except OSError:
        return None
    if cur.is_file():
        cur = cur.parent
    for node in [cur, *cur.parents]:
        if (node / _WEST_CONFIG).is_file():
            return node
    return None


def _is_under(child: Path, parent: Path) -> bool:
    """``child`` 是否在 ``parent`` 之内（**resolve 后**比较，防 junction/symlink）。"""
    if not str(parent) or not str(child):
        return False
    try:
        c = child.resolve()
        p = parent.resolve()
    except OSError:
        return False
    return c == p or p in c.parents


def _rel_posix(target: Path, base: Path) -> str:
    """相对路径（posix 斜杠）；跨盘/不在其下时退回绝对路径。"""
    try:
        return to_fwd(target.resolve().relative_to(base.resolve()))
    except (ValueError, OSError):
        return to_fwd(target)


# ── 主入口 ──────────────────────────────────────────────────────
def discover(cfg: Config, app_dir: str = "", *, refresh: bool = False) -> WorkspaceFacts:
    """探测 ``app_dir`` 所在的工作区形状（纯读，不跑 west、不写盘）。

    ``topdir`` 的来源优先级：

    1. ``ELAB_WORKSPACE`` 环境变量（显式覆盖，CI 与多工作区都用它）；
    2. **从 app 目录向上找最近的** ``.west/config``（多工作区共存的关键）；
    3. ``kharon.host.yaml: tools.zephyr.workspace``（app 在工作区之外时的兜底，
       本仓 ``applications/`` 走的就是这条）；
    4. 都没有 → 无工作区（``present=False``，可降级 cmake 直编）。

    > 第 2 步排在第 3 步之前是相对计划书 §3.2 的**刻意修正**：host 是全局单值，
      若把它排前面，"agent 同时接管两个工作区"会互相覆盖（计划书 §10.1-2）。
      app 在工作区之外时探测必然空手，host 照样生效 —— 现有工程行为不变。
    """
    app = Path(to_fwd(app_dir)) if str(app_dir or "").strip() else Path.cwd()
    # ★ 相对路径按**仓库根**解析，不按进程 cwd：调用方可能给 `applications/x`（工程量少写
    #   `${ELAB_ROOT}`），也可能给绝对路径；后者不能依赖"当前在哪个目录"。
    if not app.is_absolute():
        app = Path(cfg.root) / app
    app_key = to_fwd(app) if str(app_dir or "").strip() else ""
    env_ws = to_fwd(os.environ.get("ELAB_WORKSPACE") or "")

    key = (env_ws, app_key)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < CACHE_TTL_S:
        return hit[1]

    topdir: Path | None = None
    source = "none"
    if env_ws:
        cand = Path(env_ws)
        if (cand / _WEST_CONFIG).is_file():
            topdir, source = cand, "env"
    if topdir is None and app.is_dir():
        found = _find_topdir(app)
        if found is not None:
            topdir, source = found, "detect"
    if topdir is None:
        host_ws = _host_workspace(cfg)
        if host_ws:
            cand = Path(host_ws)
            if (cand / _WEST_CONFIG).is_file():
                topdir, source = cand, "host"

    facts = _build(cfg, app, topdir, source)
    _CACHE[key] = (now, facts)
    return facts


def _build(cfg: Config, app: Path, topdir: Path | None, source: str) -> WorkspaceFacts:
    manifest_path = ""
    manifest_repo = ""
    topology = "none"
    zb = Path("")

    if topdir is not None:
        manifest_repo, manifest_file, zb_raw = _read_west_config(topdir)
        manifest_path = to_fwd(topdir / manifest_repo / manifest_file)
        topology = "t1" if manifest_repo == DEFAULT_MANIFEST_PATH else "t2"
        if zb_raw:
            zb = Path(zb_raw) if Path(zb_raw).is_absolute() else (topdir / zb_raw)
        elif (topdir / "zephyr").is_dir():
            zb = topdir / "zephyr"
        elif os.environ.get("ZEPHYR_BASE"):
            zb = Path(os.environ["ZEPHYR_BASE"])
        elif _host_base(cfg):
            zb = Path(_host_base(cfg))
    else:
        env_base = os.environ.get("ZEPHYR_BASE") or _host_base(cfg)
        if env_base:
            zb = Path(to_fwd(env_base))

    zephyr_base = to_fwd(zb) if str(zb) and zb.is_dir() else ""
    zephyr_version = _zephyr_version(zb) if zephyr_base else ""

    app_dir_s = to_fwd(app) if app.is_dir() else ""

    # ── app_role：只看 `<app>` 落在哪（三种应用类型的唯一判据）────────
    if topdir is None:
        app_role = "standalone"
    elif zephyr_base and _is_under(app, Path(zephyr_base)):
        app_role = "repo"
    elif _is_under(app, topdir):
        app_role = "workspace"
    else:
        app_role = "standalone"

    # ── app_arg / cwd / build_dir：三形态的**全部差异**（计划书 §3.3）──
    if app_role == "repo":
        base = Path(zephyr_base)
        app_arg = _rel_posix(app, base)
        cwd_ = zephyr_base
        # 官方默认把 build 落进 <zephyr_base>/build（= 写进内核仓），改落平级 build/
        build_dir = to_fwd(base.parent / "build" / app.name)
    elif app_role == "workspace":
        app_arg = _rel_posix(app, topdir)
        cwd_ = to_fwd(topdir)
        build_dir = to_fwd(topdir / "build")
    else:
        app_arg = app_dir_s
        cwd_ = to_fwd(topdir) if topdir is not None else app_dir_s
        build_dir = to_fwd(app / "build") if app.is_dir() else ""

    return WorkspaceFacts(
        present=topdir is not None,
        topdir=to_fwd(topdir) if topdir is not None else "",
        topology=topology,
        manifest_path=manifest_path,
        manifest_repo=manifest_repo,
        zephyr_base=zephyr_base,
        zephyr_version=zephyr_version,
        app_dir=app_dir_s,
        app_role=app_role,
        app_arg=app_arg,
        build_dir=build_dir,
        cwd=cwd_,
        source=source,
    )


# ── P2：board 三源 + 模块缺口 ───────────────────────────────────
#: SoC family → 必须的模块（确定性映射，**不猜**；未知 family 返回空）。
#: 依据：``zephyr/soc/<family>/`` 的 Kconfig 依赖 ``hal_<family>``；
#: ARM 平台一律还要 ``cmsis_6``（4.x 起由 ``hal_cmsis`` 换成独立仓）。
SOC_MODULES: dict[str, tuple[str, ...]] = {
    "stm32": ("hal_stm32", "cmsis_6"),
    "espressif": ("hal_espressif",),
    "nordic": ("hal_nordic", "cmsis_6"),
    "nxp": ("hal_nxp", "cmsis_6"),
    "atmel": ("hal_atmel", "cmsis_6"),
    "ti": ("hal_ti", "cmsis_6"),
    "infineon": ("hal_infineon", "cmsis_6"),
    "raspberrypi": ("hal_rpi_pico", "cmsis_6"),
    "silabs": ("hal_silabs", "cmsis_6"),
    "gd32": ("hal_gigadevice", "cmsis_6"),
    "microchip": ("hal_microchip", "cmsis_6"),
}


def _repo_boards(cfg: Config, repo: str) -> list[str]:
    """``<repo>/boards/**/board.yml`` 直接枚举（不依赖 west，无工作区也能用）。

    口径与 ``west boards`` 一致：``<vendor>/<board>``，多核板追加 ``/<core>``。
    读不到 ``board.yml`` 时退回目录名（宁可显示得糙，也不要漏板）。
    """
    from elab.core import _yaml

    root = Path(repo) / "boards"
    if not root.is_dir():
        return []
    out: list[str] = []
    for bm in sorted(root.glob("*/*/board.yml")):
        data: dict = {}
        try:
            data = _yaml.load_file(bm) or {}
        except Exception:                       # noqa: BLE001
            data = {}
        node = data.get("board") or {}
        name = str(node.get("name") or bm.parent.name)
        socs = node.get("socs") or []
        if isinstance(socs, list) and socs:
            for soc in socs:
                core = ""
                if isinstance(soc, dict):
                    core = str(soc.get("cpucluster") or soc.get("name") or "")
                if core:
                    out.append(f"{bm.parent.parent.name}/{name}/{core}")
                else:
                    out.append(f"{bm.parent.parent.name}/{name}")
        else:
            out.append(f"{bm.parent.parent.name}/{name}")
    return out


def boards_by_source(cfg: Config, facts: WorkspaceFacts | None = None,
                     *, west_boards: list[str] | None = None) -> dict:
    """board 的三个来源（回答"这块板从哪来"）。

    * ``pack``：芯片包 ``target`` 声明（本仓真正在用的板）；
    * ``west``：``west boards``（工作区内所有，含模块板）；
    * ``repo``：``<ZEPHYR_ROOT>/kharon-boards`` 直接扫（无工作区时唯一来源）。

    三个列表都**去重**，``all`` 是并集（按 pack → west → repo 顺序，同名只留一条）。
    """
    packs: list[str] = []
    try:
        cards = cfg.projects
    except ElabError:
        cards = {}
    for name in cards:
        try:
            proj = cfg.resolved_project(name)
            # ★ 只统计 **Zephyr 工程**：classic 的芯片也有 target，混进来会把
            #   "这块板从哪来"答成"at32f421g8 来自 zephyr 芯片包"（假的）。
            arch = str(proj.get("archetype") or "")
            prov = str(proj.get("provider") or "")
            if arch != "zephyr" and not prov.startswith("zephyr/"):
                continue
            chip = cfg.chip_of(proj)
        except ElabError:
            continue
        target = str(chip.get("target") or chip.get("id") or "")
        board = target.split("/")[0] if target else ""
        if board and board not in packs:
            packs.append(board)

    repo_root = Path(cfg.zephyr_root) / "kharon-boards"
    if not repo_root.is_dir():
        # 旧布局兼容：物理拆分前的自研板模块
        repo_root = Path(cfg.root) / "zephyr_modules" / "elab-boards"
    repo: list[str] = []
    if repo_root.is_dir():
        for b in _repo_boards(cfg, str(repo_root)):
            if b not in repo:
                repo.append(b)

    west = list(west_boards or [])
    allb: list[str] = []
    for b in [*packs, *west, *repo]:
        if b not in allb:
            allb.append(b)
    return {"pack": packs, "west": west, "repo": repo, "all": allb}


def _board_soc(zephyr_base: str, board_name: str) -> str:
    """从 ``<zephyr_base>/boards/*/<board>/board.yml`` 读 SoC 名（读不到返回空）。

    为什么需要它：``west boards`` 打的是**裸 board 名**（``blackpill_f411ce``），
    名字里没有 vendor —— 只看名字会把 STM32 板判成"未知 family"，于是"缺哪个
    模块"永远答不出来。``board.yml`` 的 ``board.socs[0].name`` 才是权威。
    """
    if not zephyr_base or not board_name:
        return ""
    from elab.core import _yaml

    base = Path(zephyr_base) / "boards"
    if not base.is_dir():
        return ""
    for bm in base.glob(f"*/{board_name}/board.yml"):
        try:
            data = _yaml.load_file(bm) or {}
        except Exception:                       # noqa: BLE001
            continue
        socs = ((data.get("board") or {}).get("socs") or [])
        if socs:
            first = socs[0]
            if isinstance(first, dict):
                return str(first.get("name") or "")
            return str(first)
    return ""


#: SoC/芯片名前缀 → SOC_MODULES family 别名：Zephyr 的 SoC 名是芯片名
#: （``esp32s31`` / ``nrf52840``…），不是 vendor 名 —— 只对 SOC_MODULES 的键做
#: 前缀匹配会把它们判成"未知 family"。实测踩到：esp32s31 自研板。
SOC_ALIASES: dict[str, str] = {
    "esp32": "espressif",
    "nrf": "nordic",
    "kw": "nxp",
    "mk": "microchip",
}


def _family_of(*candidates: str) -> str:
    for cand in candidates:
        seg = str(cand or "").strip().lower()
        if not seg:
            continue
        for key in SOC_MODULES:
            if seg.startswith(key):
                return key
        for key, fam in SOC_ALIASES.items():
            if seg.startswith(key):
                return fam
    return ""


def modules_for(board: str, modules_present: list[str] | None = None,
                *, zephyr_base: str = "") -> dict:
    """某块板**需要哪些模块**、缺哪些、怎么补（确定性映射，不猜）。

    ``board`` 可以是 ``<vendor>/<board>[/<core>]`` 或裸 board 名。family 依次取
    vendor 段 → ``board.yml`` 的 SoC 名；都没有就返回空 ``needed``（不猜）。
    """
    seg = [s for s in str(board or "").split("/") if s]
    vendor = seg[0] if seg else ""
    # ★ family 候选不只有 vendor 段：`<board>/<soc>/<core>` 三段式的 seg[1] 是
    #   SoC 名（esp32s31…），自研板 seg[0] 是板名（esp32s31_function_coreboard），
    #   恰恰**不是** vendor。全段都试 + board.yml 的 SoC 名兜底。
    family = _family_of(vendor, *seg[1:],
                        _board_soc(zephyr_base, vendor) if zephyr_base else "")
    needed = list(SOC_MODULES.get(family, ()))
    present = set(modules_present or [])
    missing = [m for m in needed if m not in present] if modules_present is not None else []
    return {
        "board": board,
        "family": family,
        "needed": needed,
        "missing": missing,
        "fix": [f"python -m west update {m}" for m in missing],
    }
