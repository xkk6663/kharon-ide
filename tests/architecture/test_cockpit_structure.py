"""P2 结构门禁：驾驶舱入口必须保持"薄"，路由与领域逻辑各归其位。

这些断言是**可维护性契约**，不是功能测试：功能由 ``tests/it_cockpit_server.py``
的 HTTP 集成用例覆盖。这里防止未来改动把 server.py 重新堆回 4000 行。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COCKPIT = ROOT / "cockpit"

#: 入口文件行数上限（P2 目标：< 500 行）。
SERVER_MAX_LINES = 500
#: 单个 HTTP 方法上限（P2 目标：< 120 行）。
METHOD_MAX_LINES = 120

#: 已迁出的领域模块 → 必须存在的入口函数。
ROUTE_MODULES = {
    "cockpit/api/routes/system.py": {"handle_get"},
    "cockpit/api/routes/workspace.py": {"handle_get"},
    "cockpit/api/routes/runs.py": {"handle_get", "handle_events", "handle_post"},
    "cockpit/api/routes/debug.py": {"handle_get", "handle_post", "handle_delete"},
    "cockpit/api/routes/serial.py": {"handle_get", "handle_post"},
    "cockpit/api/routes/project.py": {"handle_post"},
    "cockpit/api/routes/pkg_actions.py": {"pkg", "adapt"},
    "cockpit/api/routes/sse.py": {"sse", "sse_debug", "sse_frame", "sse_write"},
    "cockpit/api/routes/web.py": {"static"},
}

#: 领域实现模块 → 必须存在的类。
DOMAIN_MODULES = {
    "cockpit/run/manager.py": {"ActiveRun", "RunManager"},
    "cockpit/debug/manager.py": {"DebugManager"},
    "cockpit/platform/jobs.py": {"PkgJobManager"},
}


def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _top_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names.add(n.name)
    return names


class CockpitStructureTest(unittest.TestCase):
    def test_server_is_thin(self) -> None:
        path = COCKPIT / "server.py"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), SERVER_MAX_LINES,
                        "server.py 必须保持薄入口（路由/领域逻辑已迁出）")
        tree = ast.parse("\n".join(lines))
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.name == "CockpitHandler":
                for m in n.body:
                    if isinstance(m, ast.FunctionDef) and m.name in (
                            "do_GET", "do_POST", "do_DELETE"):
                        size = m.end_lineno - m.lineno + 1
                        self.assertLess(size, METHOD_MAX_LINES,
                                        f"{m.name} 应只做分发（当前 {size} 行）")

    def test_server_does_not_own_domain_objects(self) -> None:
        names = _top_names(_tree("cockpit/server.py"))
        for banned in ("RunManager", "DebugManager", "PkgJobManager"):
            self.assertNotIn(banned, names,
                             f"{banned} 应位于 cockpit 子包，而不是 server.py")

    def test_route_modules_expose_entrypoints(self) -> None:
        for rel, required in ROUTE_MODULES.items():
            with self.subTest(module=rel):
                self.assertTrue((ROOT / rel).is_file(), f"缺少 {rel}")
                self.assertTrue(required <= _top_names(_tree(rel)),
                                f"{rel} 缺少入口函数 {sorted(required)}")

    def test_domain_managers_live_in_subpackages(self) -> None:
        for rel, required in DOMAIN_MODULES.items():
            with self.subTest(module=rel):
                self.assertTrue((ROOT / rel).is_file(), f"缺少 {rel}")
                self.assertTrue(required <= _top_names(_tree(rel)))

    def test_router_dispatches_every_domain(self) -> None:
        src = (COCKPIT / "api" / "router.py").read_text(encoding="utf-8")
        for domain in ("system", "workspace", "runs", "debug", "serial", "project"):
            self.assertIn(domain, src, f"router 未接入 {domain} 路由")


if __name__ == "__main__":
    unittest.main()
