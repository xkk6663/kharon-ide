/**
 * AUTO-GENERATED FILE — DO NOT EDIT.
 *
 * Source: contracts/events.schema.json
 * Regenerate: node scripts/generate-contracts.mjs
 * Guard:      node scripts/check-generated-contracts.mjs
 */
export const CONTRACTS_VERSION = 1
export const CONTRACTS_HASH = '0b0caf32d209b3ce9179f83559225431ea240f981ec5a03ad5ad71657404d602'

/** 全部事件 topic（SSE / .proc.jsonl 共用）。 */
export const EVENT_TOPICS = [
  'run/start',
  'run/step-enter',
  'run/step-exit',
  'run/end',
  'run/cancel',
  'proc/stdout',
  'proc/stderr',
  'proc/stdout-batch',
  'proc/exit',
  'serial/open',
  'serial/line',
  'serial/close',
  'serial/error',
  'serial/closed-loop',
  'debug/opened',
  'debug/state',
  'debug/stop',
  'debug/breakpoint',
  'debug/output',
  'debug/error',
  'debug/closed',
  'stream/overrun',
  'stream/closed'
] as const
export type EventTopic = (typeof EVENT_TOPICS)[number]

/** 服务端合成、不落盘的 topic。 */
export const SYNTHETIC_TOPICS = [
  'stream/overrun',
  'stream/closed'
] as const

/** 绝不可丢的控制类 topic（stream/closed）。 */
export const CONTROL_TOPICS = [
  'stream/closed'
] as const
