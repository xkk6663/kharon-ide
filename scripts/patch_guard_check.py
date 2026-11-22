#!/usr/bin/env python3
"""补丁守卫：zephyr 树的脏改动必须**恰好等于** patches/zephyr 的补丁集。

为什么不能写成"``git status`` 必须为空"：``bootstrap`` 会幂等重放
``patches/zephyr/*.patch``，补丁 applied 之后树**必然**是脏的。守卫要判的是
"除了补丁改的那几个文件，有没有人（或某次 bootstrap）顺手把树改坏了"。

三条判据（对应《待办_模型C工作区入库》第 4 项）:

  ① 每个补丁都已应用 —— ``git apply --check --reverse <patch>`` 必须通过
     （reverse 能干净回退 = 正处在"已应用"状态）。
  ② 树内改动 ⊆ 补丁触及的文件集合 —— ``git diff HEAD --name-only`` 里出现的
     每个路径，都必须被某个补丁 ``diff --git a/... b/...`` 头命中；多出来的
     就是"有人手改了树"，直接失败（并打印路径）。
  ③ 清单哈希三处一致 —— ``kharon-manifest/west.yml`` 的
     ``manifest.projects[zephyr].revision``、同文件顶层 ``zephyr.commit``、
     ``.kharon/bootstrap.json`` 的 ``zephyr_commit`` 必须是同一个 commit。
     任一处漂移，说明"钉的基线"已经不再唯一。

用法:
    python scripts/patch_guard_check.py --root <ZEPHYR_ROOT> [--patch-dir <dir>] [--json]

    ``--root``       Zephyr 工作区根（默认 $ZEPHYR_ROOT）
    ``--patch-dir``  补丁目录（默认本仓 patches/zephyr）
    ``--json``       输出 JSON

退出码: 0 通过；1 有违规；2 参数/环境错误。

注意: 判据 ① ② 需要 ``<root>/zephyr`` 是 git 工作树；不是（或没装 git）时
这两条记为 "skipped"，**不影响** 判据 ③，也不把整体判成通过 —— 输出里会
明确写 ``skipped``，CI 据此决定是否接受。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

RE_DIFF_HEAD = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$", re.MULTILINE)


def run_git(cwd: Path, args: list[str]) -> tuple[int, str]:
    """跑一条 git 命令；git 缺席/超时都当作"环境不可用"返回 -1。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return proc.returncode, (proc.stdout or "").strip()


def collect_patched_files(patches: list[Path]) -> set[str]:
    """从补丁文件里直接读 ``diff --git`` 头，拿到被触及的路径集合。"""
    touched: set[str] = set()
    for p in patches:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in RE_DIFF_HEAD.finditer(text):
            # 取 b/ 侧；重命名补丁两侧都记上，避免误判
            touched.add(m.group(2).strip())
            touched.add(m.group(1).strip())
    return touched


def check_applied(zephyr: Path, patches: list[Path]) -> list[dict]:
    """判据 ①：每个补丁 reverse-apply 自检（= 当前处于已应用状态）。"""
    out: list[dict] = []
    for p in patches:
        rc, txt = run_git(zephyr, ["apply", "--check", "--reverse", str(p)])
        if rc < 0:
            out.append({"rule": "patch.git_unavailable", "path": p.name,
                        "msg": "git 不可用，无法自检补丁是否已应用"})
            continue
        if rc != 0:
            out.append({"rule": "patch.not_applied", "path": p.name,
                        "msg": "补丁未应用或已冲突（reverse --check 失败）：" + (txt.splitlines() or [""])[0]})
    return out


