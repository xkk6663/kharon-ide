#!/usr/bin/env node
/**
 * contracts → TypeScript 生成器（P4：消除前端 ICD 双镜像）。
 *
 * 事实源：
 *   contracts/events.schema.json   —— SSE / 事件日志的 topic 与信封
 *
 * 生成物（入库，禁止手工修改）：
 *   cockpit/web/src/api/generated/contracts.ts
 *
 * 用法：
 *   node scripts/generate-contracts.mjs            # 写入
 *   node scripts/check-generated-contracts.mjs     # CI 漂移检查
 */
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const OUTPUTS = [
  'cockpit/web/src/api/generated/contracts.ts',
]

/** 键排序后的紧凑 JSON —— 与 Python 侧 json.dumps(sort_keys=True, separators=(',', ':')) 等价。 */
function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']'
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort()
    return '{' + keys.map((k) => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}'
  }
  return JSON.stringify(value)
}

function readSchema(name) {
  return JSON.parse(readFileSync(resolve(ROOT, 'contracts', name), 'utf8'))
}

function list(items, indent = '  ') {
  return items.map((x) => "'" + String(x).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'").map((x) => indent + x).join(',\n')
}

/** 构建生成物内容（不落盘，供 check 脚本复用）。 */
export function buildArtifacts() {
  const events = readSchema('events.schema.json')

  const hash = createHash('sha256')
    .update(stable(events), 'utf8')
    .digest('hex')

  const topics = events.topics.map((t) => t.topic)
  const synthetic = events.topics.filter((t) => t.delivery !== 'log').map((t) => t.topic)
  const control = events.topics.filter((t) => t.delivery === 'control').map((t) => t.topic)

  const text = `/**
 * AUTO-GENERATED FILE — DO NOT EDIT.
 *
 * Source: contracts/events.schema.json
 * Regenerate: node scripts/generate-contracts.mjs
 * Guard:      node scripts/check-generated-contracts.mjs
 */
export const CONTRACTS_VERSION = ${events.version}
export const CONTRACTS_HASH = '${hash}'

/** 全部事件 topic（SSE / .proc.jsonl 共用）。 */
export const EVENT_TOPICS = [
${list(topics)}
] as const
export type EventTopic = (typeof EVENT_TOPICS)[number]

/** 服务端合成、不落盘的 topic。 */
export const SYNTHETIC_TOPICS = [
${list(synthetic)}
] as const

/** 绝不可丢的控制类 topic（stream/closed）。 */
export const CONTROL_TOPICS = [
${list(control)}
] as const
`
  return { hash, text, events }
}

export function writeArtifacts() {
  const { text, hash } = buildArtifacts()
  for (const rel of OUTPUTS) {
    const path = resolve(ROOT, rel)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, text, 'utf8')
  }
  return hash
}

const invoked = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (invoked) {
  const hash = writeArtifacts()
  console.log(`contracts generated (hash ${hash.slice(0, 12)}…)`)
  for (const rel of OUTPUTS) console.log('  -', rel)
}
