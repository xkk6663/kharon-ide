"""elab.doctor —— 环境与"两源漂移"体检。

三层体检：
  1. **L0 主机层**：所有声明路径是否可达；工具链版本是否与声明一致。
  2. **交叉引用**：项目→芯片、项目→探针、芯片→openocd cfg 是否都能解析。
  3. **两源漂移**（本设计的核心价值）：
     对自带 CMake 的工程，芯片参数在"工程里"和"chip.yaml 里"各有一份。
     doctor 拿 ``verify.expect_*`` 去比对**工程实际**的
     链接脚本内存布局 / 编译 flags / 预定义宏，不一致即报 ERROR。
     ``--deep`` 会真跑一次 configure（到 ``.work/<name>.doctor``）后
     从 compile_commands.json 取"实际编译命令"作证据。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from elab.platform._winproc import proc_kwargs
from elab.core.config import Config, ElabError, Host, to_fwd
from elab.core import _yaml
from elab.core import paths as core_paths
from elab.platform import ota_layout as ota_layout
from elab.workflow.plan import Plan, plan_for

OK, WARN, ERROR, INFO = "ok", "warn", "error", "info"
_ICON = {OK: "OK  ", WARN: "WARN", ERROR: "FAIL", INFO: "info"}


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, level: str, code: str, msg: str):
        self.items.append((level, code, msg))
        return self

    def ok(self, code, msg):
        return self.add(OK, code, msg)

    def warn(self, code, msg):
        return self.add(WARN, code, msg)

    def fail(self, code, msg):
        return self.add(ERROR, code, msg)

    def info(self, code, msg):
        return self.add(INFO, code, msg)

    @property
    def errors(self):
        return [i for i in self.items if i[0] == ERROR]

    @property
    def warns(self):
        return [i for i in self.items if i[0] == WARN]

    @property
    def passed(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = []
        for level, code, msg in self.items:
            lines.append(f"  [{_ICON[level]}] {code:<22} {msg}")
        return "\n".join(lines)


# ── 链接脚本内存布局 ──────────────────────────────────────────────
def parse_ld_memory(path) -> dict:
    """解析 ld 的 ``MEMORY { ... }`` 段 → ``{region: (origin, length)}``。"""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    m = re.search(r"MEMORY\s*\{(.*?)\}", txt, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        mm = re.match(
            r"\s*([A-Za-z0-9_]+)\s*(\([^)]*\))?\s*:\s*"
            r"ORIGIN\s*=\s*([^,\s]+)\s*,?\s*LENGTH\s*=\s*([^,\s]+)",
            line,
        )
        if mm:
            out[mm.group(1)] = (_parse_num(mm.group(3)), _parse_num(mm.group(4)))
    return out


def _parse_num(s: str) -> int:
    s = s.strip().rstrip(",").strip()
    mul = 1
    if s and s[-1] in "KkMm":
        mul = 1024 if s[-1] in "Kk" else 1024 * 1024
        s = s[:-1]
    try:
        return int(s, 0) * mul
    except ValueError:
        return -1


def _fmt(n: int) -> str:
    return f"0x{n:X} ({n})"


# ── 单项检查 ──────────────────────────────────────────────────────
def check_host(cfg: Config, rep: Report, host: Host, native_needed: bool = True,
               strict_host: bool = False):
    """主机事实源体检。

    ``native_needed``：范围内是否存在**内核接管工具链**（builtin provider）的工程。
    ★ 全部工程都委托给工具链 provider 时（如 Kharon 全 zephyr），ARM 交叉工具链
      与本机无关 —— 检查只会产生"声明为空"的假 FAIL（数据驱动，无厂商分支）。

    ``strict_host``（``elab doctor --strict-host``）：未显式选择 profile、
    或 ``${host:...}`` 引用的键在 profile + host.local 中都不存在时，直接判失败。
    """
    sel = cfg.host_selection
    if sel["implicit"]:
        msg = (f"未显式选择主机 profile（ELAB_HOST），当前按 {sel['origin']} 使用 "
               f"{sel['path'].name}；修复：设置 ELAB_HOST=classic 或 ELAB_HOST=kharon")
        (rep.fail if strict_host else rep.warn)("host.selection", msg)
    else:
        rep.info("host.profile",
                 f"{sel['profile'] or 'legacy-file'} ← {to_fwd(sel['path'])}")
    if cfg.host_local:
        rep.info("host.local", "host.local.yaml（本机绝对路径覆盖已合并）")

    rep.info("host.yaml", to_fwd(cfg.host_path))

    if not native_needed:
        rep.info("toolchain.native",
                 "范围内工程全部委托 provider，内核不接管交叉工具链 → 跳过 ARM 工具链检查")
        gcc_root = host.gcc_root
        if gcc_root and Path(gcc_root).is_dir():
            rep.ok("toolchain.root", f"{gcc_root}  (声明版本 {host.gcc.get('version', '?')})")
        _check_shared_tools(cfg, rep, host)
        return

    gcc_root = host.gcc_root
    if gcc_root and Path(gcc_root).is_dir():
        rep.ok("toolchain.root", f"{gcc_root}  (声明版本 {host.gcc.get('version', '?')})")
    else:
        rep.fail("toolchain.root", f"工具链根目录不存在：{gcc_root!r}")

    if Path(host.cc).exists():
        rep.ok("toolchain.cc", Path(host.cc).name)
    else:
        rep.fail("toolchain.cc", f"找不到交叉编译器：{host.cc}")

    # 版本漂移：声明值 vs 实测值
    declared = str(host.gcc.get("version", "")).strip()
    if declared and Path(host.cc).exists():
        try:
            out = subprocess.run(
                [host.cc, "--version"], capture_output=True, text=True, timeout=20,
                **proc_kwargs(),
            ).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            actual = m.group(1) if m else "?"
            if actual == declared:
                rep.ok("toolchain.version", f"{actual} == 声明值")
            else:
                rep.fail(
                    "toolchain.version",
                    f"实测 {actual} ≠ 声明 {declared}（elab.host.yaml 需更新）",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            rep.warn("toolchain.version", f"无法探测版本：{exc}")

    _check_shared_tools(cfg, rep, host)


def _check_shared_tools(cfg: Config, rep: Report, host: Host):
    """ninja / cmake / openocd / 探针 cfg / gdb —— classic 与 provider 共用的部分。"""
    if host.ninja and Path(host.ninja).exists():
        rep.ok("tools.ninja", to_fwd(host.ninja))
    else:
        rep.fail("tools.ninja", f"ninja 不存在：{host.ninja!r}")

    cm = host.cmake
    if cm in ("", "auto", None):
        found = _which("cmake")
        if found:
            rep.ok("tools.cmake", f"{found} (auto，要求 ≥ {host.cmake_min})")
        else:
            rep.fail("tools.cmake", "PATH 上找不到 cmake")
    else:
        rep.ok("tools.cmake", cm)

    if host.openocd and Path(host.openocd).exists():
        rep.ok("tools.openocd", to_fwd(host.openocd))
    else:
        rep.warn("tools.openocd", f"openocd 不存在：{host.openocd!r}（build 不受影响）")

    if host.openocd_scripts and Path(host.openocd_scripts).is_dir():
        rep.ok("openocd.scripts", to_fwd(host.openocd_scripts))
    else:
        rep.warn("openocd.scripts", f"openocd scripts 目录不存在：{host.openocd_scripts!r}")

    # 探针的 interface cfg —— 属于 L0，与芯片正交（同一个 DAP-Link 可驱动多颗芯片）
    scripts = host.openocd_scripts
    if scripts and Path(scripts).is_dir():
        for pname, probe in (host.probes or {}).items():
            ic = probe.get("interface_cfg")
            if not ic:
                continue
            if Path(scripts, ic).exists():
                rep.ok(f"probe.{pname}", f"{ic}  (backend={probe.get('backend')})")
            else:
                rep.warn(f"probe.{pname}", f"interface cfg 不存在：{ic}")

    if host.gdb and Path(host.gdb).exists():
        rep.ok("tools.gdb", Path(host.gdb).name)
    else:
        rep.warn("tools.gdb", f"ARM gdb 不存在：{host.gdb!r}（debug 不可用）")


def _which(name: str):
    from shutil import which

    return which(name)


def check_chip(rep: Report, cfg: Config, host: Host, chip: dict):
    cid = chip.get("id", "?")
    core = chip.get("core") or {}
    cpu = core.get("cpu", "")
    fpu = core.get("fpu", "")
    target = str(chip.get("target") or "")      # zephyr: `west -b` 的板名
    runner = str((chip.get("flash") or {}).get("runner") or "")
    dbg = chip.get("debug") or {}

    # ★ board-as-chip（zephyr 包红线）：不写 memory / core / compiler_defines，
    #   架构与编译事实由 provider 承载 —— 有 target 键即 zephyr 口径，不要求 ARM
    #   的 core.cpu（classic 芯片没有 target，仍走原判定）。
    if target:
        rep.ok(f"chip.{cid}.target", f"{target}  (west -b 板名)")
    if cpu:
        rep.ok(f"chip.{cid}.cpu", cpu)
    elif target:
        rep.info(f"chip.{cid}.cpu",
                 "未声明 core.cpu（board-as-chip：编译事实由 provider 承载，core_name 仅 UI 展示）")
    else:
        rep.fail(f"chip.{cid}.cpu", "chip.yaml 缺少 core.cpu")

    if not fpu or fpu == "none":
        rep.info(f"chip.{cid}.fpu", f"{fpu or '(未声明)'} → 不产出 -mfloat-abi")
    else:
        rep.info(f"chip.{cid}.fpu", f"{fpu} → -mfloat-abi={fpu}")

    # target cfg 属于【芯片】；interface cfg 属于【探针】（见 check_host）。
    # ★ zephyr 非 openocd runner（esp/dfu/jlink）烧录走 west flash，openocd cfg
    #   无关 —— 不检查、不置灰；整份 cfg 通道见 openocd_config。
    if target and runner and runner != "openocd":
        rep.info(f"chip.{cid}.openocd",
                 f"flash.runner={runner} → 烧录交回 west flash，openocd cfg 不适用")
        oc_cfg = dbg.get("openocd_config")
        if oc_cfg:
            found = [Path(cfg.root) / oc_cfg, Path(cfg.zephyr_root) / oc_cfg]
            if any(p.is_file() for p in found):
                rep.ok(f"chip.{cid}.openocd_config", oc_cfg)
            else:
                rep.warn(f"chip.{cid}.openocd_config", f"整份 cfg 不存在：{oc_cfg}")
    else:
        # ESP-IDF 芯片用 IDF 自带 OpenOCD 的脚本目录（与 ARM 那份不通）
        backend = str(dbg.get("backend") or "")
        scripts = (host.openocd_esp32_scripts if backend in ("esp-idf", "idf")
                   else host.openocd_scripts)
        oc_cfg = dbg.get("openocd_config")
        if oc_cfg:
            # ★ 整份 cfg 通道：interface + target 都写在这份文件里，芯片侧**没有**
            #   `openocd_target` 这个键。若仍按 target 判，会稳定报一句假告警
            #   （f411_breath 实测：明明能烧，doctor 说"flash/debug 不可用"）。
            found = [Path(cfg.root) / oc_cfg, Path(cfg.zephyr_root) / oc_cfg]
            if any(p.is_file() for p in found):
                rep.ok(f"chip.{cid}.openocd_config", oc_cfg)
            else:
                rep.warn(f"chip.{cid}.openocd_config", f"整份 cfg 不存在：{oc_cfg}")
        elif scripts and Path(scripts).is_dir():
            tgt = dbg.get("openocd_target")
            if not tgt:
                # ★ 键名用 openocd_target 而不是 target：同一条输出里已经有
                #   `chip.<id>.target`（west -b 的板名）了，两个同名 code 会让人以为
                #   "板名没声明"，实际缺的是 openocd 的 target 脚本。
                rep.warn(f"chip.{cid}.openocd_target", "未声明（flash/debug 不可用）")
            elif Path(scripts, tgt).exists():
                rep.ok(f"chip.{cid}.target", tgt)
            else:
                rep.warn(f"chip.{cid}.target", f"openocd cfg 不存在：{tgt}")
    svd = dbg.get("svd")
    if not svd:
        rep.info(f"chip.{cid}.svd", "未声明（影响 IDE 寄存器视图，不影响编译）")
    else:
        found = host.resolve_svd(svd)
        if found:
            rep.ok(f"chip.{cid}.svd", f"{svd} → {found}")
        else:
            rep.warn(f"chip.{cid}.svd",
                     f"未找到 {svd}（搜索根见 elab.host.yaml: svd_roots）")


def check_project(rep: Report, cfg: Config, host: Host, plan: Plan):
    name = plan.name
    proj = plan.proj
    rep.info(f"proj.{name}", f"archetype={proj.get('archetype', '?')}  chip={proj.get('chip')}")

    def _delegated(step: str) -> str:
        """``steps.<step> == tool:<name>`` → 返回工具名；否则空串。"""
        spec = (proj.get("steps") or {}).get(step)
        if isinstance(spec, str) and spec.startswith("tool:"):
            return spec[len("tool:"):].strip()
        return ""

    # 外部步骤（provider 或内联 tool:）都算"编译/烧录由项目工具链接管"：
    # 内核不再检查 ARM toolchain/链接脚本/探针（provider 的 host 前提由 check_toolchains 负责）。
    try:
        from elab.platform import provider as provider_mod
        tc = provider_mod.resolve(cfg, name)
    except ElabError:
        tc = None

    def _external(step: str) -> bool:
        decl = tc.steps.get(step) if tc else None
        return bool(decl and decl.kind == "external")

    def _external_source(step: str) -> str:
        decl = tc.steps.get(step) if tc else None
        if not decl or decl.kind != "external":
            return ""
        return (f"tools.{decl.tool_name}" if decl.provider == "project"
                else f"provider {tc.provider.name}@{tc.provider.version}")

    if Path(plan.source_dir).is_dir():
        rep.ok(f"proj.{name}.root", plan.source_dir)
    else:
        rep.fail(f"proj.{name}.root", f"业务工程目录不存在：{plan.source_dir}")

    src = to_fwd(proj.get("build", {}).get("source", ""))
    if src and Path(src).exists():
        rep.ok(f"proj.{name}.source", src)
    else:
        rep.warn(f"proj.{name}.source", f"构建入口不存在：{src!r}")

    build_tool = _delegated("build")
    build_ext = _external("build")
    if build_tool or build_ext:
        # ★ 委托/provider 模式：编译由项目工具链执行，内核不检查 ARM toolchain/ld。
        src_s = f"tools.{build_tool}" if build_tool else _external_source("build")
        rep.info(f"proj.{name}.build_tool",
                 f"编译委托给 {src_s}（内核不接管工具链/链接脚本）")
    else:
        for label, path in (("toolchain_file", plan.toolchain_file),
                            ("project_include", plan.project_include)):
            if path and Path(path).exists():
                rep.ok(f"proj.{name}.{label}", Path(path).name)
            else:
                rep.fail(f"proj.{name}.{label}", f"文件不存在：{path!r}")

        if plan.linker_script and Path(plan.linker_script).exists():
            rep.ok(f"proj.{name}.ld", Path(plan.linker_script).name)
        else:
            rep.fail(f"proj.{name}.ld", f"链接脚本不存在：{plan.linker_script!r}")

    # 探针：烧录/校验已委托给项目工具时不需要（如 esptool 走 USB 串口）
    probe_tool = _delegated("flash") or _delegated("debug_verify")
    flash_ext = _external("flash") or _external("debug_verify")
    if probe_tool or flash_ext:
        src_s = f"tools.{probe_tool}" if probe_tool else _external_source("flash")
        rep.info(f"proj.{name}.probe",
                 f"烧录/校验委托给 {src_s}（不需要探针）")
    elif plan.probe:
        if host.probe(plan.probe):
            rep.ok(f"proj.{name}.probe", plan.probe)
        else:
            rep.warn(f"proj.{name}.probe", f"探针 {plan.probe!r} 未在 elab.host.yaml 声明")
    else:
        rep.warn(f"proj.{name}.probe", "未指定探针")


def check_drift(rep: Report, plan: Plan):
    """两源漂移：工程自带 ld 的 MEMORY vs chip.yaml 的内存声明。

    单镜像项目：ld region 必须**逐字节等于**芯片容量（旧行为，不变）。
    OTA 双镜像项目（flash.images 已声明，C33）：改为**镜像级口径** ——
      ① 每个镜像的 ld region 必须落在芯片物理范围内（BOOT 18K / APP 44K 都
         ≠ 芯片 64K，逐字节相等口径必然误报）；
      ② 各镜像在 **flash** 上的分区互不重叠（RAM 两个镜像都声明全量是 OTA
         惯例——boot/app 不同时运行，不参与互斥检查）；
      ③ bin 镜像的 address 必须等于其 ld 的 flash 区起点（烧错位 = 跑飞）。
    """
    # ★ OTA 分区表三方对账（方案 A/M1）：chips yaml ota_layout ↔
    #   flash.images 烧录地址 ↔ 各镜像 ld 的 FLASH ORIGIN。
    #   刻意放在 expect_memory 早退之前 —— 它只依赖 ota_layout 自身，
    #   不该因为芯片没写 verify.expect_memory 而被跳过。
    if plan.ota_layout:
        phys = (plan.chip.get("memory") or {}).get("flash") or None
        physical = None
        if isinstance(phys, dict) and phys.get("origin") is not None and phys.get("length") is not None:
            physical = (_parse_num(str(phys["origin"])), _parse_num(str(phys["length"])))
        problems = ota_layout_drift(plan.ota_layout, plan.flash_images, physical)
        if problems:
            rep.fail(f"drift.{plan.name}.ota-layout", "OTA 分区表漂移：" + "; ".join(problems))
        else:
            rep.ok(
                f"drift.{plan.name}.ota-layout",
                f"ota_layout ↔ 烧录地址 ↔ ld 三方一致（{len(plan.flash_images)} 镜像）",
            )

    chip = plan.chip
    verify = chip.get("verify") or {}
    expect = verify.get("expect_memory")
    if not expect:
        rep.info(f"drift.{plan.name}.memory", "chip.yaml 未声明 verify.expect_memory，跳过")
        return
    if not plan.linker_script or not Path(plan.linker_script).exists():
        rep.warn(f"drift.{plan.name}.memory", "链接脚本不可读，跳过内存比对")
        return

    if plan.flash_images:
        _check_drift_images(rep, plan, expect)
    else:
        _check_drift_single(rep, plan, expect)

    # 通用约定守卫：gcc.cmake 必须带 .elf 后缀（约束 C5）
    tc = Path(plan.toolchain_file) if plan.toolchain_file else None
    if tc and tc.exists():
        txt = tc.read_text(encoding="utf-8", errors="ignore")
        if "CMAKE_EXECUTABLE_SUFFIX_C" in txt and ".elf" in txt:
            rep.ok(f"convention.{plan.name}.elf", "通用工具链带 .elf 后缀约定 (C5)")
        else:
            rep.warn(f"convention.{plan.name}.elf",
                     "通用工具链缺少 .elf 后缀约定 → A 类工程产物会静默改名 (C5)")


def _chip_physical(plan: Plan) -> dict:
    """芯片物理内存范围：优先 chip.memory（含 origin），回退 expect_memory 标量。"""
    out: dict[str, tuple[int | None, int]] = {}
    for k, v in (plan.chip.get("memory") or {}).items():
        if isinstance(v, dict) and v.get("length") is not None:
            origin = _parse_num(str(v["origin"])) if v.get("origin") is not None else None
            out[k] = (origin, _parse_num(str(v["length"])))
    if not out:
        for k, v in ((plan.chip.get("verify") or {}).get("expect_memory") or {}).items():
            if isinstance(v, dict):
                origin = _parse_num(str(v.get("origin"))) if v.get("origin") is not None else None
                out[k] = (origin, _parse_num(str(v.get("length"))))
            else:
                out[k] = (None, _parse_num(str(v)))
    return out


def ota_layout_drift(layout: dict, flash_images: list[dict],
                     physical: tuple[int, int] | None) -> list[str]:
    """OTA 分区表三方对账（★ 方案 A/M1，纯函数；返回问题列表，空 = 一致）。

    ① 每个分区 ⊆ 芯片物理 flash；
    ② flash.images 每个带地址的镜像，地址必须命中【恰好一个】分区起点；
    ③ 命中分区的起点必须与该镜像 ld 的 FLASH ORIGIN 一致（跨文件漂移）。
    分区互斥在 plan 归一化（normalize_ota_layout）已 fail-fast，此处不重复。
    """
    problems: list[str] = []
    regions = {k: (layout[k]["addr"], layout[k]["size"]) for k in ota_layout.REGION_KEYS}

    # ★ 双槽校验（方案 §3.1）：槽互斥/覆盖性已在 plan 归一化 fail-fast，
    #   这里补"槽 ⊆ 物理 flash"与"出厂 app 镜像必须落槽 A 起点"。
    slots = layout.get("slots") or []
    if slots:
        for i, s in enumerate(slots):
            if physical is not None:
                p_origin, p_len = physical
                if not (p_origin <= s["addr"] and s["addr"] + s["size"] <= p_origin + p_len):
                    problems.append(
                        f"slots[{i}] {s['addr']:#x}+{s['size']:#x} 越界物理 flash "
                        f"{p_origin:#x}+{p_len:#x}"
                    )

    if physical is not None:
        p_origin, p_len = physical
        for key, (addr, size) in regions.items():
            if not (p_origin <= addr and addr + size <= p_origin + p_len):
                problems.append(
                    f"{key} {addr:#x}+{size:#x} 越界物理 flash {p_origin:#x}+{p_len:#x}"
                )

    for idx, img in enumerate(flash_images):
        raw = img.get("address")
        if not raw:
            continue  # elf 无显式地址 → 自带地址，无从对账（合法）
        addr = int(raw, 16) if isinstance(raw, str) else int(raw)
        hit = [k for k, (ra, _rs) in regions.items() if ra == addr]
        if len(hit) != 1:
            problems.append(
                f"flash.images[{idx}] 地址 {addr:#x} 未命中任何分区起点"
                f"（命中 {'/'.join(hit) if hit else '无'}）"
            )
            continue
        key = hit[0]
        # ★ 双槽（§3.1）：app 镜像（命中 app 区起点 == 槽A）必须是烧槽 A——出厂固件
        #   永远落 ACTIVE 槽；槽 B 只由 boot 的升级流程写入，flash.images 不许碰。
        if key == "app" and slots and addr != slots[0]["addr"]:
            problems.append(
                f"flash.images[{idx}] 地址 {addr:#x} ≠ 槽A起点 {slots[0]['addr']:#x}"
                "（出厂镜像必须烧槽 A）"
            )
        ld = img.get("ld")
        if not ld:
            continue
        if not Path(ld).exists():
            problems.append(f"flash.images[{idx}] ld 不可读：{ld}")
            continue
        regs = parse_ld_memory(ld)
        flash_key = next((r for r in regs if "FLASH" in r.upper()), None) if regs else None
        if flash_key is None:
            problems.append(f"flash.images[{idx}] 未能从 {Path(ld).name} 解析 FLASH 段")
        elif regs[flash_key][0] != addr:
            problems.append(
                f"flash.images[{idx}]（{Path(ld).name}）ld FLASH ORIGIN "
                f"{regs[flash_key][0]:#x} ≠ 命中分区 {key} 起点 {addr:#x}"
            )
    return problems


def _check_drift_images(rep: Report, plan: Plan, expect):
    """镜像级内存对账（C33）：⊆ 物理 + flash 互斥 + bin 地址对齐 ld 起点。"""
    physical = _chip_physical(plan)
    good, details = True, []

    segs: dict[str, list[tuple[str, int, int]]] = {}   # chip_key -> [(镜像名, origin, len)]
    for idx, img in enumerate(plan.flash_images):
        ld = img.get("ld") or plan.linker_script
        tag = f"#{idx}({Path(ld).name})" if img.get("ld") else f"#{idx}({Path(plan.linker_script).name})"
        if not ld or not Path(ld).exists():
            good = False
            details.append(f"{tag}: ld 不可读")
            continue
        regions = parse_ld_memory(ld)
        if not regions:
            good = False
            details.append(f"{tag}: 未能解析 MEMORY 段")
            continue
        for key in expect:
            match = next((r for r in regions if key.upper() in r.upper()), None)
            if match is None:
                good = False
                details.append(f"{tag}.{key}: ld 未声明该区")
                continue
            origin, length = regions[match]
            phys = physical.get(key)
            if phys:
                p_origin, p_len = phys
                if p_origin is not None and not (
                        p_origin <= origin and origin + length <= p_origin + p_len):
                    good = False
                    details.append(
                        f"{tag}.{key}: {_fmt(length)}@{origin:#x} 越界 "
                        f"(物理 {_fmt(p_len)}@{p_origin:#x})")
                elif p_origin is None and p_len and length > p_len:
                    good = False
                    details.append(f"{tag}.{key}: {_fmt(length)} > 物理 {_fmt(p_len)}")
                if "flash" in key.lower():
                    segs.setdefault(key, []).append((tag, origin, length))
            details.append(f"{tag}.{key}={_fmt(length)}@{origin:#x}")

        # ③ bin 镜像地址必须对齐其 ld 的 flash 起点
        if img["format"] == "bin" and img.get("address"):
            m = next((r for r in regions if "FLASH" in r.upper()), None)
            if m and int(img["address"], 16) != regions[m][0]:
                good = False
                details.append(
                    f"{tag}: bin address {img['address']} ≠ {m} 起点 "
                    f"{regions[m][0]:#x}（烧错位 = 跑飞）")

    # ② flash 分区互斥（RAM 不查——boot/app 不同时运行，各自声明全量是惯例）
    for key, lst in segs.items():
        ordered = sorted(lst, key=lambda t: t[1])
        for a, b in zip(ordered, ordered[1:]):
            if a[1] + a[2] > b[1]:
                good = False
                details.append(
                    f"flash 分区重叠：{a[0]}[{a[1]:#x}~{a[1]+a[2]:#x}] ∩ "
                    f"{b[0]}[{b[1]:#x}~{b[1]+b[2]:#x}]")

    if good:
        rep.ok(f"drift.{plan.name}.memory",
               f"双镜像布局合法（{len(plan.flash_images)} 镜像 ⊆ 芯片物理，flash 分区互斥）")
    else:
        rep.fail(f"drift.{plan.name}.memory", "镜像内存布局漂移：" + "; ".join(details))


def _check_drift_single(rep: Report, plan: Plan, expect):
    """单镜像口径（旧行为）：ld region 逐字节等于芯片容量。"""
    regions = parse_ld_memory(plan.linker_script)
    if not regions:
        rep.warn(f"drift.{plan.name}.memory", f"未能从 {Path(plan.linker_script).name} 解析 MEMORY 段")
        return

    good = True
    details = []
    for key, exp in expect.items():
        match = next((r for r in regions if key.upper() in r.upper()), None)
        if match is None:
            good = False
            details.append(f"{key}=未找到")
            continue
        origin, length = regions[match]
        if isinstance(exp, dict):
            exp_origin, exp_len = _parse_num(str(exp.get("origin"))), _parse_num(str(exp.get("length")))
        else:
            exp_origin, exp_len = None, _parse_num(str(exp))
        if length != exp_len or (exp_origin is not None and origin != exp_origin):
            good = False
            details.append(f"{key}: ld={_fmt(length)} ≠ chip={_fmt(exp_len)}")
        else:
            details.append(f"{key}={_fmt(length)}")
    if good:
        rep.ok(f"drift.{plan.name}.memory", "ld MEMORY 与 chip.yaml 一致 [" + ", ".join(details) + "]")
    else:
        rep.fail(f"drift.{plan.name}.memory", "内存布局漂移：" + "; ".join(details))


def deep_check(rep: Report, plan: Plan) -> dict:
    """真跑一次 configure，从 compile_commands.json 取实际编译命令作证据。"""
    scratch = plan.work_dir + ".doctor"
    Path(scratch).parent.mkdir(parents=True, exist_ok=True)

    # 复用 plan 的 -D，只替换 -B
    cmd = list(plan.configure_cmd())
    b = cmd.index("-B")
    cmd[b + 1] = scratch

    env = plan.host.build_env()
    try:
        proc = subprocess.run(
            cmd, cwd=str(plan.cfg.root), env=env,
            capture_output=True, text=True, timeout=300, **proc_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        rep.fail(f"deep.{plan.name}", f"configure 无法启动：{exc}")
        return {"configure_ok": False}
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        rep.fail(f"deep.{plan.name}", f"configure 失败 (rc={proc.returncode})\n{tail}")
        return {"configure_ok": False}
    rep.ok(f"deep.{plan.name}", f"configure 通过（证据目录 {Path(scratch).name}）")

    cc_path = Path(scratch) / "compile_commands.json"
    if not cc_path.exists():
        rep.warn(f"deep.{plan.name}.evidence", "未生成 compile_commands.json，无法取编译命令")
        return {"configure_ok": True}

    entries = json.loads(cc_path.read_text(encoding="utf-8"))
    if not entries:
        rep.warn(f"deep.{plan.name}.evidence", "compile_commands.json 为空")
        return {"configure_ok": True}

    # 取一条"业务源文件"的编译命令作为代表（优先 main.c）
    entry = next((e for e in entries if str(e.get("file", "")).endswith("main.c")), entries[0])
    args = entry.get("arguments")
    cmdline = " ".join(args) if args else entry.get("command", "")
    src_name = Path(str(entry.get("file", "?"))).name

    verify = plan.chip.get("verify") or {}
    for flag in verify.get("expect_flags", []) or []:
        if flag in cmdline:
            rep.ok(f"deep.{plan.name}.flag", f"{flag}  (来自 {src_name})")
        else:
            rep.fail(f"deep.{plan.name}.flag", f"实际编译命令缺少 {flag}")
    for d in verify.get("expect_defines", []) or []:
        if re.search(rf"-D{d}(\b|=|$)", cmdline) or f"-D{d}" in cmdline:
            rep.ok(f"deep.{plan.name}.define", f"-D{d}")
        else:
            rep.fail(f"deep.{plan.name}.define", f"实际编译命令缺少 -D{d}")

    fpu = (plan.chip.get("core") or {}).get("fpu", "")
    if (not fpu or fpu == "none") and "-mfloat-abi" in cmdline:
        rep.fail(f"deep.{plan.name}.fpu", "无 FPU 芯片却出现了 -mfloat-abi（C7 回归）")

    # 链接行份数：确认 -T 只出现一次（约束 C1）
    ld = Path(plan.linker_script).name
    if ld and "-T" in cmdline:
        rep.info(f"deep.{plan.name}.linkflags", f"代表命令含链接参数（ld={ld}）")

    return {"configure_ok": True, "entry": src_name}


# ── 工具链 provider（P1/P2）───────────────────────────────────────
def check_toolchains(cfg: Config, rep: Report, names) -> None:
    """项目声明的 provider：是否安装、版本是否满足、host/probe/serial 是否就绪。"""
    from elab.platform import provider as provider_mod
    try:
        reg = provider_mod.ProviderRegistry(cfg)
    except ElabError as exc:
        rep.fail("provider.registry", str(exc))
        return
    for name in names:
        proj = cfg.resolved_project(name)
        ref = str(proj.get("provider") or "").strip()
        if not ref:
            continue
        try:
            prov = reg.resolve_ref(ref)
        except ElabError as exc:
            rep.fail(f"provider.{name}", str(exc))
            continue
        if getattr(prov.ref, "source", "") == "builtin":
            continue
        tc = prov.resolve(cfg, proj)
        state = provider_mod.evaluate(cfg, name, tc)
        ref_s = f"{prov.ref.name}@{prov.ref.version}"
        if state.get("available"):
            rep.info(f"provider.{name}", f"provider {ref_s} 可用（"
                     f"steps={','.join(sorted((prov.man.get('steps') or {})))}）")
            continue
        for reason in state.get("reasons") or []:
            code = str(reason.get("code") or "")
            msg = f"provider {ref_s}：{reason.get('message')}"
            if code in ("host.missing", "host.invalid", "probe.missing"):
                rep.fail(f"provider.{name}.{code}", msg)
            else:
                rep.warn(f"provider.{name}.{code}", msg)


#: ``${host:...}`` 引用扫描（P1 主机 profile 键审计）。
_HOST_REF_RE = re.compile(r"\$\{host:([^}]+)\}")


def check_host_refs(cfg: Config, rep: Report, strict: bool = False) -> None:
    """校验当前 profile 下所有 ``${host:...}`` 引用都能解析。

    扫描范围 = 工程 YAML + **当前 profile 的 provider 发现根**里的 ``provider.yaml``：
    classic 只看 providers/installed，kharon 还会看 official/zephyr ——
    不会拿 Zephyr 的键去要求 classic 主机。
    """
    from elab.platform import provider as provider_mod

    files: list[Path] = sorted(core_paths.products_dir(cfg).glob("*.yaml"))
    for root_dir in provider_mod.provider_roots(cfg):
        if root_dir.is_dir():
            files.extend(sorted(root_dir.rglob("provider.yaml")))

    refs: dict[str, set[str]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _HOST_REF_RE.finditer(text):
            key = match.group(1).strip()
            if key:
                refs.setdefault(key, set()).add(to_fwd(path.relative_to(cfg.root)))

    missing = [key for key in sorted(refs)
               if cfg.host_value(key) is None]
    sel = cfg.host_selection
    profile = sel["profile"] or "classic"
    for key in missing:
        where = ", ".join(sorted(refs[key])[:3])
        msg = (f"${{host:{key}}} 未定义（profile={profile}，引用：{where}）；"
               f"修复：在 host.local.yaml 的 {profile}: 下补该键，"
               f"或设置对应 ELAB_* 环境变量")
        (rep.fail if strict else rep.warn)("host.ref", msg)
    if refs and not missing:
        rep.ok("host.refs", f"{len(refs)} 个 ${{host:...}} 引用在当前 profile 下全部可解析")


# ── 工作区体检（三形态 + 双拓扑，2026-11-18）──────────────────────
def workspace_check(cfg: Config, rep: Report, only: str | None = None) -> None:
    """``elab doctor --workspace``：拓扑 / 形态 / 模块缺口 / 树内残留板。

    为什么单独一节而不是塞进 ``check_host``：这一节回答的是**"这个工程长什么样"**，
    不是"路径对不对"。L1「看懂」的目标是"不装任何东西也能把话说清楚"，所以
    连不上 west 时只 warn，不 fail（standalone 本来就是合法形态）。
    """
    from elab.platform import westws
    from elab.platform import wszephyr

    facts = wszephyr.discover(cfg, refresh=True)
    rep.info("ws.source", f"工作区来源={facts.source}（env → 探测 → host）")
    if facts.present:
        rep.ok("ws.topology", f"拓扑={facts.topology}"
                              f"（清单仓={facts.manifest_repo}）")
        rep.info("ws.manifest", facts.manifest_path)
    else:
        rep.warn("ws.topology", "没有 west 工作区（.west/config 不存在）"
                                "：只能走 cmake 直编，或先 kharon bootstrap")
    if facts.zephyr_base:
        rep.ok("ws.zephyr", f"ZEPHYR_BASE={facts.zephyr_base}"
                            f"  {facts.zephyr_version}")
    else:
        rep.fail("ws.zephyr", "找不到 ZEPHYR_BASE（[zephyr] base / $ZEPHYR_BASE 都没有）")

    # 模块缺口：只说"缺"，不说"缺了会怎样"等于没说 —— 这里给到 board 粒度。
    if facts.present:
        modules = westws._modules(cfg)                 # west list（~1s）
        present = [m["name"] for m in modules if m.get("present")]
        declared = [m["name"] for m in modules]
        if modules:
            rep.info("ws.modules", f"{len(present)} / {len(declared)} 已拉取"
                                   "（west list 全量声明，present 才是本机有的）")
        boards: list[str] = []
        try:
            names = [only] if only else list(cfg.projects)
        except ElabError:
            names = []
        for name in names:
            try:
                proj = cfg.resolved_project(name)
                arch = str(proj.get("archetype") or "")
                prov = str(proj.get("provider") or "")
                if arch != "zephyr" and not prov.startswith("zephyr/"):
                    continue
                chip = cfg.chip_of(proj)
            except ElabError:
                continue
            target = str(chip.get("target") or chip.get("id") or "")
            board = target.split("/")[0] if target else ""
            if board and board not in boards:
                boards.append(board)
        for board in boards:
            info = wszephyr.modules_for(board, present,
                                        zephyr_base=facts.zephyr_base)
            if not info["needed"]:
                continue                            # 未知 family：不猜，静默跳过
            if info["missing"]:
                for mod in info["missing"]:
                    rep.fail(f"ws.mod.{board}.{mod}",
                             f"板 {board} 需要模块 {mod}，本机未拉取 —— "
                             f"修复：python -m west update {mod}")
            else:
                rep.info(f"ws.mod.{board}",
                         f"需要的模块齐了：{', '.join(info['needed'])}")

    # 树内残留板：module 化之后，同名副本会让 `west boards` 直接 RuntimeError
    module_boards = Path(cfg.zephyr_root) / "kharon-boards" / "boards"
    if not module_boards.is_dir():
        module_boards = Path(cfg.root) / "zephyr_modules" / "elab-boards" / "boards"
    if module_boards.is_dir() and facts.zephyr_base:
        for bm in sorted(module_boards.glob("*/*/board.yml")):
            try:
                data = _yaml.load_file(bm) or {}
            except Exception:                      # noqa: BLE001
                data = {}
            vendor = str(((data.get("board") or {}).get("vendor")) or bm.parent.parent.name)
            stale = Path(facts.zephyr_base) / "boards" / vendor / bm.parent.name
            if stale.exists():
                rep.fail("ws.stale-board",
                         f"树内残留旧副本 {stale} —— module 化后必须删掉"
                         "（否则 west boards 报 defined multiple times）")


# ── 对外入口 ──────────────────────────────────────────────────────
def run(cfg: Config, only: str | None = None, deep: bool = False,
        strict_lock: bool = False, workspace: bool = False,
        strict_host: bool = False) -> Report:
    rep = Report()
    host = Host(cfg)

    names = [only] if only else list(cfg.projects)
    # ★ ARM 交叉工具链检查只对"内核接管工具链"的工程有意义：范围内全是
    #   provider 工程时跳过（避免 Kharon 主机上出现声明为空的假 FAIL）。
    native_needed = False
    for name in names:
        try:
            from elab.platform import provider as provider_mod
            tc = provider_mod.resolve(cfg, name)
        except ElabError:
            native_needed = True
            break
        if tc.provider.source == "builtin":
            native_needed = True
            break
    check_host(cfg, rep, host, native_needed=native_needed, strict_host=strict_host)
    check_host_refs(cfg, rep, strict=strict_host)

    for name in names:
        try:
            plan = plan_for(cfg, name)
        except ElabError as exc:
            rep.fail(f"proj.{name}", str(exc))
            continue
        check_chip(rep, cfg, host, plan.chip)
        check_project(rep, cfg, host, plan)
        check_drift(rep, plan)
        if deep:
            deep_check(rep, plan)
    check_toolchains(cfg, rep, names)

    if workspace:
        workspace_check(cfg, rep, only)

    from elab.platform import lock as lock_mod
    lock_mod.check_into(rep, cfg, strict=strict_lock)
    return rep
