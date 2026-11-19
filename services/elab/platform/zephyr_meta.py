"""elab.zephyr_meta —— Zephyr 构建产物的只读读数（内存 / 板级事实）。

为什么需要它
------------
内核既有的 ``builder.parse_map_memory`` 认的是 **CubeMX/GNU ld 的 map 形态**：

    .isr_vector     0x08000000       0xcc          ← 段名顶格、行尾就是大小

Zephyr 的 map 不一样，同一条记录前面有**一个空格**，行尾还跟着对象文件：

     .text          0x00000000        0x0 zephyr/CMakeFiles/.../empty_file.c.obj

于是既有解析器对 Zephyr 只能算出 FLASH（还是撞运气撞到的），RAM 永远是空的。
**不去改那个解析器**：classic 的逐字节不变量比多一个数字重要。这里另起一条
只认 ELF 的路——readelf 的活，纯 Python 干，不拉工具链、不依赖平台。

红线（方案 §3.3 / R8）
----------------------
这些数字是**读数**，不是**声明**：它来自构建产物（`zephyr.elf` + `zephyr.map`），
绝不回写进 `chips/*.yaml`。芯片卡上的「实测」角标就是这个意思。
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

#: ELF 常量
_PT_LOAD = 1
_SHF_ALLOC = 0x2
_SHT_NOBITS = 8


# ── region 表（来自 .map 的 Memory Configuration）─────────────────
def regions_from_map(text: str) -> list[tuple[str, int, int]]:
    """从 ``Memory Configuration`` 表取 ``[(name, origin, length)]``（保持声明顺序）。

    顺序有意义：Zephyr 的 ``RAM`` 与 ``SRAM0`` 是**同一段地址的别名**，
    链接器把用量算给先出现的那一个（实测 RAM 有数、SRAM0 为 0）。
    """
    out: list[tuple[str, int, int]] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Memory Configuration"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line:
            continue
        if line.startswith("Linker script"):
            break
        if line.startswith("Name") and "Origin" in line:
            continue
        parts = line.split()
        if len(parts) < 3 or not parts[1].startswith("0x") or not parts[2].startswith("0x"):
            continue
        name = parts[0]
        if "default" in name:            # `*default*` 覆盖全地址空间，必须排除
            continue
        try:
            out.append((name, int(parts[1], 16), int(parts[2], 16)))
        except ValueError:
            continue
    return out


# ── ELF 只读解析（32/64 位、LE/BE 都认）───────────────────────────
def _elf_sections(path: Path):
    """产出 ``(name_index, sh_type, sh_flags, sh_addr, sh_size)``。"""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        return
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    if is64:
        (e_phoff,) = struct.unpack_from(end + "Q", data, 0x20)
        (e_shoff,) = struct.unpack_from(end + "Q", data, 0x28)
        (e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
            end + "HHHHH", data, 0x36)
        sh_fmt = end + "IIQQQQ"
        ph_fmt = end + "IIQQQQQQ"
    else:
        (e_phoff,) = struct.unpack_from(end + "I", data, 0x1C)
        (e_shoff,) = struct.unpack_from(end + "I", data, 0x20)
        (e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
            end + "HHHHH", data, 0x2A)
        sh_fmt = end + "IIIIII"
        ph_fmt = end + "IIIIIIII"

    # 程序头：给每个 sh_addr 找 LMA（.data 的初值存在 Flash，VMA 在 RAM）
    loads: list[tuple[int, int, int]] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if is64:
            p_type, _flags, _off, p_vaddr, p_paddr, _filesz, p_memsz, _align = \
                struct.unpack_from(ph_fmt, data, off)
        else:
            p_type, _off, p_vaddr, p_paddr, _filesz, p_memsz, _flags, _align = \
                struct.unpack_from(ph_fmt, data, off)
        if p_type == _PT_LOAD:
            loads.append((p_vaddr, p_memsz, p_paddr))

    def lma_of(addr: int):
        for vaddr, memsz, paddr in loads:
            if vaddr <= addr < vaddr + memsz:
                return paddr + (addr - vaddr)
        return None

    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        vals = struct.unpack_from(sh_fmt, data, off)
        sh_name, sh_type, sh_flags, sh_addr, _sh_off, sh_size = vals[:6]
        if not (sh_flags & _SHF_ALLOC) or sh_size == 0:
            continue
        yield sh_name, sh_type, sh_flags, sh_addr, sh_size, lma_of(sh_addr)


def memory_from_elf(elf: Path, regions) -> dict:
    """按 region 归集 ELF 的 ALLOC 段，按**跨度**（span）而不是求和。

    span 的理由与 ``builder.parse_map_memory`` 同一条：ld 的 region 游标从 origin
    起算只增不减，段间对齐填充也算占用。求和会少算几个字节的 `*fill*`。
    """
    acc = {name: None for name, _o, _l in regions}

    def account(addr: int, size: int) -> None:
        for name, origin, length in regions:
            if origin <= addr < origin + length:
                hi = addr + size
                acc[name] = hi if acc[name] is None else max(acc[name], hi)
                return

    for _n, sh_type, _f, sh_addr, sh_size, lma in _elf_sections(elf):
        account(sh_addr, sh_size)
        # NOBITS（.bss）只有名义 LMA，不占 Flash
        if lma is not None and lma != sh_addr and sh_type != _SHT_NOBITS:
            account(lma, sh_size)

    out: dict[str, dict] = {}
    for name, origin, length in regions:
        hi = acc[name]
        if hi is None or not length:
            continue
        used = max(0, hi - origin)
        out[name] = {"used": used, "region": length,
                     "pct": round(used * 100.0 / length, 2)}
    return out


# ── 对外：一次读全 ────────────────────────────────────────────────
def memory(work_dir: str) -> dict:
    """``{region: {used, region, pct}}``；读不到返回 ``{}``（不抛错）。"""
    zdir = Path(work_dir) / "zephyr"
    mp = zdir / "zephyr.map"
    elf = zdir / "zephyr.elf"
    if not mp.is_file() or not elf.is_file():
        return {}
    try:
        regions = regions_from_map(mp.read_text(encoding="utf-8", errors="replace"))
        if not regions:
            return {}
        return memory_from_elf(elf, regions)
    except (OSError, struct.error):
        return {}


def is_zephyr_build(work_dir: str) -> bool:
    """构建目录是不是 Zephyr 产物（判据是 ``zephyr/zephyr.elf`` + map）。"""
    zdir = Path(work_dir) / "zephyr"
    return (zdir / "zephyr.elf").is_file() and (zdir / "zephyr.map").is_file()


def build_info(work_dir: str) -> dict:
    """``build_info.yml`` 里的事实（board / dts / kconfig / west 命令行）。

    这是 cmake 生成的嵌套 YAML（map + 块序列），内核自带的极简解析器能吃下
    （实测：``cmake/board/path`` 的块序列与 ``west.command`` 的多反斜杠字符串都正确）。
    读不到就返回空——面板退化为"无数据"，不编造。
    """
    path = Path(work_dir) / "build_info.yml"
    if not path.is_file():
        return {}
    try:
        from elab.core import _yaml
        data = _yaml.load_file(path)
        return data if isinstance(data, dict) else {}
    except Exception:                    # noqa: BLE001 —— 只读取数，坏文件不阻塞
        return {}


# ── runners.yaml：Zephyr 自己声明的 flash / debug 事实 ─────────────
_TOP_KEYS = {"runners", "flash-runner", "debug-runner", "config", "args"}


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def runners(work_dir: str) -> dict:
    """``zephyr/runners.yaml`` —— 探针、GDB、OpenOCD 路径的唯一事实源。

    为什么必须读它，而不是在芯片包里写 openocd cfg
    ----------------------------------------------
    这份文件在 configure 阶段由 ``board.cmake`` 生成，已经把 **runner 列表、
    默认 flash/debug runner、elf/hex/bin 路径、gdb 与 openocd 的绝对路径、
    openocd 的 scripts 搜索路径、每路 runner 的参数** 全都算好了。

    它是构建系统的输出，也就是 Zephyr 侧那一半"单一事实源"。我们在芯片包里
    再抄一份，立刻出现第二套真相（同一块板两份参数，谁对谁错取决于人）——
    这正是方案 §6.1 的红线。所以顺序是：**runners.yaml 优先，芯片包只做覆盖**。

    内核自带的极简 YAML 解析器认不了这里的形态（序列项与 key 同缩进：
    ``runners:`` 的下一行是顶格的 ``- dfu-util``），所以这里用只认这份文件
    骨架的小状态机，而不是放宽通用解析器（classic 依赖它逐字节稳定）。
    """
    path = Path(work_dir) / "zephyr" / "runners.yaml"
    out: dict = {
        "ok": False,
        "path": str(path).replace("\\", "/"),
        "runners": [], "flash_runner": "", "debug_runner": "",
        "config": {}, "args": {},
    }
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    section = ""
    list_key = ""       # config 下"值在后续序列里"的键（openocd_search）
    args_key = ""

    for raw in text.splitlines():
        line = raw.rstrip()
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        # 顶格序列项（`runners:` 之下）
        if indent == 0 and body.startswith("- "):
            if section == "runners":
                out["runners"].append(_unquote(body[2:]))
            continue

        key, _sep, val = body.partition(":")
        key = key.strip()
        val = _unquote(val)

        if indent == 0:
            list_key = ""
            args_key = ""
            if val:                                  # 顶层 `flash-runner: xxx`
                if key == "flash-runner":
                    out["flash_runner"] = val
                elif key == "debug-runner":
                    out["debug_runner"] = val
                continue
            if key in _TOP_KEYS:
                section = key
            continue

        # 缩进内的序列项
        if body.startswith("- "):
            item = _unquote(body[2:])
            if section == "args" and args_key:
                out["args"].setdefault(args_key, []).append(item)
            elif section == "config" and list_key:
                cur = out["config"].get(list_key)
                if isinstance(cur, list):
                    cur.append(item)
            continue

        if section == "config":
            if val:
                out["config"][key] = val
                list_key = ""
            else:
                out["config"][key] = []
                list_key = key
        elif section == "args":
            args_key = key
            out["args"].setdefault(key, [])

    out["ok"] = bool(out["runners"] or out["debug_runner"])
    return out


def flash_runner(work_dir: str, preferred: str = "") -> str:
    """取实际会用的 flash runner（``preferred`` 覆盖 → 否则 yaml 的默认值）。"""
    r = runners(work_dir)
    if preferred:
        return preferred
    return r.get("flash_runner") or (r.get("runners") or [""])[0]


# ── .config：Kconfig 的最终取值（含未启用项）──────────────────────
_CONFIG_SET_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
_CONFIG_UNSET_RE = re.compile(r"^#\s*(CONFIG_[A-Za-z0-9_]+)\s+is not set$")
_GROUP_RE = re.compile(r"^#\s*(.+?)\s*$")


def _kconfig_kind(name: str, value: str) -> str:
    if value == "y":
        return "bool"
    if value == "m":
        return "module"
    if re.fullmatch(r"-?\d+", value):
        return "int"
    if re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return "hex"
    if name.endswith('_STRING') or value.startswith('"'):
        return "string"
    return "other"


def _short_group(name: str) -> str:
    """``cmsis_6 (C:/…/modules/hal/cmsis_6)`` → ``cmsis_6 (cmsis_6)``（UI 用）。

    完整名字仍留在 ``name`` 里供搜索——模块名后的路径常常就是模块名，
    但偶尔不同（如 ``mbedtls (modules/crypto/mbedtls)``），所以不能直接丢。
    """
    m = re.fullmatch(r"(.*?)\s*\((.+)\)", name)
    if not m:
        return name
    tail = m.group(2).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return f"{m.group(1)} ({tail})"


def kconfig(work_dir: str) -> dict:
    """``zephyr/.config`` → 分组的符号表。

    为什么给"未启用"也留一条：调 Kconfig 时最常问的是"这个选项到底是没开，
    还是根本没定义"——只列 ``=y`` 的项回答不了这个问题。
    """
    path = Path(work_dir) / "zephyr" / ".config"
    out: dict = {"ok": False, "path": str(path).replace("\\", "/"),
                 "groups": [], "count": 0, "enabled": 0}
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out

    groups: list[dict] = []
    stray: list[dict] = []          # 落在分节之外的项（`# end of X` 之后那一堆）
    cur: dict | None = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _CONFIG_SET_RE.match(line)
        if m:
            name, value = m.group(1), _unquote(m.group(2))
            item = {"name": name, "value": value, "enabled": True,
                    "kind": _kconfig_kind(name, value)}
            (cur["items"] if cur else stray).append(item)
            out["enabled"] += 1
            continue
        m = _CONFIG_UNSET_RE.match(line)
        if m:
            item = {"name": m.group(1), "value": "", "enabled": False, "kind": "bool"}
            (cur["items"] if cur else stray).append(item)
            continue
        m = _GROUP_RE.match(line)
        if m:
            name = m.group(1)
            if cur and cur["items"]:
                groups.append(cur)
            # `# end of XXX` 是分节**结束**标记，不是新分节。落在它之后、
            # 下一个标题之前的项（大量 `# CONFIG_X is not set`）属于无分组，
            # 归到 `end of XXX` 下会让人以为那是 XXX 的配置。
            cur = None if name.startswith("end of ") else {
                "name": name, "short": _short_group(name), "items": []}
    if cur and cur["items"]:
        groups.append(cur)
    if stray:
        groups.append({"name": "（未分组）", "short": "（未分组）", "items": stray})

    out["groups"] = groups
    out["count"] = sum(len(g["items"]) for g in groups)
    out["ok"] = out["count"] > 0
    return out


# ── zephyr.dts：展开后的设备树（注释里带路径与来源）───────────────
_NODE_RE = re.compile(r"/\*\s*node\s+'([^']*)'\s+defined in\s+(.+?)\s*\*/")
_ATTR_SRC_RE = re.compile(r"/\*\s*in\s+(.+?)\s*\*/")


def devicetree(work_dir: str) -> dict:
    """``zephyr/zephyr.dts`` → 扁平节点表（带来源文件与行号）。

    Zephyr 构建时会把 overlay 合并、include 展开，并在**每个节点前写一条注释**：

        /* node '/soc/usart@40011000' defined in zephyr\\dts\\arm\\st\\f4\\stm32f4.dtsi:1234 */

    这比解析 dts 语法可靠得多，而且顺带给了"这一行是谁写的"——调 overlay 时
    最想知道的就是"我改的属性有没有生效、生效的是哪一份文件"。
    """
    path = Path(work_dir) / "zephyr" / "zephyr.dts"
    out: dict = {"ok": False, "path": str(path).replace("\\", "/"),
                 "nodes": [], "count": 0}
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out

    nodes: list[dict] = []
    index: dict[str, dict] = {}
    cur: dict | None = None
    pending: tuple[str, str] | None = None
    pending_attr: tuple[str, str] | None = None      # 跨行属性值续接

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        m = _NODE_RE.match(stripped)
        if m:
            node_path, src = m.group(1), m.group(2)
            if node_path in index:
                cur = index[node_path]
            else:
                parent = node_path.rsplit("/", 1)[0] or "/"
                name = node_path.rsplit("/", 1)[-1] or "/"
                cur = {"path": node_path, "name": name,
                       "parent": parent if node_path != "/" else "",
                       "source": src, "props": []}
                index[node_path] = cur
                nodes.append(cur)
            pending = None
            pending_attr = None
            continue

        if not stripped or stripped.startswith("/*") or stripped.startswith("*/"):
            continue

        if pending_attr is not None and cur is not None:
            # 上一行没以 `;` 结尾 → 值继续
            name, acc = pending_attr
            acc = (acc + " " + stripped.split("/*")[0].strip()).strip()
            if acc.endswith(";"):
                cur["props"].append({"name": name, "value": acc[:-1].strip(), "source": ""})
                pending_attr = None
            else:
                pending_attr = (name, acc)
            continue

        if cur is None or "=" not in stripped:
            continue

        # 属性行：`key = value;  /* in file:line */`
        body, _sep, tail = stripped.partition("/*")
        body = body.strip()
        src = ""
        msrc = _ATTR_SRC_RE.search(tail)
        if msrc:
            src = msrc.group(1)
        name, _eq, value = body.partition("=")
        name = name.strip()
        value = value.strip()
        if value.endswith(";"):
            cur["props"].append({"name": name, "value": value[:-1].strip(), "source": src})
        else:
            pending_attr = (name, value)

    out["nodes"] = nodes
    out["count"] = len(nodes)
    out["ok"] = bool(nodes)
    return out


# ── footprint：ram.json / rom.json 的层级用量（旭日图数据）──────────
def _trim_tree(node: dict, depth: int, budget: list[int]) -> dict:
    """裁到 ``depth`` 层、总节点数不超预算；返回 ``{name,size,children}``。"""
    kids = sorted(node.get("children") or [], key=lambda c: -(c.get("size") or 0))
    out = {"name": node.get("name") or "", "size": node.get("size") or 0}
    if depth <= 0 or not kids:
        if kids:
            out["cut"] = len(kids)          # 被裁掉的子节点数（前端可提示）
        return out
    kept: list[dict] = []
    for kid in kids:
        if budget[0] <= 0:
            out["cut"] = len(kids) - len(kept)
            break
        budget[0] -= 1
        kept.append(_trim_tree(kid, depth - 1, budget))
    out["children"] = kept
    return out


def footprint(work_dir: str, depth: int = 3, max_nodes: int = 800) -> dict:
    """``ram.json`` / ``rom.json`` → 裁剪后的层级树（``size_report`` 的输出）。

    这两个文件由 ``ninja ram_report rom_report`` 生成，是符号级用量，不是段级——
    旭日图要的就是"RAM 那 5.5K 分别被谁吃了"。原始 rom.json 有 260 KB，
    直接塞给前端会拖慢首屏，所以在后端就按深度与节点数裁。
    """
    out: dict = {"ok": False, "targets": {}}
    for target in ("ram", "rom"):
        path = Path(work_dir) / f"{target}.json"
        if not path.is_file():
            out["targets"][target] = {"ok": False, "path": str(path).replace("\\", "/")}
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            out["targets"][target] = {"ok": False, "path": str(path).replace("\\", "/")}
            continue
        root = data.get("symbols") or {}
        budget = [max_nodes]
        out["targets"][target] = {
            "ok": True,
            "path": str(path).replace("\\", "/"),
            "total": data.get("total_size") or 0,
            "root": _trim_tree(root, depth, budget),
            "truncated": budget[0] <= 0,
        }
    out["ok"] = any(t.get("ok") for t in out["targets"].values())
    return out
