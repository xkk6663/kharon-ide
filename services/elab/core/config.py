"""elab.config —— 配置加载与变量插值（L0 主机层 / L1 芯片层 / L4 项目层）。

设计要点
--------
- **唯一主机事实源**：所有主机路径只在 ``elab.host.yaml`` 写一次，
  其余层只允许用 ``${...}`` 引用，禁止各自拼绝对路径。
- **变量插值**：``${ELAB_ROOT}`` = 本目录（elab-flow/）；项目作用域内还可用
  ``${root}`` / ``${work_dir}`` / ``${name}`` / ``${project_name}``。
  未命中的 ``${X}`` 回退到环境变量 ``X``；再未命中则原样保留（便于 doctor 报错）。
- 路径一律输出**正斜杠**形式（cmake / ninja / gcc 均接受），
  需要进 PATH 时另用 :func:`to_os_path`。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from elab.core import _yaml

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


class ElabError(RuntimeError):
    """elab 的可预期错误（用户可修复），CLI 捕获后打印为 human 消息。"""


def to_fwd(p) -> str:
    """统一为正斜杠字符串（cmake 参数用）。"""
    s = str(p)
    return s.replace("\\", "/")


def to_os_path(p) -> str:
    """转为当前系统原生的路径字符串（PATH 用）。"""
    return os.path.normpath(str(p))


def _env_path(name: str) -> str:
    """读路径类环境变量覆盖：空 / 全空白视为未设置（回退 yaml 事实源）。"""
    return (os.environ.get(name) or "").strip()


# ── 主机 profile（P1：显式分层，环境变量 > host.local.yaml > profile）──────
#: profile 文件固定放在 ``config/host.<name>.profile.yaml``；名字即选择器。
HOST_PROFILE_DIR = "config"
HOST_PROFILES = ("classic", "kharon")
#: 本机绝对路径覆盖文件（gitignored），按 profile 分节。
HOST_LOCAL_FILE = "host.local.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并（override 优先；列表整体替换，不做拼接）。

    列表替换而非追加：``svd_roots`` / ``providers.roots`` 这类"发现根"
    语义是**完整声明**，半合并会得到两份互不知情的路径。
    """
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


