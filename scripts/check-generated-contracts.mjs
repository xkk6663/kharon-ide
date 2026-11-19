#!/usr/bin/env node
/**
 * CI 漂移守卫：contracts/ 改了但没重新生成 → 失败。
 *
 * 判据 = 生成结果与入库文件**逐字节一致**（不接受"看起来差不多"）。
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildArtifacts } from './generate-contracts.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUTPUTS = [
  'cockpit/web/src/api/generated/contracts.ts',
]

const { text, hash } = buildArtifacts()
let bad = 0
for (const rel of OUTPUTS) {
  let actual = ''
  try {
    actual = readFileSync(resolve(ROOT, rel), 'utf8')
  } catch {
    console.error(`✗ 缺少生成物：${rel}`)
    bad++
    continue
  }
  if (actual !== text) {
    console.error(`✗ 生成物与 contracts/ 不一致：${rel}`)
    console.error('  修复：node scripts/generate-contracts.mjs')
    bad++
  }
}

if (bad) process.exit(1)
console.log(`✓ contracts 生成物一致（hash ${hash.slice(0, 12)}…）`)
