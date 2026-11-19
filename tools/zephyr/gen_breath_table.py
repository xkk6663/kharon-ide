#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_breath_table.py —— 生成「同步呼吸灯」的亮度表、校验和与验收向量

规范 v1.0（数字都写死在这里，改规范 = 改这个脚本 = 所有设备重新生成一遍）：

    周期 P       = 4 000 000 us（4 s）
    相位锚点 A   = epoch 0（1970-01-01T00:00:00Z）
    相位         φ = (t - A) mod P
    归一化       x = φ / P ∈ [0, 1)
    波形         s = (1 - cos(2πx)) / 2     余弦，两端速度为零
    亮度校正     γ = 2.0                    s 先压 γ 再映射到 0..255
    表长         N = 256                    查表一步出结果
    表项         LUT[i] = round(255 * s(i/N)^γ)

为什么要有这个脚本：
  「一致性」的敌人是浮点。sin/cos 在 libm/newlib/FPU 上的最后一位可能不同，
  不同编译器开不开 FMA 也不同。所以规范不定义公式的“计算结果”，
  而是定义**一张表 + 一个校验和**：谁拿到的表字节一样，谁算出来的亮度就一样。

用法：
    python tools/gen_breath_table.py                 # 打印生成区段到 stdout
    python tools/gen_breath_table.py --inplace       # 就地更新 breath_sync.c
    python tools/gen_breath_table.py --json          # 机器可读（给 PC 端/JS 用）

生成区段在 breath_sync.c 里由下面两行标记包围，脚本只替换这两行之间的内容：
    /* BEGIN GENERATED ... */ ... /* END GENERATED */
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys

# ---------------------------------------------------------------- 规范常数
N = 256                 # 表长
GAMMA = 2.0             # 亮度校正指数
PERIOD_US = 4_000_000   # 4 s

BEGIN_MARK = "BEGIN GENERATED"
END_MARK = "END GENERATED"

# 验收向量：(utc_us, 期望 phase, 期望 level)
# phase 是 LUT 下标，level 是包络输出（0..255，未乘设备 gain）。
VECTORS = [
    (0, "epoch 0，相位 0"),
    (250_000, "x=1/16"),
    (500_000, "x=1/8"),
    (1_000_000, "x=1/4"),
    (1_500_000, "x=3/8"),
    (2_000_000, "x=1/2，峰值"),
    (2_500_000, "x=5/8"),
    (3_000_000, "x=3/4"),
    (3_500_000, "x=7/8"),
    (3_999_999, "周期最后一微秒"),
    (4_000_000, "整周期，应回绕到相位 0"),
    (1_735_689_600_000_000, "2025-01-01T00:00:00Z（epoch 后整 4s 倍数）"),
]


def level_at(i: int) -> int:
    """表项 i 的亮度值：余弦包络 + gamma，四舍五入（.5 向上）。"""
    s = (1.0 - math.cos(2.0 * math.pi * i / N)) / 2.0
    return int(math.floor(255.0 * (s ** GAMMA) + 0.5))


def lut() -> list[int]:
    return [level_at(i) for i in range(N)]


def phase_of(utc_us: int) -> int:
    """规范定义的相位下标，整数运算，与 C 实现逐字节可对齐。"""
    p = utc_us % PERIOD_US
    return (p * N) // PERIOD_US


def fnv1a32(data) -> int:
    """FNV-1a 32 位。选它是因为纯 32 位乘/异或，MCU 一条指令，且实现无歧义。"""
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def ascii_diagram(table: list[int]) -> str:
    """把表画成 41 列宽的 ASCII 曲线（首尾都落在相位 0，形状才看得准）。"""
    rows, cols = 10, 41
    grid = [[" "] * cols for _ in range(rows)]
    for c in range(cols):
        i = c * (N - 1) // (cols - 1)
        v = table[i]
        r = rows - 1 - (v * (rows - 1) + 254) // 255
        grid[r][c] = "*"
    lines = ["  255 |" + "".join(grid[0])]
    for r in range(1, rows):
        mark = "     |" if r != rows - 1 else "    0 |"
        lines.append(mark + "".join(grid[r]))
    lines.append("       +" + "-" * cols)
    lines.append("       0" + " " * (cols - 9) + "4 s（相位 φ）")
    return "\n".join(lines)


