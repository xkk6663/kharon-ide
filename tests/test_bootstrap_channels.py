"""bootstrap 双通道的纯逻辑测试（不联网、不碰真实 west 工作区）。"""

from __future__ import annotations

from pathlib import Path

from kharon import bootstrap


MANIFEST_DATA = {
    "zephyr": {
        "commit": "34e025569cbb27660b71db809627c3a7402e2111",
        "pinned_modules": ["hal_espressif", "hal_stm32", "cmsis_6",
                           "mbedtls", "tf-psa-crypto", "mcuboot"],
    },
    "projects": [
        {"name": "hal_espressif", "path": "modules/hal/espressif"},
        {"name": "hal_stm32", "path": "modules/hal/stm32"},
        {"name": "cmsis_6", "path": "modules/hal/cmsis_6"},
        {"name": "mbedtls", "path": "modules/crypto/mbedtls"},
        {"name": "tf-psa-crypto", "path": "modules/crypto/tf-psa-crypto"},
        {"name": "mcuboot", "path": "bootloader/mcuboot"},
        {"name": "extra_a", "path": "modules/extra/a"},
        {"name": "extra_b", "path": "modules/extra/b"},
        {"name": "extra_c", "path": "modules/extra/c"},
        {"name": "extra_d", "path": "modules/extra/d"},
        {"name": "optional_test", "path": "modules/test/optional", "optional": True},
    ],
}


def test_pinned_modules_reads_manifest_and_falls_back() -> None:
    assert bootstrap._pinned_modules(MANIFEST_DATA) == [
        "hal_espressif", "hal_stm32", "cmsis_6",
        "mbedtls", "tf-psa-crypto", "mcuboot",
    ]
    assert bootstrap._pinned_modules({"zephyr": {}}) == list(bootstrap.PINNED_FALLBACK)


def test_required_modules_pinned_and_learning(monkeypatch) -> None:
    pinned, label = bootstrap._required_modules(
        bootstrap.PINNED, MANIFEST_DATA, "py")
    assert label == "钉版本模块"
    assert [p["name"] for p in pinned] == [
        "hal_espressif", "hal_stm32", "cmsis_6",
        "mbedtls", "tf-psa-crypto", "mcuboot",
    ]

    monkeypatch.setattr(bootstrap, "_present_projects", lambda _py: [
        {"name": "hal_espressif", "path": "modules/hal/espressif"},
        {"name": "optional_test", "path": "modules/test/optional"},
    ])
    learning, label = bootstrap._required_modules(
        bootstrap.LEARNING, MANIFEST_DATA, "py")
    assert label == "模块"
    assert [p["name"] for p in learning] == ["hal_espressif"]


def test_infer_channel_from_disk(monkeypatch, tmp_path: Path) -> None:
    west = tmp_path / "west"
    monkeypatch.setattr(bootstrap, "WEST", west)
    monkeypatch.setattr(bootstrap, "MANIFEST", tmp_path / "zephyr.west.yml")
    monkeypatch.setattr(bootstrap, "_load_manifest", lambda: MANIFEST_DATA)

    assert bootstrap._infer_channel() == bootstrap.LEARNING

    (west / ".west").mkdir(parents=True)
    (west / ".west" / "config").write_text("", encoding="utf-8")
    for project in MANIFEST_DATA["projects"][:6]:
        (west / project["path"]).mkdir(parents=True, exist_ok=True)
    assert bootstrap._infer_channel() == bootstrap.PINNED

    (west / "modules" / "extra" / "a").mkdir(parents=True, exist_ok=True)
    (west / "modules" / "extra" / "b").mkdir(parents=True, exist_ok=True)
    (west / "modules" / "extra" / "c").mkdir(parents=True, exist_ok=True)
    (west / "modules" / "extra" / "d").mkdir(parents=True, exist_ok=True)
    assert bootstrap._infer_channel() == bootstrap.LEARNING


def test_state_round_trip(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / ".kharon" / "bootstrap.json"
    monkeypatch.setattr(bootstrap, "STATE", state)
    bootstrap._write_state(bootstrap.PINNED)
    assert bootstrap._state()["channel"] == bootstrap.PINNED
