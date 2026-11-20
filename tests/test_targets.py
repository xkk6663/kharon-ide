import textwrap
from pathlib import Path

import pytest

from elab.core.config import Config, ElabError


def _write(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "products" / "kharon"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("KHARON_PRODUCTS_DIR", str(tmp_path / "products"))
    _write(tmp_path, "single", """
        name: single
        chip: only_chip
        root: ${ZEPHYR_ROOT}/applications/single
        build: {kind: cmake, work_dir: ${ZEPHYR_ROOT}/build/only}
    """)
    _write(tmp_path, "multi", """
        name: multi
        root: ${ZEPHYR_ROOT}/applications/multi
        build: {kind: cmake, source: ${root}/CMakeLists.txt}
        targets:
          - id: s31
            chip: chip_a
            build: {work_dir: ${ZEPHYR_ROOT}/build/a}
          - id: s3
            chip: chip_b
            build: {work_dir: ${ZEPHYR_ROOT}/build/b}
    """)
    _write(tmp_path, "conflict", """
        name: conflict
        chip: chip_a
        targets:
          - id: a
            chip: chip_b
    """)
    _write(tmp_path, "dup", """
        name: dup
        targets:
          - id: same
            chip: chip_a
          - id: same
            chip: chip_b
    """)
    return Config(root=tmp_path)


def test_legacy_single_chip_is_unchanged(cfg):
    p = cfg.resolved_project("single")
    assert p["chip"] == "only_chip"
    assert p["build"]["work_dir"].endswith("/build/only")


def test_multi_target_requires_choice(cfg):
    with pytest.raises(ElabError, match="请用 --target"):
        cfg.resolved_project("multi")


def test_target_merges_chip_and_work_dir(cfg):
    p = cfg.resolved_project("multi", "s3")
    assert p["chip"] == "chip_b"
    assert p["_target_id"] == "s3"
    assert p["build"]["kind"] == "cmake"          # 顶层继承
    assert p["build"]["work_dir"].endswith("/build/b")
    assert p["build"]["source"].endswith("/CMakeLists.txt")


def test_select_target_is_sticky(cfg):
    assert cfg.select_target("multi", "s31") == "s31"
    p = cfg.resolved_project("multi")
    assert p["chip"] == "chip_a"
    assert cfg.select_target("single") is None


def test_unknown_target_lists_candidates(cfg):
    with pytest.raises(ElabError, match="s31, s3"):
        cfg.select_target("multi", "nope")


def test_top_chip_must_match_first_target(cfg):
    with pytest.raises(ElabError, match="冲突"):
        cfg.resolved_project("conflict", "a")


def test_duplicate_target_id_rejected(cfg):
    with pytest.raises(ElabError, match="重复"):
        cfg.resolved_project("dup", "same")