class Config:
    """elab-flow 配置树。

    ``root`` 默认取本文件所在包的上一级（= ``elab-flow/``），
    也可由环境变量 ``ELAB_ROOT`` 覆盖（CI 场景）。
    """

    def __init__(self, root=None):
        if root is None:
            root = os.environ.get("ELAB_ROOT")
        if root is None:
            root = Path(__file__).resolve().parents[3]  # services/elab/core/config.py -> elab-flow
        self.root = Path(root).resolve()
        self._host = None
        self._host_fb = None
        self._chips = None
        self._all_chips = None
        self._pinned_chips: dict[str, dict] = {}
        self._projects = None
        self._packs = None
        self._zephyr_root = None
        # Product -> Target 选择（仅本进程有效；CLI run 一次只跑一个 target）
        self._target_selection: dict[str, str] = {}

    # ── 双根：IDE 根 / Zephyr 工作区根 ───────────────────────────
    @property
    def ide_root(self) -> Path:
        """IDE 根（历史 ``ELAB_ROOT`` 的新名字）。"""
        return self.root

    @property
    def zephyr_root(self) -> Path:
        """Zephyr 工作区根（west topdir）。

        目标布局：``IDE_ROOT/../zephyr-workspace``；旧布局回落到
        ``IDE_ROOT/west``。显式环境变量 ``ZEPHYR_ROOT`` / ``ELAB_ZEPHYR_ROOT``
        优先，保证迁移期与 CI 都能指定。
        """
        if self._zephyr_root is None:
            raw = (os.environ.get("ZEPHYR_ROOT")
                   or os.environ.get("ELAB_ZEPHYR_ROOT") or "").strip()
            if raw:
                p = Path(raw)
                if not p.is_absolute():
                    p = self.root / p
                self._zephyr_root = p.resolve()
            else:
                target = self.root.parent / "zephyr-workspace"
                if (target / ".west" / "config").is_file():
                    self._zephyr_root = target.resolve()
                else:
                    self._zephyr_root = (self.root / "west").resolve()
        return self._zephyr_root

    def root_ctx(self) -> dict:
        """双根插值上下文；``ELAB_ROOT`` 是 ``IDE_ROOT`` 的兼容别名。"""
        return {
            "IDE_ROOT": to_fwd(self.ide_root),
            "ELAB_ROOT": to_fwd(self.ide_root),
            "ZEPHYR_ROOT": to_fwd(self.zephyr_root),
        }

    @property
    def products_dir(self) -> Path:
        """产品定义根：目标布局在 workspace 的 kharon-manifest/products。"""
        env = (os.environ.get("KHARON_PRODUCTS_DIR") or "").strip()
        if env:
            p = Path(env)
            if not p.is_absolute():
                p = self.root / p
            return p.resolve()
        target = self.zephyr_root / "kharon-manifest" / "products"
        if target.is_dir():
            return target.resolve()
        return (self.root / "projects").resolve()

    @property
    def runtime_dir(self) -> Path:
        """IDE 侧运行时状态根（调试/帧/组件 JSON），不进产品仓。"""
        env = (os.environ.get("KHARON_RUNTIME_DIR") or "").strip()
        if env:
            p = Path(env)
            if not p.is_absolute():
                p = self.root / p
            return p.resolve()
        return (self.root / ".work" / "projects").resolve()

    # ── L0 主机层 ────────────────────────────────────────────────
    @property
    def host_local(self) -> dict:
        """本机绝对路径覆盖（``host.local.yaml``，按 profile 分节；缺失 = 空）。"""
        p = self.root / HOST_LOCAL_FILE
        if not p.exists():
            return {}
        try:
            return _yaml.load_file(p) or {}
        except (ElabError, OSError):
            return {}

    @property
    def host_selection(self) -> dict:
        """主机事实源**选择结果**（profile / 路径 / 是否显式）。

        选择顺序（- 方案 §4-P1）：
          1. ``ELAB_HOST`` 是 profile 名（classic / kharon）→ ``config/host.<name>.profile.yaml``
          2. ``ELAB_HOST`` 是路径 → 该文件（历史用法，保持兼容）
          3. 未设置 → 仓库根 ``elab.host.yaml``（旧布局兼容）；没有才落回 classic profile

        ``implicit=True`` 表示用户没有显式选择 —— doctor 会主动报出来，
        而不是像以前那样静默在两份 host 之间"猜"。
        """
        raw = (os.environ.get("ELAB_HOST") or "").strip()
        if raw in HOST_PROFILES:
            return {"profile": raw, "path": self.root / HOST_PROFILE_DIR /
                    f"host.{raw}.profile.yaml", "origin": "env-profile",
                    "implicit": False}
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = self.root / p
            return {"profile": None, "path": p, "origin": "env-file", "implicit": False}
        legacy = self.root / "elab.host.yaml"
        if legacy.exists():
            return {"profile": None, "path": legacy, "origin": "legacy-file",
                    "implicit": True}
        # 本仓只保留 Kharon：classic profile 不存在时默认落到 kharon。
        # 旧仓仍有 classic profile，行为不变。
        classic = self.root / HOST_PROFILE_DIR / "host.classic.profile.yaml"
        default = "classic" if classic.exists() else "kharon"
        return {"profile": default, "path": self.root / HOST_PROFILE_DIR /
                f"host.{default}.profile.yaml", "origin": "default-profile",
                "implicit": True}

    @property
    def host_path(self) -> Path:
        return self.host_selection["path"]

    @property
    def host(self) -> dict:
        if self._host is None:
            sel = self.host_selection
            if not sel["path"].exists():
                hint = ""
                if sel["profile"]:
                    hint = (f"（profile={sel['profile']}；可从 "
                            f"config/host.local.yaml.example 复制本机覆盖）")
                raise ElabError(f"找不到主机配置：{sel['path']}{hint}")
            data = _yaml.load_file(sel["path"]) or {}
            if sel["profile"]:
                local = self.host_local.get(sel["profile"]) or {}
                data = deep_merge(data, local)
                if sel["profile"] == "kharon":
                    # bootstrap --write-local 的产物（扁平 kharon host 覆盖）；
                    # 优先级最高（比 host.local.yaml 更专门），但仍低于环境变量。
                    kp = self.root / "kharon.local.yaml"
                    if kp.exists():
                        try:
                            data = deep_merge(data, _yaml.load_file(kp) or {})
                            data["_local"] = True
                        except (ElabError, OSError):
                            pass
                data["_profile"] = sel["profile"]
                data["_local"] = bool(local) or bool(data.get("_local"))
            else:
                data["_profile"] = data.get("profile")
                data["_local"] = False
            data["_origin"] = sel["origin"]
            data["_path"] = to_fwd(sel["path"])
            self._host = data
        return self._host

    def host_value(self, key: str, source: bool = False):
        """按点路径取主机事实源的值，可选返回**来源**。

        来源取值：``env``（ELAB_* 覆盖）/ ``local``（host.local.yaml）/
        ``profile``（config/host.*.profile.yaml）/ ``legacy``（旧 elab.host.yaml）。
        """
        env_map = {
            "toolchains.arm-none-eabi.root": "ELAB_ARM_GCC_ROOT",
            "tools.cmake.path": "ELAB_CMAKE",
            "tools.ninja.path": "ELAB_NINJA",
            "tools.openocd.path": "ELAB_OPENOCD",
            "tools.openocd.scripts": "ELAB_OPENOCD_SCRIPTS",
        }
        node = self.host
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        origin = ("legacy" if self.host_selection["profile"] is None
                  else "profile")
        local = self.host_local.get(self.host_selection["profile"] or "") or {}
        probe = local
        for part in key.split("."):
            probe = probe.get(part) if isinstance(probe, dict) else None
        if probe is not None:
            origin = "local"
        env_name = env_map.get(key)
        if env_name and _env_path(env_name):
            origin = "env"
        return (node, origin) if source else node

    @property
    def host_fallback(self) -> dict:
        """**已废弃**：旧版自动读 ``kharon.host.yaml`` 的隐式兜底。

        P1 起主机事实源由 profile 显式选择（``ELAB_HOST=classic|kharon``），
        两 profile 各自声明自己需要的键；"主文件缺键就偷偷看另一份"会让
        doctor 与构建读到不同的值（f411_breath 实测踩到）。

        保留属性只为旧调用方不崩：返回空 dict 并发出 DeprecationWarning。
        """
        import warnings

        warnings.warn(
            "host_fallback 已废弃：请用 ELAB_HOST=classic|kharon 选择 profile，"
            "本机绝对路径写入 host.local.yaml",
            DeprecationWarning, stacklevel=2)
        return {}

    # ── L1 芯片层：只来自已安装的适配包 ────────────────────────────
    @property
    def pack_roots(self) -> list[Path]:
        """适配包搜索根（见 ``elab.pkg.pack_roots``）。"""
        from elab.platform import pkg
        return pkg.pack_roots(self)

    @property
    def packs(self) -> dict[str, dict]:
        """已发现的适配包 ``{name: record}``（不校验、不安装）。"""
        if self._packs is None:
            from elab.platform import pkg
            self._packs = pkg.discover(self)
        return self._packs

    @property
    def active_pack(self) -> dict | None:
        """当前启用的适配包（``packs/installed/active.json``；一次只启用一套）。"""
        from elab.platform import pkg
        return pkg.active_pack(self, self.packs)

    def _load_chips(self, records: list[dict]) -> dict:
        """从给定包记录加载 ``{ref|id: chip}``（含跨包重复校验）。

        ``records`` 是 ``pkg.discover`` 返回的记录（字段 ``dir`` / ``name``）。
        """
        out: dict[str, dict] = {}
        by_ref: dict[str, str] = {}
        by_id: dict[str, str] = {}
        for rec in records:
            chips_dir = Path(rec["dir"]) / "chips"
            if not chips_dir.is_dir():
                continue
            for path in sorted(chips_dir.rglob("*.yaml")):
                chip = _yaml.load_file(path) or {}
                if "id" not in chip:
                    raise ElabError(f"芯片文件缺少 id：{path}")
                path_s = to_fwd(path)
                ref = f"{chip.get('vendor')}/{chip['id']}"
                if ref in by_ref and by_ref[ref] != path_s:
                    raise ElabError(
                        f"芯片 {ref} 在多个适配包中重复：{by_ref[ref]} 与 {path_s}")
                if chip["id"] in by_id and by_id[chip["id"]] != path_s:
                    raise ElabError(
                        f"芯片 id {chip['id']} 在多个适配包中重复："
                        f"{by_id[chip['id']]} 与 {path_s}")
                by_ref[ref] = path_s
                by_id[chip["id"]] = path_s
                chip["_path"] = path_s
                chip["_pack"] = rec["name"]
                out[ref] = chip
                out.setdefault(chip["id"], chip)
        return out

    @property
    def chips(self) -> dict:
        """返回 ``{ref: chip}``，ref 同时支持 ``vendor/id`` 与裸 ``id``。

        ★ 骨架仓**不再直读根目录 ``chips/``**：芯片数据只来自**当前启用**的
          那个适配包（``packs/installed/active.json``）—— 一次只启用一套，其余
          已装包仍留在本地，切换只需改启用状态。没有启用包时返回空字典，
          这就是"通用工具链"的形态。
        """
        if self._chips is None:
            active = self.active_pack
            self._chips = self._load_chips([active] if active else [])
        return self._chips

    @property
    def all_chips(self) -> dict:
        """**所有已安装包**的芯片视图（只读）。

        ★ 与 ``chips`` 的区别：``chips`` 只含当前启用包（项目运行时口径）；
          ``all_chips`` 供 ``adapt`` 探测使用 —— 接一颗新芯片的第一件事不该先
          切换 active（adapter 产出的是固定包项目，运行时不看 active）。
        """
        if self._all_chips is None:
            self._all_chips = self._load_chips(list(self.packs.values()))
        return self._all_chips

    @property
    def vendor_fingerprints(self) -> list[dict]:
        """**所有已安装包**声明的厂商指纹（adapt 探测器的唯一事实源）。

        ★ 不用 active 包口径：adapt 要在「当前启用 stm32」时也能识别 ESP-IDF
          工程，否则接新芯片前得先切换 active —— 顺序反了。
        """
        out: list[dict] = []
        for rec in self.packs.values():
            man = rec.get("manifest") or {}
            for fp in man.get("vendor_fingerprints") or []:
                if not isinstance(fp, dict):
                    continue
                item = dict(fp)
                item["vendor"] = man.get("vendor") or ""
                item["pack"] = rec.get("name") or ""
                out.append(item)
        return out

    # ── L4 项目层 ────────────────────────────────────────────────
    @property
    def projects(self) -> dict:
        if self._projects is None:
            out: dict[str, dict] = {}
            # ★ 递归发现：`projects/kharon/*.yaml` 这类发行子目录也要被看见。
            #   packs / providers 两处发现根早已数据驱动，只有 projects 还是
            #   单层 glob —— 新增发行时不必再动内核（Kharon T3）。
            base = self.products_dir
            for path in sorted(base.rglob("*.yaml")):
                proj = _yaml.load_file(path) or {}
                if "name" not in proj:
                    raise ElabError(f"项目文件缺少 name：{path}")
                proj["_path"] = to_fwd(path)
                out[proj["name"]] = proj
            self._projects = out
        return self._projects

    # ── Product / Target ───────────────────────────────────────────
    def targets_of(self, proj: dict) -> list[dict]:
        """产品声明的 target 列表；旧单-chip 产品返回空列表。

        规则（计划 §5.3）：
        - ``targets[].id`` 唯一，``chip`` 必须存在；
        - 顶层 ``chip`` 若存在，只能等于 ``targets[0].chip``（默认 target 的别名），
          避免“两个真相”。
        """
        targets = proj.get("targets") or []
        if not isinstance(targets, list):
            raise ElabError(f"产品 {proj.get('name')} 的 targets 必须是列表")
        out: list[dict] = []
        seen: set[str] = set()
        for t in targets:
            if not isinstance(t, dict):
                raise ElabError(f"产品 {proj.get('name')} 的 target 必须是映射")
            tid = str(t.get("id") or "").strip()
            chip = str(t.get("chip") or "").strip()
            if not tid or not chip:
                raise ElabError(
                    f"产品 {proj.get('name')} 的 target 缺 id 或 chip：{t!r}")
            if tid in seen:
                raise ElabError(f"产品 {proj.get('name')} 的 target id 重复：{tid}")
            seen.add(tid)
            out.append(t)
        top = str(proj.get("chip") or "").strip()
        if out and top and top != str(out[0].get("chip") or ""):
            raise ElabError(
                f"产品 {proj.get('name')} 顶层 chip={top} 与 targets[0].chip="
                f"{out[0].get('chip')} 冲突；只保留一个真相")
        return out

    def select_target(self, project: str, target_id: str | None = None) -> str | None:
        """为当前进程选定一个 target；无 targets 的旧产品返回 None。"""
        if project not in self.projects:
            raise ElabError(f"未知项目：{project}")
        targets = self.targets_of(self.projects[project])
        if not targets:
            if target_id:
                raise ElabError(f"产品 {project} 没有 targets，不能指定 --target {target_id}")
            self._target_selection.pop(project, None)
            return None
        if target_id is None:
            if len(targets) > 1:
                ids = ", ".join(str(t["id"]) for t in targets)
                raise ElabError(
                    f"产品 {project} 有 {len(targets)} 个 target，请用 --target 指定：{ids}")
            target_id = str(targets[0]["id"])
        if target_id not in {str(t["id"]) for t in targets}:
            ids = ", ".join(str(t["id"]) for t in targets)
            raise ElabError(f"产品 {project} 没有 target {target_id}（可选：{ids}）")
        self._target_selection[project] = target_id
        return target_id

    def _apply_target(self, proj: dict, target_id: str | None) -> dict:
        """把 target 的 chip / build / serial / monitor 合并进产品视图。"""
        targets = self.targets_of(proj)
        if not targets:
            if target_id:
                raise ElabError(f"产品 {proj.get('name')} 没有 targets")
            return proj
        if target_id is None:
            if len(targets) > 1:
                ids = ", ".join(str(t["id"]) for t in targets)
                raise ElabError(
                    f"产品 {proj.get('name')} 有 {len(targets)} 个 target，"
                    f"请用 --target 指定：{ids}")
            target_id = str(targets[0]["id"])
        target = next((t for t in targets if str(t["id"]) == str(target_id)), None)
        if target is None:
            ids = ", ".join(str(t["id"]) for t in targets)
            raise ElabError(
                f"产品 {proj.get('name')} 没有 target {target_id}（可选：{ids}）")

        merged = dict(proj)
        merged["chip"] = str(target.get("chip") or "")
        merged["_target_id"] = str(target["id"])
        for key, value in target.items():
            if key in ("id", "chip"):
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        # 顶层 build.kind/source 仍然继承，target 只覆盖 work_dir 等局部键。
        return merged

    # ── 插值 ─────────────────────────────────────────────────────
    def project_ctx(self, proj: dict) -> dict:
        """构造项目作用域插值上下文（已解析的绝对值）。"""
        root = to_fwd(self.resolve(proj["root"], {}))
        build = proj.get("build") or {}
        ctx = dict(self.root_ctx())
        ctx.update({
            "root": root,
            "name": proj.get("name", ""),
            "project_name": build.get("project_name", proj.get("name", "")),
        })
        # work_dir 可能自身引用 ${ELAB_ROOT} / ${ZEPHYR_ROOT}
        ctx["work_dir"] = to_fwd(self.resolve(build.get("work_dir", ""), ctx))
        return ctx

    def resolve(self, value, ctx: dict | None = None):
        """递归插值 ``${...}``。非字符串原样返回。"""
        if not isinstance(value, str):
            return value
        ctx = ctx or {}

        def sub(m: re.Match) -> str:
            key = m.group(1)
            if key in ctx:
                return str(ctx[key])
            if key == "IDE_ROOT":
                return to_fwd(self.ide_root)
            if key == "ZEPHYR_ROOT":
                return to_fwd(self.zephyr_root)
            if key == "ELAB_ROOT":
                return to_fwd(self.ide_root)
            if key in os.environ:
                return os.environ[key]
            return m.group(0)

        for _ in range(10):
            new = _VAR_RE.sub(sub, value)
            if new == value:
                break
            value = new
        return value

    def resolve_deep(self, node, ctx: dict | None = None):
        """对整个 dict/list 递归插值。"""
        if isinstance(node, dict):
            return {k: self.resolve_deep(v, ctx) for k, v in node.items()}
        if isinstance(node, list):
            return [self.resolve_deep(v, ctx) for v in node]
        return self.resolve(node, ctx)

    # ── 组合视图 ─────────────────────────────────────────────────
    def resolved_project(self, name: str, target_id: str | None = None) -> dict:
        """取项目并完成全部插值（含 host 派生值）。

        ``target_id`` 省略时使用 ``select_target`` 的选择；多 target 产品若
        从未选择，会在这里报错并列出可选 id，而不是静默取第一个。
        """
        if name not in self.projects:
            known = ", ".join(sorted(self.projects)) or "(无)"
            raise ElabError(f"未知项目：{name}（可选：{known}）")
        raw = self._apply_target(
            dict(self.projects[name]),
            target_id if target_id is not None else self._target_selection.get(name))
        ctx = self.project_ctx(raw)
        proj = self.resolve_deep(raw, ctx)
        proj["_ctx"] = ctx
        return proj

    def chip_of(self, proj: dict) -> dict:
        """按项目解析芯片；项目可写 ``pack: vendor/platform`` **固定包**。

        固定包解析**只读**包目录、不改 ``active.json``：不同项目可以并行
        使用不同适配包（STM32 与 ESP32 不必互相切换）。
        """
        ref = proj.get("chip")
        pinned = str(proj.get("pack") or "").strip()
        if pinned:
            rec = self.packs.get(pinned)
            if not rec:
                raise ElabError(
                    f"项目 {proj.get('name')} 固定包未安装：{pinned}"
                    f"（先执行 elab pkg install {pinned}）")
            cache = self._pinned_chips.get(pinned)
            if cache is None:
                cache = self._load_chips([rec])
                self._pinned_chips[pinned] = cache
            chip = cache.get(ref) or cache.get(str(ref).split("/")[-1])
            if chip:
                return chip
            known = ", ".join(sorted(k for k in cache if "/" in k)) or "(无)"
            raise ElabError(
                f"包 {pinned} 内没有芯片 {ref}（包内芯片：{known}）")
        if ref not in self.chips:
            if not self.packs:
                hint = ("—— 尚未安装任何芯片适配包；先 "
                        "`elab pkg search` 然后 `elab pkg install <vendor>/<platform>`")
            else:
                known = ", ".join(sorted(k for k in self.chips if "/" in k)) or "(无)"
                hint = f"（已安装包中的芯片：{known}）"
            raise ElabError(f"项目 {proj.get('name')} 引用了未知芯片：{ref}{hint}")
        return self.chips[ref]


