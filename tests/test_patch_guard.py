"""`scripts/patch_guard_check.py` 的可执行夹具。

为什么需要它：补丁守卫的核心是"zephyr 树脏改动 ⊆ 补丁集"，而 CI 里没有 Zephyr
工作区。这里用临时根 + 真实 ``git`` 造三种树（已应用 / 未应用 / 额外手改），
让三条判据都真的被执行到，而不是只靠本地人工跑。

夹具里的 zephyr 仓只有 ``a.txt`` 一个文件，补丁用真实 ``git diff`` 生成，
所以 ``git apply --check --reverse`` 的语义与线上完全一致。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "patch_guard_check.py"
COMMIT = "25851fa15cdc2dc9c395807bce97fa81c46c10ee"


def _load_module():
    spec = importlib.util.spec_from_file_location("patch_guard_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def _make_ws(tmp: Path, *, apply_patch: bool = True, extra_edit: bool = False,
             boot_commit: str = COMMIT) -> tuple[Path, Path]:
    """造一个最小工作区，返回 (workspace, patch_file)。"""
    zephyr = tmp / "zephyr"
    zephyr.mkdir(parents=True)
    _git(zephyr, "init", "-b", "main")
    _git(zephyr, "config", "user.email", "t@example.com")
    _git(zephyr, "config", "user.name", "t")
    (zephyr / "a.txt").write_text("old\n", encoding="utf-8")
    (zephyr / "b.txt").write_text("keep\n", encoding="utf-8")
    _git(zephyr, "add", ".")
    _git(zephyr, "commit", "-m", "base")

    # 用真实 git 造补丁：改 → diff → 还原
    (zephyr / "a.txt").write_text("new\n", encoding="utf-8")
    diff = _git(zephyr, "diff", "HEAD")
    assert diff.returncode == 0
    patch_dir = tmp / "patches"
    patch_dir.mkdir()
    patch = patch_dir / "0001-a.patch"
    patch.write_text(diff.stdout, encoding="utf-8")
    _git(zephyr, "checkout", "--", "a.txt")

    if apply_patch:
        assert _git(zephyr, "apply", str(patch)).returncode == 0
    if extra_edit:
        (zephyr / "b.txt").write_text("tampered\n", encoding="utf-8")

    man = tmp / "kharon-manifest"
    man.mkdir()
    (man / "west.yml").write_text(
        "manifest:\n"
        '  version: "0.13"\n'
        "  projects:\n"
        "    - name: zephyr\n"
        "      url: https://example.com/zephyr\n"
        "      path: zephyr\n"
        f"      revision: {COMMIT}\n"
        "zephyr:\n"
        f"  commit: {COMMIT}\n",
        encoding="utf-8",
    )
    kharon = tmp / ".kharon"
    kharon.mkdir()
    (kharon / "bootstrap.json").write_text(
        json.dumps({"zephyr_commit": boot_commit}), encoding="utf-8")
    return tmp, patch_dir


@pytest.fixture()
def guard():
    pytest.importorskip("yaml", reason="补丁守卫解析 west.yml 需要 PyYAML")
    return _load_module()


def test_clean_patched_tree_passes(guard, tmp_path, capsys):
    ws, pdir = _make_ws(tmp_path)
    assert guard.main(["--root", str(ws), "--patch-dir", str(pdir)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_unapplied_patch_is_reported(guard, tmp_path, capsys):
    ws, pdir = _make_ws(tmp_path, apply_patch=False)
    assert guard.main(["--root", str(ws), "--patch-dir", str(pdir)]) == 1
    assert "patch.not_applied" in capsys.readouterr().out


def test_extra_hand_edit_is_reported(guard, tmp_path, capsys):
    ws, pdir = _make_ws(tmp_path, extra_edit=True)
    assert guard.main(["--root", str(ws), "--patch-dir", str(pdir)]) == 1
    out = capsys.readouterr().out
    assert "tree.unexpected_edit" in out
    assert "b.txt" in out


def test_hash_mismatch_is_reported(guard, tmp_path, capsys):
    ws, pdir = _make_ws(tmp_path, boot_commit="deadbeef")
    assert guard.main(["--root", str(ws), "--patch-dir", str(pdir)]) == 1
    assert "hash.mismatch" in capsys.readouterr().out


def test_missing_patch_dir_is_env_error(guard, tmp_path):
    ws, _ = _make_ws(tmp_path)
    assert guard.main(["--root", str(ws), "--patch-dir", str(tmp_path / "nope")]) == 2
