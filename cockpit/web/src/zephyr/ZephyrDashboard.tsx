/**
 * 构建面板（Zephyr Dashboard）：内存旭日图 / Kconfig / 设备树。
 *
 * 对标 VS Code 的「IDE for Zephyr」Build Dashboard，但数据口径不同：
 * 那边由插件自己解析构建产物，这边一律走内核只读端点
 * （`/api/zephyr/{footprint,kconfig,devicetree}`），**面板不解析任何文件**。
 *
 * 为什么需要这一页（方案 §4.2）：`west` 本身完全不给视图 —— 想知道
 * 「RAM 这 5.5K 被谁吃了」「我改的 overlay 到底生效没有」，终端里只能翻
 * map 文件与展开后的 dts。这两件事恰好是驾驶舱能提供、而 west 不给的。
 */

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { FpNode } from "../api/client";
import { Input } from "../primitives/Input";
import { SegmentedTabs, type SegTab } from "../primitives/SegmentedTabs";
import { RunnerView } from "./RunnerView";
import s from "./ZephyrDashboard.module.css";

type Tab = "mem" | "kconfig" | "dts" | "runner";

interface FpTarget {
  ok: boolean;
  path: string;
  total?: number;
  truncated?: boolean;
  root?: FpNode;
}

export function ZephyrDashboard({ project }: { project: string | null }) {
  const [tab, setTab] = useState<Tab>("mem");
  const tabs: Array<SegTab<Tab>> = [
    { id: "mem", label: "内存" },
    { id: "kconfig", label: "Kconfig" },
    { id: "dts", label: "设备树" },
    { id: "runner", label: "调试器" },
  ];

  if (!project) {
    return <div className={s.note}>先在左栏选一个 Zephyr 工程。</div>;
  }
  return (
    <div className={s.wrap}>
      <div className={s.head}>
        <SegmentedTabs tabs={tabs} value={tab} onChange={setTab} />
        <span className={s.hint}>数据全部来自构建产物；未构建时为空。</span>
      </div>
      {tab === "mem" ? <MemoryView project={project} /> : null}
      {tab === "kconfig" ? <KconfigView project={project} /> : null}
      {tab === "dts" ? <DtsView project={project} /> : null}
      {tab === "runner" ? <RunnerView project={project} /> : null}
    </div>
  );
}

/* ── 内存：旭日图 ─────────────────────────────────────────────── */

interface Seg {
  a0: number;
  a1: number;
  depth: number;
  name: string;
  size: number;
  key: string;
  idx: number;
  ghost?: boolean;
  /** 叶子：扇区一直延伸到最外圈，否则外围会空出一块像破洞 */
  leaf?: boolean;
  /** 原始节点（点击下钻用） */
  node?: FpNode;
}

/** 层级树 → 扇区序列（角度按 size 比例分配）。 */
function layoutSegments(root: FpNode, maxDepth: number): Seg[] {
  const out: Seg[] = [];
  let idx = 0;
  const walk = (node: FpNode, a0: number, a1: number, depth: number, key: string) => {
    if (depth >= maxDepth) return;
    const kids = node.children ?? [];
    if (kids.length === 0) return;
    const sum = kids.reduce((acc, k) => acc + (k.size || 0), 0);
    // 父节点 size 可能大于子节点之和（未归因的填充/对齐），补一段"未归因"，
    // 否则扇区只覆盖一圈的一部分，看上去像渲染坏了。
    const total = Math.max(node.size || 0, sum) || 1;
    const per = (a1 - a0) / total;
    let cur = a0;
    for (const kid of kids) {
      const span = (kid.size || 0) * per;
      const kk = `${key}/${kid.name}`;
      out.push({
        a0: cur, a1: cur + span, depth, name: kid.name,
        size: kid.size || 0, key: kk, idx: idx++,
        leaf: (kid.children ?? []).length === 0,
        node: kid,
      });
      walk(kid, cur, cur + span, depth + 1, kk);
      cur += span;
    }
    if (sum < total - 1) {
      out.push({
        a0: cur, a1, depth, name: "(未归因)", size: total - sum,
        key: `${key}/(未归因)`, idx: idx++, ghost: true,
      });
    }
  };
  walk(root, 0, Math.PI * 2, 0, "");
  return out;
}