class Host:
    """L0 主机层的**已解析视图**：把 elab.host.yaml 变成可直接用的路径。

    每个主机路径都可以被环境变量覆盖（DSH 插件的「一键环境包」用这条缝：
    工具解到 ``~/.dsh/elab/env/<id>`` 后只注入环境变量，**不改**仓库里的
    ``elab.host.yaml``）：

    | 变量 | 覆盖 |
    |---|---|
    | ``ELAB_ARM_GCC_ROOT`` | ``toolchains.arm-none-eabi.root``（bin 随之派生） |
    | ``ELAB_CMAKE`` | ``tools.cmake.path`` |
    | ``ELAB_NINJA`` | ``tools.ninja.path`` |
    | ``ELAB_OPENOCD`` | ``tools.openocd.path`` |
    | ``ELAB_OPENOCD_SCRIPTS`` | ``tools.openocd.scripts`` |
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # ★ 解析视图：主机路径几乎都是 `${ELAB_ROOT}/...` 形式（Kharon 的
        #   .deps/venv、.deps/tools 全部如此）。不在这里展开，`host.cmake`
        #   这类属性就会把字面量漏给子进程。`raw` 保留未解析原样。
        self.raw = cfg.host
        self.data = cfg.resolve_deep(cfg.host, cfg.root_ctx())
        self._resolve_env()

    def _resolve_env(self):
        os_name = (self.data.get("host") or {}).get("os", "auto")
        if os_name in (None, "", "auto"):
            if os.name == "nt":
                os_name = "windows"
            else:
                os_name = "macos" if sys.platform == "darwin" else "linux"
        self.os = os_name
        self.is_windows = self.os == "windows"

        ext = (self.data.get("host") or {}).get("exec_ext", "auto")
        if ext in (None, "", "auto"):
            ext = ".exe" if self.is_windows else ""
        self.exec_ext = ext

    # ── 工具链 ───────────────────────────────────────────────────
    def toolchain(self, name: str) -> dict:
        return (self.data.get("toolchains") or {}).get(name) or {}

    # ── 通用工具访问器（T10）─────────────────────────────────────
    def tool_spec(self, key: str) -> dict:
        """``tools.<key>`` 原样视图（``{path, scripts, ...}``）；未声明返回 ``{}``。

        为什么要有通用口：一条链对应一种架构的调试器（Zephyr 的 ARM / RISC-V /
        Xtensa 三套 gdb 与 addr2line）。若给每个厂商加一个 Host 属性，主机层会
        随着芯片数量线性膨胀；芯片侧只写**键名**（``debug.gdb_key``），取值归这里。
        """
        if not key:
            return {}
        node = (self.data.get("tools") or {}).get(str(key))
        return node if isinstance(node, dict) else {}

    def tool(self, key: str, field: str = "path") -> str:
        """``tools.<key>.<field>`` → 已解析的绝对路径（未声明返回空串）。

        环境变量覆盖沿用既有约定：``ELAB_TOOL_<KEY大写>`` 优先（插件注入用）。
        """
        if not key:
            return ""
        env = _env_path("ELAB_TOOL_" + str(key).upper().replace("-", "_"))
        if field == "path" and env:
            return to_fwd(env)
        return to_fwd(self.tool_spec(key).get(field, "") or "")

    @property
    def gcc(self) -> dict:
        return self.toolchain("arm-none-eabi")

    @property
    def gcc_root(self) -> str:
        root = _env_path("ELAB_ARM_GCC_ROOT") or self.gcc.get("root", "")
        return to_fwd(root)

    @property
    def gcc_bin(self) -> str:
        return f"{self.gcc_root}/bin"

    def _gb(self, tool: str) -> str:
        return f"{self.gcc_bin}/{tool}{self.exec_ext}"

    @property
    def cc(self) -> str:
        return self._gb("arm-none-eabi-gcc")

    @property
    def objcopy(self) -> str:
        return self._gb("arm-none-eabi-objcopy")

    @property
    def size(self) -> str:
        return self._gb("arm-none-eabi-size")

    @property
    def gdb(self) -> str:
        spec = (self.data.get("tools") or {}).get("gdb") or {}
        # ★ 只在**没有显式 path**时才默认 arm-none-eabi：Zephyr 主机会直接写
        #   `gdb: { path: .../arm-zephyr-eabi-gdb.exe }`（SDK 的 gdb，不是 classic 的），
        #   旧逻辑无视这个 path、拼出 `''/arm-none-eabi-gdb.exe` → doctor 假告警。
        src = spec.get("from", "" if spec.get("path") else "arm-none-eabi")
        if src == "arm-none-eabi":
            return self._gb("arm-none-eabi-gdb")
        return spec.get("path", "")

    @property
    def gdb_riscv(self) -> str:
        """RISC-V 调试器（ESP-IDF 自带 riscv32-esp-elf-gdb）。"""
        return (_env_path("ELAB_GDB_RISCV")
                or ((self.data.get("tools") or {}).get("gdb_riscv") or {}).get("path", ""))

    # ── ESP-IDF（工程层工具链；主机侧只声明入口路径）────────────────
    @property
    def idf(self) -> dict:
        return (self.data.get("tools") or {}).get("idf") or {}

    @property
    def idf_python(self) -> str:
        return _env_path("ELAB_IDF_PYTHON") or self.idf.get("python", "")

    @property
    def idf_root(self) -> str:
        return _env_path("ELAB_IDF_PATH") or self.idf.get("idf_path", "")

    @property
    def idf_tools_path(self) -> str:
        return _env_path("ELAB_IDF_TOOLS_PATH") or self.idf.get("tools_path", "")

    @property
    def idf_version(self) -> str:
        return _env_path("ELAB_IDF_VERSION") or self.idf.get("version", "")

    @property
    def idf_venv(self) -> str:
        """ESP-IDF 的 Python venv 根（``IDF_PYTHON_ENV_PATH``）。

        idf.py 会逐字符比较它与 ``sys.prefix``，必须保留宿主原生分隔符
        （正斜杠会被判成"不是同一个 venv"）。
        """
        v = _env_path("ELAB_IDF_VENV") or self.idf.get("venv", "")
        if v:
            return v
        py = self.idf_python
        return str(Path(py).parent.parent) if py else ""

    # ── 主机工具 ─────────────────────────────────────────────────
    @property
    def cmake(self) -> str:
        return (_env_path("ELAB_CMAKE")
                or ((self.data.get("tools") or {}).get("cmake") or {}).get("path", "auto"))

    @property
    def cmake_min(self) -> str:
        return ((self.data.get("tools") or {}).get("cmake") or {}).get("min", "3.15")

    @property
    def ninja(self) -> str:
        return (_env_path("ELAB_NINJA")
                or ((self.data.get("tools") or {}).get("ninja") or {}).get("path", ""))

    @property
    def openocd(self) -> str:
        return (_env_path("ELAB_OPENOCD")
                or ((self.data.get("tools") or {}).get("openocd") or {}).get("path", ""))

    @property
    def openocd_scripts(self) -> str:
        return (_env_path("ELAB_OPENOCD_SCRIPTS")
                or ((self.data.get("tools") or {}).get("openocd") or {}).get("scripts", ""))

    @property
    def openocd_esp32(self) -> str:
        """ESP-IDF 自带 OpenOCD（与 ARM 那份是两套脚本，不能混用）。"""
        return (_env_path("ELAB_OPENOCD_ESP32")
                or ((self.data.get("tools") or {}).get("openocd_esp32") or {}).get("path", ""))

    @property
    def openocd_esp32_scripts(self) -> str:
        return (_env_path("ELAB_OPENOCD_ESP32_SCRIPTS")
                or ((self.data.get("tools") or {}).get("openocd_esp32") or {}).get("scripts", ""))

    @property
    def probes(self) -> dict:
        return (self.data.get("probes") or {}).get("list") or {}

    @property
    def default_probe(self) -> str:
        """新接入工程默认使用哪个探针（L0 的 ``probes.default``）。"""
        return str((self.data.get("probes") or {}).get("default") or "").strip()

    def probe(self, name: str) -> dict:
        return self.probes.get(name) or {}

    @property
    def serial(self) -> dict:
        return self.data.get("serial") or {}

    # ── SVD ─────────────────────────────────────────────────────
    @property
    def svd_roots(self) -> list[str]:
        """SVD 搜索根（L0 声明）。chip.yaml 只写文件名，目录在这里。"""
        return [to_fwd(p) for p in (self.data.get("svd_roots") or [])]

    def resolve_svd(self, name):
        """按文件名在 svd_roots 下查找；也接受 chip.yaml 直接给绝对路径。"""
        if not name:
            return None
        p = Path(str(name))
        if p.is_absolute():
            return to_fwd(p) if p.exists() else None
        for root in self.svd_roots:
            cand = Path(root) / str(name)
            if cand.exists():
                return to_fwd(cand)
        return None

    # ── PATH ─────────────────────────────────────────────────────
    def path_prepend(self) -> list[str]:
        """按 D1 决策，返回需要前置进 PATH 的目录（原生路径形式）。"""
        dirs = []
        if self.gcc.get("prepend_path", True) and self.gcc_root:
            dirs.append(self.gcc_bin)
        return dirs

    def build_env(self) -> dict:
        """构造子进程环境：前置工具链 bin，并注入 ELAB_* 事实。"""
        env = dict(os.environ)
        prepend = [to_os_path(d) for d in self.path_prepend() if d]
        if prepend:
            env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
        env["ELAB_ARM_GCC_ROOT"] = self.gcc_root
        env["ELAB_ROOT"] = to_fwd(self.cfg.ide_root)
        env["IDE_ROOT"] = to_fwd(self.cfg.ide_root)
        env["ZEPHYR_ROOT"] = to_fwd(self.cfg.zephyr_root)
        return env