def check_tree_subset(zephyr: Path, patched: set[str]) -> list[dict]:
    """判据 ②：``git diff HEAD`` 的文件必须全部落在补丁触及集合内。"""
    rc, txt = run_git(zephyr, ["diff", "HEAD", "--name-only"])
    if rc < 0:
        return [{"rule": "tree.git_unavailable", "path": str(zephyr),
                 "msg": "git 不可用，无法读取树内改动"}]
    if rc != 0:
        return [{"rule": "tree.head_unreadable", "path": str(zephyr),
                 "msg": "git diff HEAD 失败（可能是空仓或 HEAD 不可解）"}]
    dirty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    out: list[dict] = []
    for rel in dirty:
        if rel not in patched:
            out.append({"rule": "tree.unexpected_edit", "path": rel,
                        "msg": "该文件被改动但不在任何补丁里；请手工确认并撤回，或补成正式补丁"})
    return out


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise SystemExit(f"[patch-guard] 缺少 PyYAML：{exc}") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover
        raise SystemExit(f"[patch-guard] 无法解析 {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def check_hash(root: Path) -> list[dict]:
    """判据 ③：三处 zephyr commit 必须一致。"""
    out: list[dict] = []
    west = root / "kharon-manifest" / "west.yml"
    boot = root / ".kharon" / "bootstrap.json"

    rev = ""
    commit = ""
    if west.is_file():
        data = _load_yaml(west)
        man = data.get("manifest") or {}
        for proj in (man.get("projects") or []):
            if isinstance(proj, dict) and proj.get("name") == "zephyr":
                rev = str(proj.get("revision") or "").strip()
                break
        commit = str((data.get("zephyr") or {}).get("commit") or "").strip()
    else:
        out.append({"rule": "hash.west_missing", "path": "kharon-manifest/west.yml",
                    "msg": "找不到清单文件，无法核对 revision"})

    boot_commit = ""
    if boot.is_file():
        try:
            payload = json.loads(boot.read_text(encoding="utf-8"))
        except Exception as exc:
            out.append({"rule": "hash.bootstrap_unreadable", "path": ".kharon/bootstrap.json",
                        "msg": f"无法解析：{exc}"})
            payload = {}
        boot_commit = str(payload.get("zephyr_commit") or "").strip()
    else:
        out.append({"rule": "hash.bootstrap_missing", "path": ".kharon/bootstrap.json",
                    "msg": "找不到 bootstrap 记录；跑一次 bootstrap 后再校验"})

    for label, value, rel in (
        ("west.yml manifest.projects[zephyr].revision", rev, "kharon-manifest/west.yml"),
        ("west.yml zephyr.commit", commit, "kharon-manifest/west.yml"),
        (".kharon/bootstrap.json zephyr_commit", boot_commit, ".kharon/bootstrap.json"),
    ):
        if not value:
            out.append({"rule": "hash.empty", "path": rel, "msg": f"{label} 是空的"})

    seen = [v for v in (rev, commit, boot_commit) if v]
    if len(seen) > 1 and len(set(seen)) > 1:
        out.append({"rule": "hash.mismatch", "path": "kharon-manifest/west.yml",
                    "msg": "三处 zephyr commit 不一致：revision={0} commit={1} bootstrap={2}".format(
                        rev or "-", commit or "-", boot_commit or "-")})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="补丁守卫（zephyr 树脏改动 == 补丁集）")
    ap.add_argument("--root", default=os.environ.get("ZEPHYR_ROOT", ""),
                    help="Zephyr 工作区根；默认 $ZEPHYR_ROOT")
    ap.add_argument("--patch-dir", default=str(Path(__file__).resolve().parent.parent / "patches" / "zephyr"),
                    help="补丁目录；默认 <本仓>/patches/zephyr")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not args.root:
        print("[patch-guard] 需要 --root 或 ZEPHYR_ROOT", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[patch-guard] 根目录不存在：{root}", file=sys.stderr)
        return 2

    patch_dir = Path(args.patch_dir).resolve()
    patches = sorted(patch_dir.glob("*.patch")) if patch_dir.is_dir() else []
    if not patches:
        print(f"[patch-guard] {patch_dir} 下没有 .patch；守卫无对象可判", file=sys.stderr)
        return 2

    zephyr = root / "zephyr"
    patched = collect_patched_files(patches)

    violations: list[dict] = []
    skipped: list[str] = []

    if (zephyr / ".git").exists():
        violations.extend(check_applied(zephyr, patches))
        violations.extend(check_tree_subset(zephyr, patched))
    else:
        skipped.append("① ② （<root>/zephyr 不是 git 工作树）")

    violations.extend(check_hash(root))

    if args.json:
        print(json.dumps({
            "root": str(root),
            "patch_dir": str(patch_dir),
            "patches": [p.name for p in patches],
            "patched_files": sorted(patched),
            "skipped": skipped,
            "violations": violations,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[patch-guard] root={root}")
        print(f"[patch-guard] patches={', '.join(p.name for p in patches)}")
        print(f"[patch-guard] 补丁触及 {len(patched)} 个文件")
        for s in skipped:
            print(f"[patch-guard] skipped {s}")
        if not violations:
            print("[patch-guard] OK：树内改动 == 补丁集，三处 commit 一致")
        else:
            print(f"[patch-guard] {len(violations)} 条违规：")
            for v in violations:
                print(f"  - [{v['rule']}] {v['path']}: {v['msg']}")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
