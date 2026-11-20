"""架构页 YAML 编辑：路径反查 + 校验 + 原子写入。"""

from __future__ import annotations

import textwrap

import pytest

from elab.architecture_edit import read_source, write_source
from elab.core.config import Config, ElabError


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "zephyr-workspace"
    (root / "kharon-manifest" / "products" / "kharon").mkdir(parents=True)
    app = root / "applications" / "demo_app"
    app.mkdir(parents=True)
    (app / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
    (app / "app.yaml").write_text("kind: product\nboard_scope: demo\n", encoding="utf-8")
    (root / "kharon-manifest" / "products" / "kharon" / "demo.yaml").write_text(
        textwrap.dedent("""\
            name: demo
            archetype: app
            targets:
              - id: s3
                chip: esp32s3
                board: esp32s3_korvo
                app: demo_app
            """), encoding="utf-8")
    monkeypatch.setenv("ZEPHYR_ROOT", str(root))
    cfg = Config(root=tmp_path / "kharon-ide")
    cfg.projects  # 预热缓存，验证 save 后会失效
    return cfg


def test_read_product_and_app(ws):
    prod = read_source(ws, "product", "demo")
    assert prod["exists"] and "targets:" in prod["text"]
    assert prod["path"].endswith("demo.yaml")

    app = read_source(ws, "app", "demo_app")
    assert app["exists"] and "kind: product" in app["text"]


def test_save_product_updates_topology_cache(ws):
    proj = ws.projects["demo"]
    assert len(proj.get("targets") or []) == 1
    text = textwrap.dedent("""\
        name: demo
        archetype: app
        targets:
          - id: s3
            chip: esp32s3
            board: esp32s3_korvo
            app: demo_app
          - id: f103
            chip: stm32f103_mini
            board: stm32f103_mini
            app: demo_app
        """)
    saved = write_source(ws, "product", "demo", text)
    assert saved["path"].endswith("demo.yaml")
    assert len(ws.projects["demo"]["targets"]) == 2


def test_product_name_mismatch_rejected(ws):
    with pytest.raises(ElabError, match="name 必须保持"):
        write_source(ws, "product", "demo", "name: other\n")


def test_bad_yaml_does_not_clobber(ws):
    path = read_source(ws, "product", "demo")["path"]
    before = open(path, encoding="utf-8").read()
    with pytest.raises(ElabError, match="YAML 语法错误"):
        write_source(ws, "product", "demo", "name: demo\n\ttargets: []\n")
    assert open(path, encoding="utf-8").read() == before


def test_unknown_names_rejected(ws):
    with pytest.raises(ElabError, match="未知产品"):
        read_source(ws, "product", "nope")
    with pytest.raises(ElabError, match="未知 APP"):
        read_source(ws, "app", "nope")
    with pytest.raises(ElabError, match="kind"):
        read_source(ws, "board", "demo")


def test_app_yaml_can_be_created(ws):
    path = ws.zephyr_root / "applications" / "demo_app" / "app.yaml"
    path.unlink()
    assert read_source(ws, "app", "demo_app")["exists"] is False
    write_source(ws, "app", "demo_app", "kind: lab\n")
    assert "kind: lab" in path.read_text(encoding="utf-8")
