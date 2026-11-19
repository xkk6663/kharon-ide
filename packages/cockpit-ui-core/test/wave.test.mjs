import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  autoRange,
  decimateIndices,
  fmtDuration,
  fmtRate,
  fmtValue,
  measure,
  nearestFrameIndex,
  snapshotJson,
  valueToY,
} from '../src/index.ts'

test('autoRange：忽略无效值、常量通道留白、空数组返回 null', () => {
  assert.deepEqual(autoRange([1, 3, NaN, Infinity, -2]), { lo: -2, hi: 3 })
  const flat = autoRange([5, 5])
  assert.ok(flat)
  assert.ok(flat.lo < 5 && flat.hi > 5)
  assert.equal(autoRange([]), null)
  assert.equal(autoRange([NaN, Infinity]), null)
})

test('valueToY：顶部=0、底部=height；gain 压扁幅度、offset 整体上移', () => {
  // lo=0 hi=10，height=100：v=10 → 顶 0，v=0 → 底 100
  assert.equal(valueToY(10, 0, 10, 100), 0)
  assert.equal(valueToY(0, 0, 10, 100), 100)
  assert.equal(valueToY(5, 0, 10, 100), 50)
  // gain=0.5 → v=10 变成 5 → 中点
  assert.equal(valueToY(10, 0, 10, 100, 0.5), 50)
  // offset=5 → v=0 变成 5 → 中点
  assert.equal(valueToY(0, 0, 10, 100, 1, 5), 50)
  // 常量量程不除零（返回中线）
  assert.equal(valueToY(7, 5, 5, 100), 50)
})

test('nearestFrameIndex：吸附 + 越界夹紧', () => {
  assert.equal(nearestFrameIndex(0, 10), 0)
  assert.equal(nearestFrameIndex(1, 10), 9)
  assert.equal(nearestFrameIndex(0.44, 10), 4)
  assert.equal(nearestFrameIndex(-0.2, 10), 0)
  assert.equal(nearestFrameIndex(1.3, 10), 9)
  assert.equal(nearestFrameIndex(0.5, 0), 0)
})

test('measure：Δ帧 / Δt / ΔY（原始与缩放）', () => {
  const a = { index: 0, t_us: 1_000_000, values: [1, 10] }
  const b = { index: 10, t_us: 1_100_000, values: [3, 4] }
  const r = measure(a, b, [0.5, 2], [0, 0])
  assert.equal(r.d_frames, 10)
  assert.equal(r.dt_us, 100_000)
  assert.deepEqual(r.dy, [2, -6])
  assert.deepEqual(r.dy_scaled, [1, -12])
  // 无时间源 → dt_us=null（不编造）
  assert.equal(measure({ index: 1, values: [1] }, { index: 2, values: [2] }).dt_us, null)
  // 缺值通道给 NaN，不误报
  assert.ok(Number.isNaN(measure({ index: 0, values: [1] }, { index: 1, values: [] }).dy[0]))
})

test('fmtDuration / fmtValue', () => {
  assert.equal(fmtDuration(null), '—')
  assert.equal(fmtDuration(100), '100 μs')
  assert.equal(fmtDuration(100_000), '100 ms')
  assert.equal(fmtDuration(2_500_000), '2.5 s')
  assert.equal(fmtDuration(-500), '-500 μs')
  assert.equal(fmtValue(0.1 + 0.2), '0.3')
  assert.equal(fmtValue(NaN), '—')
})

test('snapshotJson：稳定结构（视口 + 游标 + 通道）', () => {
  const s = JSON.parse(
    snapshotJson({
      frozen: true,
      scaleX: 2,
      offsetX: 10,
      frameCount: 100,
      cursors: { a: { index: 3, t_us: 1, values: [1] }, b: null },
      channels: [{ index: 0, label: 'rpm', unit: 'RPM', gain: 1, offset: 0 }],
      takenAt: '2026-11-04T00:00:00.000Z',
    }),
  )
  assert.equal(s.kind, 'elab-wave-snapshot')
  assert.equal(s.view.scale_x, 2)
  assert.equal(s.cursors.a.i, 3)
  assert.equal(s.cursors.b, null)
  assert.equal(s.channels[0].label, 'rpm')
})

test('fmtRate：Δt 换算频率，自动选单位；零/无值给空串', () => {
  assert.equal(fmtRate(1_000), '1 kHz')
  assert.equal(fmtRate(500), '2 kHz')
  assert.equal(fmtRate(20), '50 kHz')
  assert.equal(fmtRate(10_000_000), '0.1 Hz')
  assert.equal(fmtRate(1_000_000), '1 Hz')
  // 负的 Δt（游标顺序反过来）按绝对值算，频率没有方向
  assert.equal(fmtRate(-1_000), '1 kHz')
  assert.equal(fmtRate(0), '')
  assert.equal(fmtRate(null), '')
})

test('decimateIndices：点数少于桶数×2 时原样返回', () => {
  assert.deepEqual(decimateIndices([], 10), [])
  assert.deepEqual(decimateIndices([1], 10), [0])
  const small = [3, 1, 2]
  assert.deepEqual(decimateIndices(small, 10), [0, 1, 2])
  // 边界：n = 2*buckets 也算"不多"，不抽
  const edge = new Array(20).fill(0).map((_, i) => i)
  assert.equal(decimateIndices(edge, 10).length, 20)
})

test('decimateIndices：每桶保 min+max，尖峰不被平均掉', () => {
  // 10000 点里只有第 5001 点是 100，其余 0 —— 100 必须活下来
  const values = new Array(10_000).fill(0)
  values[5_000] = 100
  const idx = decimateIndices(values, 100)
  assert.ok(idx.length <= 202, `抽稀后点数应 ≤ 2×桶数，实际 ${idx.length}`)
  assert.ok(idx.includes(5_000), '尖峰点必须保留')
  // 下标严格递增（保证 x 单调，不会画锯齿）
  for (let i = 1; i < idx.length; i++) assert.ok(idx[i] > idx[i - 1])
  // 末点必留
  assert.equal(idx[idx.length - 1], 9_999)
})

test('decimateIndices：谷值也保留（min 与 max 同等重要）', () => {
  const values = new Array(4_000).fill(5)
  values[1_234] = -50
  const idx = decimateIndices(values, 50)
  assert.ok(idx.includes(1_234), '负向毛刺不能被抽掉')
})

test('decimateIndices：无效值是断线信号，每桶保留首个', () => {
  // 前 200 帧有效、中间 200 帧 NaN、后面 200 帧有效
  const values = [
    ...new Array(200).fill(1),
    ...new Array(200).fill(NaN),
    ...new Array(200).fill(2),
  ]
  const idx = decimateIndices(values, 20)
  const pickedNaN = idx.filter((i) => !Number.isFinite(values[i]))
  assert.ok(pickedNaN.length >= 1, '断线处必须留下至少一个无效点')
  // NaN 段起点被选中（断点位置准确）
  assert.ok(idx.includes(200), 'NaN 段起点应保留')
})

test('decimateIndices：桶数非法取值被夹到 ≥1（最少留首尾，不崩溃）', () => {
  const values = [1, 2, 3, 4, 5]
  for (const buckets of [0, -3]) {
    const idx = decimateIndices(values, buckets)
    assert.equal(idx[0], 0)
    assert.equal(idx[idx.length - 1], 4)
    for (let i = 1; i < idx.length; i++) assert.ok(idx[i] > idx[i - 1])
  }
})