function arcPath(
  cx: number, cy: number, r0: number, r1: number, a0: number, a1: number,
): string {
  const span = a1 - a0;
  const aEnd = span >= Math.PI * 2 - 1e-6 ? a1 - 1e-6 : a1;
  const large = aEnd - a0 > Math.PI ? 1 : 0;
  const px = (r: number, a: number) => cx + r * Math.cos(a);
  const py = (r: number, a: number) => cy + r * Math.sin(a);
  return [
    `M ${px(r0, a0).toFixed(2)} ${py(r0, a0).toFixed(2)}`,
    `L ${px(r1, a0).toFixed(2)} ${py(r1, a0).toFixed(2)}`,
    `A ${r1} ${r1} 0 ${large} 1 ${px(r1, aEnd).toFixed(2)} ${py(r1, aEnd).toFixed(2)}`,
    `L ${px(r0, aEnd).toFixed(2)} ${py(r0, aEnd).toFixed(2)}`,
    `A ${r0} ${r0} 0 ${large} 0 ${px(r0, a0).toFixed(2)} ${py(r0, a0).toFixed(2)}`,
    "Z",
  ].join(" ");
}

/** 扇区配色：色相按序号黄金角散开，亮度随层深下降（层级感来自明度）。 */
function segFill(seg: Seg): string {
  if (seg.ghost) return "var(--elab-border)";
  const hue = (seg.idx * 137.508) % 360;
  const light = Math.max(34, 64 - seg.depth * 8);
  const sat = Math.max(28, 60 - seg.depth * 8);
  return `hsl(${hue.toFixed(0)}, ${sat}%, ${light}%)`;
}

