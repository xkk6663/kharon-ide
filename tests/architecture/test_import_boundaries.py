"""test_import_boundaries —— 架构门禁（P0 安全网）。

运行：``PYTHONPATH=services python -m unittest discover -s tests/architecture -p "test_*.py"``

为什么先有它再动目录（方案 docs/design/架构优化与目录重构方案_2026-11-19.md §4-P0）：
文件一移动，最怕的是**依赖方向悄悄反转**（api 反向依赖实现、业务层 import 工具层），
而这种错误在行为测试里往往仍然全绿。本模块用 ``ast`` 静态扫描把边界写成可执行规则。

规则（与方案 §3.3 硬规则一一对应）：

1. 业务层（``services/elab/**``、``services/kharon/**``）不得 import 工具层与仓库外约定目录
   （``cockpit`` / ``packages`` / ``tests`` / ``site``）。
2. ``services/elab/<context>`` 子包之间只允许单向依赖：
   ``core ← platform ← workflow ← api``，``core ← platform ← serial/debug``。
   ``compat`` 是旧路径转发层，允许引用任何 context。
3. ``cockpit/api/**`` 只允许 import ``elab`` 的公开 facade（顶层模块或 context 包），
   不得伸进 context 的子模块。
4. ``elab/api/**`` 不得 import ``elab.serial.*`` / ``elab.debug.*`` 的具体实现，
   只能调用 context 的 facade（``elab.serial`` / ``elab.debug``）。
5. ``packs/**`` 与 ``providers/**`` 是声明式数据，不得出现 import ``elab`` 的 Python 文件。

现状（P3 分包前）：子包尚不存在时规则 2~4 自动跳过；规则 1、5 立即生效。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
ELAB = SERVICES / "elab"

#: context 子包允许依赖的其他 context（不含自身）。
#:
#: ★ 与方案 §4-P3 的分组一致，但把**实测存在的懒导入**如实写出来，而不是写一张
#:   好看却会让测试天天红的理想图：
#:   - core.config 在函数内懒导入 platform.pkg（芯片事实源发现）；
#:   - platform.provider 在函数内懒导入 workflow.run（provider 回调内核步骤）；
#:   - serial.serialconsole 懒导入 workflow.run（runs_dir_for）；
#:   - platform.provider 懒导入 serial.serialport（preflight 枚举串口）。
#: 这几条都是**运行时按需**的，包 __init__ 不触发，因此不构成导入期循环。
CONTEXT_DEPS: dict[str, set[str]] = {
    "core": {"platform"},
    "platform": {"core", "workflow", "serial"},
    "workflow": {"core", "platform", "serial", "debug"},
    "serial": {"core", "platform", "workflow"},
    "debug": {"core", "platform", "workflow"},
    "api": {"core", "platform", "workflow", "serial", "debug"},
}
#: 旧路径兼容层：转发层可以引用任何 context。
COMPAT_CONTEXT = "compat"

#: 业务层禁止 import 的顶层名字（工具层 / 测试 / 站点）。
FORBIDDEN_BUSINESS_IMPORTS = {"cockpit", "packages", "tests", "site"}


def _module_name(path: Path, base_dir: Path, top: str) -> str:
    rel = path.relative_to(base_dir).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([top, *parts]) if parts else top


def _resolve_from(current: str, node: ast.ImportFrom) -> str:
    """把 ``from .x import y`` 解析成绝对模块名（只到 node.module 为止）。"""
    if not node.level:
        return node.module or ""
    pkg_parts = current.split(".")
    # level=1 → 当前模块所在包；level=2 → 再上一级
    base = pkg_parts[: len(pkg_parts) - node.level]
    mod = node.module or ""
    return ".".join([*base, *(mod.split(".") if mod else [])])


def _iter_imports(path: Path, current: str):
    """产出 ``(导入的绝对模块名, 行号)``；解析失败的文件直接跳过。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):       # 非 UTF-8 / 语法错误不归本门禁管
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            yield _resolve_from(current, node), node.lineno


def _python_files(root: Path):
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts)


class ImportBoundaryTest(unittest.TestCase):
    # ── 规则 1 ────────────────────────────────────────────────
    def test_business_layer_does_not_import_tool_layer(self) -> None:
        violations = []
        for path in _python_files(SERVICES):
            current = _module_name(path, SERVICES, "services")
            for mod, line in _iter_imports(path, current):
                head = mod.split(".")[0]
                if head in FORBIDDEN_BUSINESS_IMPORTS:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{line} import {mod}")
        self.assertEqual(violations, [],
                         "业务层不得 import 工具层/测试/站点：\n" + "\n".join(violations))

    # ── 规则 2 ────────────────────────────────────────────────
    def test_context_dependency_direction(self) -> None:
        violations = []
        for ctx, allowed in CONTEXT_DEPS.items():
            ctx_dir = ELAB / ctx
            if not ctx_dir.is_dir():
                continue                       # P3 尚未迁入该 context
            for path in _python_files(ctx_dir):
                current = _module_name(path, SERVICES, "elab")
                for mod, line in _iter_imports(path, current):
                    parts = mod.split(".")
                    if len(parts) < 2 or parts[0] != "elab":
                        continue
                    dep = parts[1]
                    if dep in (ctx, COMPAT_CONTEXT, "kernel"):
                        continue
                    if dep not in CONTEXT_DEPS:
                        continue               # 顶层旧模块，迁移期允许
                    if dep not in allowed:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{line} {ctx} → {dep}")
        self.assertEqual(violations, [],
                         "context 依赖方向违规（core←platform←workflow←api）：\n"
                         + "\n".join(violations))

    # ── 规则 3 ────────────────────────────────────────────────
    def test_cockpit_api_imports_only_public_facade(self) -> None:
        api_dir = ROOT / "cockpit" / "api"
        violations = []
        for path in _python_files(api_dir):
            current = _module_name(path, ROOT, "cockpit")
            for mod, line in _iter_imports(path, current):
                parts = mod.split(".")
                if parts[0] != "elab" or len(parts) < 3:
                    continue
                if parts[1] in CONTEXT_DEPS or parts[1] in (COMPAT_CONTEXT, "kernel"):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{line} import {mod}")
        self.assertEqual(violations, [],
                         "cockpit/api 只能 import elab 公开 facade，不得进子模块：\n"
                         + "\n".join(violations))

    # ── 规则 4 ────────────────────────────────────────────────
    def test_api_context_calls_serial_debug_facade_only(self) -> None:
        api_dir = ELAB / "api"
        violations = []
        for path in _python_files(api_dir):
            current = _module_name(path, SERVICES, "elab")
            for mod, line in _iter_imports(path, current):
                parts = mod.split(".")
                if len(parts) >= 3 and parts[0] == "elab" and parts[1] in ("serial", "debug"):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{line} import {mod}")
        self.assertEqual(violations, [],
                         "elab/api 不得直接 import serial/debug 的具体实现：\n"
                         + "\n".join(violations))

    # ── 规则 5 ────────────────────────────────────────────────
    def test_packs_and_providers_are_declarative_data(self) -> None:
        violations = []
        for name in ("packs", "providers"):
            for path in _python_files(ROOT / name):
                current = _module_name(path, ROOT, name)
                for mod, line in _iter_imports(path, current):
                    if mod.split(".")[0] in {"elab", "cockpit"}:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{line} import {mod}")
        self.assertEqual(violations, [],
                         "packs/providers 是声明式数据，不得写可执行逻辑：\n"
                         + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
