"""增量构建：有 CMakeCache 时 west 不再重跑 configure。"""

from __future__ import annotations

from pathlib import Path

from elab.platform.provider import StepDecl, _reuse_west_build


def _decl() -> StepDecl:
    return StepDecl(
        step="build", kind="external", label="编译（west build）",
        cmd=(
            "python.exe", "-m", "west", "build", "-b", "blackpill_f411ce",
            "applications/stm32f4/f411_dac_adc", "-d", "C:/ws/build/app",
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "--build-opt=-j8",
        ),
    )


def test_reuse_when_cmake_cache_exists(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    (work / "CMakeCache.txt").write_text("BOARD:STRING=blackpill_f411ce\n",
                                         encoding="utf-8")
    decl = _decl()
    got = _reuse_west_build({"build": {"work_dir": str(work)}}, decl)
    assert got.cmd == ("python.exe", "-m", "west", "build",
                       "-d", str(work), "--build-opt=-j8")
    assert got.label.endswith("增量")
    assert "-DCMAKE_C_COMPILER_LAUNCHER=ccache" not in got.cmd


def test_first_build_keeps_configure_args(tmp_path):
    work = tmp_path / "build"
    got = _reuse_west_build({"build": {"work_dir": str(work)}}, _decl())
    assert got.cmd == _decl().cmd
    assert "增量" not in got.label


def test_non_west_step_untouched(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    (work / "CMakeCache.txt").write_text("x\n", encoding="utf-8")
    decl = StepDecl(step="flash", kind="external", label="烧录",
                    cmd=("openocd", "-f", "board.cfg"))
    assert _reuse_west_build({"build": {"work_dir": str(work)}}, decl) is decl