function human(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1048576).toFixed(2)} MiB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${n} B`;
}

const SUN_SIZE = 440;
const SUN_C = SUN_SIZE / 2;
const SUN_R = 200;

function Sunburst({
  root, maxDepth, onZoom,
}: {
  root: FpNode;
  maxDepth: number;
  onZoom: (node: FpNode) => void;
}) {
  const [hover, setHover] = useState<Seg | null>(null);
  const segs = useMemo(() => layoutSegments(root, maxDepth), [root, maxDepth]);
  const ring = SUN_R / maxDepth;
  const total = root.size || 1;

  return (
    <div className={s.sunWrap}>
      <svg
        className={s.sun}
        viewBox={`0 0 ${SUN_SIZE} ${SUN_SIZE}`}
        role="img"
        aria-label="内存用量旭日图"
      >
        {segs.map((seg) => (
          <path
            key={seg.key}
            d={arcPath(
              SUN_C, SUN_C,
              seg.depth * ring,
              // 叶子延伸到最外圈；非叶子留 0.8px 描边缝隙让环层分明
              seg.leaf ? SUN_R : (seg.depth + 1) * ring - 0.8,
              seg.a0, seg.a1,
            )}
            fill={segFill(seg)}
            fillOpacity={hover && hover.key !== seg.key ? 0.42 : 0.92}
            stroke="var(--elab-bg-raised)"
            strokeWidth={0.6}
            onMouseEnter={() => setHover(seg)}
            onMouseLeave={() => setHover(null)}
            onClick={() => { if (seg.node) onZoom(seg.node); }}
            style={{ cursor: seg.node ? "pointer" : "default" }}
          >
            <title>{`${seg.name} · ${human(seg.size)}（${((seg.size / total) * 100).toFixed(2)}%）`}</title>
          </path>
        ))}
        <text x={SUN_C} y={SUN_C - 4} textAnchor="middle" className={s.sunTotal}>
          {human(total)}
        </text>
        <text x={SUN_C} y={SUN_C + 16} textAnchor="middle" className={s.sunCap}>
          {hover ? `${((hover.size / total) * 100).toFixed(1)}%` : "总量"}
        </text>
      </svg>
      <div className={s.sunLegend}>
        <div className={s.sunHoverName}>{hover ? hover.name : "悬停扇区看明细"}</div>
        {hover ? <div className={s.sunHoverVal}>{human(hover.size)}</div> : null}
      </div>
    </div>
  );
}

function MemoryView({ project }: { project: string }) {
  const [target, setTarget] = useState<"rom" | "ram">("rom");
  const [depth, setDepth] = useState(3);
  const [data, setData] = useState<Record<string, FpTarget> | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [zoom, setZoom] = useState<FpNode | null>(null);

  useEffect(() => {
    let alive = true;
    setBusy(true);
    setErr("");
    api.zephyrFootprint(project, depth)
      .then((r) => { if (alive) setData(r.targets ?? {}); })
      .catch((e: unknown) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [project, depth]);

  const cur = data?.[target];
  if (busy && !data) return <div className={s.note}>读取 footprint…</div>;
  if (err) return <div className={s.warn}>读取失败：{err}</div>;
  if (!cur?.ok || !cur.root) {
    return (
      <div className={s.note}>
        还没有 <code>{target}.json</code>。它由「用量报告」步骤生成
        （等价于 <code>west build -t ram_report -t rom_report</code>）——
        在构建页点一次，或跑 <code>elab run -p {project} --steps report</code>。
      </div>
    );
  }

  // 下钻后 Top 列表跟着换层：看的是"当前这一层"谁占得多
  const topRoot = zoom ?? cur.root;
  const top = [...(topRoot.children ?? [])]
    .sort((a, b) => b.size - a.size)
    .slice(0, 12);

  return (
    <div className={s.memWrap}>
      <div className={s.bar}>
        <SegmentedTabs
          tabs={[{ id: "rom" as const, label: "Flash (ROM)" }, { id: "ram" as const, label: "RAM" }]}
          value={target}
          onChange={(v) => { setTarget(v); setZoom(null); }}
        />
        <span className={s.hint}>
          总量 <strong>{human(cur.total ?? 0)}</strong>
          {cur.truncated ? " · 已按节点上限裁剪" : ""}
        </span>
        <label className={s.depthPick}>
          层深
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[2, 3, 4, 5].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      </div>
      {zoom ? (
        <div className={s.zoomBar}>
          <button type="button" className={s.zoomBack} onClick={() => setZoom(null)}>
            ← 回到全部
          </button>
          <span className={s.zoomName}>{zoom.name}</span>
          <span className={s.hint}>{human(zoom.size)}</span>
        </div>
      ) : null}
      <div className={s.memBody}>
        <Sunburst root={zoom ?? cur.root} maxDepth={depth} onZoom={setZoom} />
        <div className={s.topList}>
          <div className={s.topHead}>
            {zoom ? `${zoom.name} 的下一级` : "一级占用"} · 前 {top.length}
          </div>
          {top.map((k) => (
            <div key={k.name} className={s.topRow}>
              <span className={s.topName} title={k.name}>{k.name}</span>
              <span className={s.topSize}>{human(k.size)}</span>
              <span className={s.topPct}>
                {(((k.size || 0) / (topRoot.size || 1)) * 100).toFixed(1)}%
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── Kconfig ─────────────────────────────────────────────────── */

function KconfigView({ project }: { project: string }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [data, setData] = useState<Awaited<ReturnType<typeof api.zephyrKconfig>> | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.zephyrKconfig(project)
      .then((r) => { if (alive) setData(r); })
      .catch((e: unknown) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [project]);

  const groups = useMemo(() => {
    if (!data) return [];
    const needle = q.trim().toLowerCase();
    if (!needle) return data.groups;
    return data.groups
      .map((g) => ({
        ...g,
        items: g.items.filter(
          (it) => it.name.toLowerCase().includes(needle) ||
                  (it.value || "").toLowerCase().includes(needle),
        ),
      }))
      .filter((g) => g.items.length > 0);
  }, [data, q]);

  if (err) return <div className={s.warn}>读取失败：{err}</div>;
  if (!data) return <div className={s.note}>读取 .config…</div>;
  if (!data.ok) return <div className={s.note}>还没有 <code>zephyr/.config</code>。先跑一次构建。</div>;

  const searching = q.trim().length > 0;

  return (
    <div className={s.kcWrap}>
      <div className={s.bar}>
        <Input
          value={q}
          placeholder="搜符号名或取值（如 UART / CONFIG_LOG）"
          onChange={(e) => setQ(e.target.value)}
          style={{ minWidth: 260 }}
        />
        <span className={s.hint}>
          共 <strong>{data.count}</strong> 项，启用 <strong>{data.enabled}</strong>
          {searching ? ` · 命中 ${groups.reduce((a, g) => a + g.items.length, 0)} 项` : ""}
        </span>
      </div>
      <div className={s.kcList}>
        {groups.map((g) => {
          const key = g.name;
          const isOpen = searching || (open[key] ?? false);
          return (
            <div key={key} className={s.kcGroup}>
              <button
                type="button"
                className={s.kcGroupHead}
                onClick={() => setOpen((m) => ({ ...m, [key]: !isOpen }))}
                aria-expanded={isOpen}
              >
                <span className={s.chev}>{isOpen ? "▾" : "▸"}</span>
                <span className={s.kcGroupName}>{g.short || g.name}</span>
                <span className={s.kcCount}>{g.items.length}</span>
              </button>
              {isOpen ? (
                <div className={s.kcItems}>
                  {g.items.map((it) => (
                    <div
                      key={it.name}
                      className={[s.kcItem, it.enabled ? "" : s.kcOff].filter(Boolean).join(" ")}
                    >
                      <span className={s.kcName}>{it.name}</span>
                      <span className={s.kcVal}>{it.enabled ? it.value || "y" : "未启用"}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
        {groups.length === 0 ? <div className={s.note}>没有匹配的符号。</div> : null}
      </div>
    </div>
  );
}

/* ── 设备树 ───────────────────────────────────────────────────── */

function DtsView({ project }: { project: string }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [data, setData] = useState<Awaited<ReturnType<typeof api.zephyrDevicetree>> | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.zephyrDevicetree(project)
      .then((r) => { if (alive) setData(r); })
      .catch((e: unknown) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [project]);

  const needle = q.trim().toLowerCase();
  const nodes = useMemo(() => {
    if (!data) return [];
    if (!needle) return data.nodes;
    // 命中节点 + 它所有祖先（否则树会断成孤岛）
    const hit = new Set<string>();
    for (const n of data.nodes) {
      const inProps = n.props.some(
        (p) => p.name.toLowerCase().includes(needle) || p.value.toLowerCase().includes(needle),
      );
      if (n.path.toLowerCase().includes(needle) || inProps) hit.add(n.path);
    }
    const keep = new Set<string>();
    for (const p of hit) {
      let cur = p;
      while (cur) {
        keep.add(cur);
        if (cur === "/") break;
        cur = cur.slice(0, cur.lastIndexOf("/")) || "/";
      }
    }
    return data.nodes.filter((n) => keep.has(n.path));
  }, [data, needle]);

  if (err) return <div className={s.warn}>读取失败：{err}</div>;
  if (!data) return <div className={s.note}>读取 zephyr.dts…</div>;
  if (!data.ok) return <div className={s.note}>还没有 <code>zephyr/zephyr.dts</code>。先跑一次构建。</div>;

  const searching = needle.length > 0;

  return (
    <div className={s.dtsWrap}>
      <div className={s.bar}>
        <Input
          value={q}
          placeholder="搜节点路径或属性（如 usart1 / zephyr,console）"
          onChange={(e) => setQ(e.target.value)}
          style={{ minWidth: 280 }}
        />
        <span className={s.hint}>
          共 <strong>{data.count}</strong> 个节点
          {searching ? ` · 命中 ${nodes.filter((n) => n.path.toLowerCase().includes(needle) || n.props.some((p) => p.name.toLowerCase().includes(needle) || p.value.toLowerCase().includes(needle))).length} 个` : ""}
        </span>
      </div>
      <div className={s.dtsList}>
        {nodes.map((n) => {
          const depth = n.path === "/" ? 0 : n.path.split("/").length - 2;
          const isOpen = searching || (open[n.path] ?? depth < 1);
          const hasKids = nodes.some((m) => m.parent === n.path);
          return (
            <div key={n.path} className={s.dtsNode} style={{ paddingLeft: depth * 14 }}>
              <button
                type="button"
                className={s.dtsHead}
                onClick={() => setOpen((m) => ({ ...m, [n.path]: !isOpen }))}
                aria-expanded={isOpen}
              >
                <span className={s.chev}>{hasKids ? (isOpen ? "▾" : "▸") : "·"}</span>
                <span className={s.dtsName}>{n.path === "/" ? "/" : n.name}</span>
                <span className={s.dtsSrc} title={n.source}>{n.source}</span>
              </button>
              {isOpen && n.props.length > 0 ? (
                <div className={s.dtsProps} style={{ marginLeft: 18 }}>
                  {n.props.map((p) => (
                    <div key={p.name} className={s.dtsProp}>
                      <span className={s.propName}>{p.name}</span>
                      <span className={s.propVal}>{p.value}</span>
                      {p.source ? <span className={s.propSrc} title={p.source}>*</span> : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
        {nodes.length === 0 ? <div className={s.note}>没有匹配的节点。</div> : null}
      </div>
    </div>
  );
}
