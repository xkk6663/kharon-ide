"""test_api_snapshots —— HTTP / CLI 输出**形状**的金标准快照（P0 安全网）。

运行：``PYTHONPATH=services python -m unittest discover -s tests/contract -p "test_*.py"``

生成 / 更新基线（形状变化必须是**有意**的）：

    $env:ELAB_UPDATE_BASELINES=1; python -m unittest tests.contract.test_api_snapshots

为什么快照是"形状"而不是原值：
  - 原值里混有本机绝对路径、串口枚举结果、版本号与工具链状态，换机器必红；
  - 架构重构真正要防的是**字段被删除 / 改名 / 改类型**（方案 §4-P0 第 2 条），
    形状快照能精确捕获它，同时不把环境噪声变成假红灯。

覆盖：
  1. ``GET /api/capabilities`` → ``server.capabilities()``
  2. ``GET /api/projects``     → ``server.project_cards()``
  3. ``GET /api/plan``         → ``run.preview()``
  4. ``elab list --json``      → CLI 子进程
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "baseline"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))

UPDATE = os.environ.get("ELAB_UPDATE_BASELINES", "").strip() not in ("", "0", "false")

#: 本仓默认主机 profile（没有 classic 了）；只在快照类内设置，避免污染其它测试的临时 elab.host.yaml。
SNAPSHOT_PROFILE = "kharon"

#: CI 与本地都随仓可得的工程（Zephyr f411 基线），用于 plan 快照。
SNAPSHOT_PROJECT = "f411_breath"


def shape(value):
    """把任意 JSON 值折叠成结构骨架。

    - dict：保留全部键（删除/改名会立刻体现）；
    - list：字典元素取**键并集**（可选字段也不丢），标量元素取首项类型；
    - 标量：只保留类型标记。
    """
    if isinstance(value, dict):
        return {k: shape(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(x, dict) for x in value):
            merged: dict[str, object] = {}
            for item in value:
                for key in item:
                    merged.setdefault(key, shape(item[key]))
            return [{k: merged[k] for k in sorted(merged)}]
        return [shape(value[0])]
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, str):
        return "<str>"
    if isinstance(value, (int, float)):
        return "<num>"
    return None


def stable(payload):
    """屏蔽**清单型**环境噪声，只保留契约字段。

    - ``serial.ports``：本机串口枚举结果（CI 无串口 = 空）；
    - ``packs.installed``：本机已安装的适配包（干净 clone = 空）。

    这两个数组的元素 schema 由各自单元测试覆盖；这里关心的是
    "字段有没有被删/改名"，不是"这台机器插了什么"。
    """
    out = copy.deepcopy(payload)
    serial = out.get("serial")
    if isinstance(serial, dict):
        serial["ports"] = []
    packs = out.get("packs")
    if isinstance(packs, dict):
        packs["installed"] = []
        packs["chips"] = []
    return out


class ApiSnapshotTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._old_host = os.environ.get("ELAB_HOST")
        os.environ["ELAB_HOST"] = SNAPSHOT_PROFILE

    def tearDown(self) -> None:
        if self._old_host is None:
            os.environ.pop("ELAB_HOST", None)
        else:
            os.environ["ELAB_HOST"] = self._old_host

    def _assert_snapshot(self, name: str, payload, source: str) -> None:
        BASELINE.mkdir(parents=True, exist_ok=True)
        path = BASELINE / f"{name}.json"
        current = {"_meta": {"source": source, "project": SNAPSHOT_PROJECT},
                   "shape": shape(payload)}
        if UPDATE:
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            self.skipTest(f"已更新基线：{path.name}")
        self.assertTrue(path.exists(),
                        f"缺少基线 {path.name}：先运行 ELAB_UPDATE_BASELINES=1 生成并入库")
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(current["shape"], expected.get("shape"),
                         f"{name} 输出形状与基线不一致（字段被删/改名/改类型？）")

    # ── HTTP 读口 ─────────────────────────────────────────────
    def test_capabilities_shape(self) -> None:
        import cockpit.server as server
        from elab.config import Config

        cfg = Config()
        mgr = server.RunManager(cfg)
        self._assert_snapshot("api_capabilities",
                              stable(server.capabilities(cfg, mgr)),
                              "GET /api/capabilities")

    def test_projects_shape(self) -> None:
        """只锁定 CI 随仓可得的那个工程条目（其余工程依赖外部适配包）。"""
        import cockpit.server as server
        from elab.config import Config

        cards = server.project_cards(Config())
        payload = {
            "steps": cards.get("steps"),
            "projects": [c for c in cards.get("projects") or []
                         if c.get("name") == SNAPSHOT_PROJECT],
        }
        self._assert_snapshot("api_projects", payload, "GET /api/projects")

    def test_plan_shape(self) -> None:
        from elab import run as run_mod
        from elab.config import Config

        payload = run_mod.preview(Config(), SNAPSHOT_PROJECT)
        self._assert_snapshot("api_plan", payload,
                              f"GET /api/plan?project={SNAPSHOT_PROJECT}")

    # ── CLI ───────────────────────────────────────────────────
    def test_cli_list_shape(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "services"), env.get("PYTHONPATH", "")]).strip(os.pathsep)
        proc = subprocess.run(
            [sys.executable, "-m", "kharon", "list", "--json"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        # 本机已安装的适配包不属于契约形状：CI 干净 clone 时这里是空的。
        payload["chips"] = []
        self._assert_snapshot("cli_list", payload, "kharon list --json")


if __name__ == "__main__":
    unittest.main()
