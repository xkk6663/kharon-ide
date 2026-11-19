"""P4 契约门禁：contracts/ 是唯一事实源，生成物必须与之一致。

判据（与 ``scripts/check-generated-contracts.mjs`` 等价，无需 Node 也能跑）：
- 生成物里的 ``CONTRACTS_HASH`` = events schema 的稳定摘要；
- ``EVENT_TOPICS / SYNTHETIC_TOPICS / CONTROL_TOPICS`` 与 ``cockpit.core`` 一致。

本仓只保留 standalone cockpit 的 HTTP/SSE 面，``elab_*`` 工具注册面随 DSH 剥离，
不再作为契约事实源。
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
#: 本仓只有 standalone cockpit 前端；DSH 生成物已随插件剥离。
GENERATED = [
    ROOT / "cockpit" / "web" / "src" / "api" / "generated" / "contracts.ts",
]


def _stable(value):
    """与 JS ``stable()`` 等价：键排序 + 紧凑 JSON。"""
    if isinstance(value, list):
        return "[" + ",".join(_stable(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _stable(value[k])
            for k in sorted(value)
        ) + "}"
    return json.dumps(value, ensure_ascii=False)


def _schemas():
    return json.loads((CONTRACTS / "events.schema.json").read_text(encoding="utf-8"))


def _expected_hash():
    events = _schemas()
    return hashlib.sha256(_stable(events).encode("utf-8")).hexdigest()


def _ts_list(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name} = \[(.*?)\] as const", text, re.S)
    if not m:
        raise AssertionError(f"生成物缺少 {name}")
    return re.findall(r"'([^']*)'", m.group(1))


class GeneratedContractsTest(unittest.TestCase):
    def test_generated_files_exist(self) -> None:
        for p in GENERATED:
            self.assertTrue(p.is_file(), f"缺少生成物 {p.relative_to(ROOT)}")

    def test_hash_matches_schemas(self) -> None:
        want = _expected_hash()
        for p in GENERATED:
            text = p.read_text(encoding="utf-8")
            self.assertIn(
                f"export const CONTRACTS_HASH = '{want}'", text,
                f"{p.relative_to(ROOT)} 已漂移，运行 node scripts/generate-contracts.mjs",
            )

    def test_event_topics_match_kernel(self) -> None:
        from cockpit import core  # noqa: PLC0415

        events = _schemas()
        topics = [t["topic"] for t in events["topics"]]
        self.assertEqual(set(topics), set(core.SSE_TOPICS))
        synthetic = [t["topic"] for t in events["topics"] if t["delivery"] != "log"]
        control = [t["topic"] for t in events["topics"] if t["delivery"] == "control"]
        self.assertEqual(set(synthetic), set(core.SYNTHETIC_TOPICS))
        self.assertEqual(set(control), set(core.CONTROL_TOPICS))
        for p in GENERATED:
            text = p.read_text(encoding="utf-8")
            self.assertEqual(_ts_list(text, "EVENT_TOPICS"), topics)
            self.assertEqual(_ts_list(text, "SYNTHETIC_TOPICS"), synthetic)
            self.assertEqual(_ts_list(text, "CONTROL_TOPICS"), control)


if __name__ == "__main__":
    unittest.main()
