"""`scripts/app_boundary_check.py` 的可执行夹具。

CI 不能 checkout 完整 Zephyr 工作区，所以这里用临时根构造“合法/违规”两种最小
workspace，保证边界规则本身在 CI 里真的会跑起来，而不是只靠人工本地检查。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "app_boundary_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("app_boundary_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _make_root(tmp: Path, *, with_presets: bool = False,
               with_board_definition: bool = False,
               with_two_truths: bool = False) -> Path:
    app = tmp / "applications" / "demo_app"
    app.mkdir(parents=True)
    (app / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})\n"
        "project(demo_app)\n"
        "target_sources(app PRIVATE src/main.c)\n",
        encoding="utf-8",
    )
    (app / "prj.conf").write_text("CONFIG_GPIO=y\n", encoding="utf-8")
    (app / "app.yaml").write_text("kind: product\n", encoding="utf-8")
    if with_presets:
        (app / "CMakePresets.json").write_text("{}\n", encoding="utf-8")
    if with_board_definition:
        board = app / "boards" / "demo_board"
        board.mkdir(parents=True)
        (board / "board.yml").write_text("board:\n  name: demo_board\n", encoding="utf-8")

    manifest = tmp / "kharon-manifest" / "products" / "kharon"
    manifest.mkdir(parents=True)
    chip = "other_chip" if with_two_truths else "demo_chip"
    (manifest / "demo_app.yaml").write_text(
        "name: demo_app\n"
        f"chip: {chip}\n"
        "archetype: zephyr\n"
        "targets:\n"
        "  - id: default\n"
        "    chip: demo_chip\n"
        "    board: demo_board\n",
        encoding="utf-8",
    )
    return tmp


def test_fixture_workspace_is_clean():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        root = _make_root(Path(td))
        violations: list[dict] = []
        for app in mod.iter_apps(root):
            violations.extend(mod.check_app(root, app))
        violations.extend(mod.check_products(root))
        assert violations == []


def test_presets_and_board_definition_are_rejected():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        root = _make_root(Path(td), with_presets=True, with_board_definition=True)
        app = root / "applications" / "demo_app"
        violations = mod.check_app(root, app)
        rules = {v["rule"] for v in violations}
        assert "app.presets" in rules
        assert "app.board_definition" in rules


def test_product_two_truths_is_rejected():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        root = _make_root(Path(td), with_two_truths=True)
        violations = mod.check_products(root)
        assert {v["rule"] for v in violations} == {"product.two_truths"}


def test_cli_fixture_passes_from_repo():
    """CI 里直接调用**仓库内**夹具，证明脚本入口与退出码可用。"""
    fixture = REPO / "tests" / "fixtures" / "app_boundary_ws"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(fixture), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["violations"] == []