def render_c(table: list[int], checksum: int) -> str:
    lines = []
    lines.append(f"/* {BEGIN_MARK} BY tools/gen_breath_table.py —— 不要手改 */")
    lines.append("/*")
    lines.append(" * 规范 v1.0：P=4000000 us，锚点=epoch 0，波形=cos，(1-cos)/2，gamma=2.0，N=256")
    lines.append(" * LUT[i] = round(255 * ((1-cos(2πi/256))/2)^2)")
    lines.append(" * 表一变，规范就变了：所有设备必须同时换表，校验和必须对得上。")
    lines.append(" */")
    lines.append("")
    lines.append(f"#define BREATH_LUT_CHECKSUM 0x{checksum:08X}u")
    lines.append("")
    lines.append("#define BREATH_LUT_SIZE 256u")
    lines.append("")
    lines.append("static const uint8_t breath_lut[BREATH_LUT_SIZE] = {")
    for start in range(0, N, 12):
        chunk = ", ".join(f"{v:3d}" for v in table[start:start + 12])
        lines.append(f"\t{chunk},")
    lines.append("};")
    lines.append("")
    lines.append("/* 验收向量：self-test 与文档里的对表都用这张表，二者必须逐条相同 */")
    lines.append("const struct breath_vector breath_vectors[] = {")
    for utc, note in VECTORS:
        lines.append(f"\t{{ {utc:>16}ull, {phase_of(utc):3d}u, {table[phase_of(utc)]:3d}u }},"
                     f"  /* {note} */")
    lines.append("};")
    lines.append("")
    lines.append(f"const uint16_t breath_vector_count = {len(VECTORS)}u;")
    lines.append(f"/* {END_MARK} */")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成同步呼吸灯的 LUT / 校验和 / 验收向量")
    ap.add_argument("--inplace", action="store_true",
                    help="就地替换 breath_sync.c 里的生成区段")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（给非 C 设备抄表）")
    ap.add_argument("--diagram", action="store_true", help="额外打印 ASCII 波形图")
    ap.add_argument("--file", default=None,
                    help="--inplace 的目标文件，默认为 applications/common/src/breath_sync.c")
    args = ap.parse_args()

    table = lut()
    checksum = fnv1a32(table)

    if args.json:
        print(json.dumps({
            "spec": "v1.0",
            "period_us": PERIOD_US,
            "anchor_epoch_us": 0,
            "waveform": "cos",
            "gamma": GAMMA,
            "size": N,
            "checksum": f"0x{checksum:08X}",
            "lut": table,
            "vectors": [
                {"utc_us": u, "phase": phase_of(u), "level": table[phase_of(u)], "note": n}
                for u, n in VECTORS
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    body = render_c(table, checksum)

    if args.diagram:
        print(ascii_diagram(table))
        print()

    if not args.inplace:
        print(body)
        return 0

    root = pathlib.Path(__file__).resolve().parent.parent
    target = pathlib.Path(args.file) if args.file else \
        root / "apps" / "common" / "src" / "breath_sync.c"

    if not target.exists():
        print(f"目标不存在：{target}", file=sys.stderr)
        return 2

    text = target.read_text(encoding="utf-8")
    pat = re.compile(
        r"\s*/\* BEGIN GENERATED[\s\S]*?/\* END GENERATED \*/\n",
        re.MULTILINE,
    )
    if not pat.search(text):
        print(f"{target} 里没有 BEGIN/END GENERATED 标记，拒绝改写", file=sys.stderr)
        return 3

    new_text = pat.sub("\n" + body + "\n", text, count=1)
    target.write_text(new_text, encoding="utf-8", newline="\n")
    print(f"已写入 {target}")
    print(f"校验和 0x{checksum:08X}，表长 {N}，向量 {len(VECTORS)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
