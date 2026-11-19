/**
 * west 工作区（Kharon）：模块树 / board 清单 / SDK 目标 / 最近构建事实。
 *
 * 这一层 west **完全不给**：`west list` 只打印 70 行文本，没有任何视图会告诉你
 * "声明了 70 个模块、实际只拉取了 7 个、另外 63 个不存在但照样能构建"。
 * 换块板才发现缺 `hal_xtensa` —— 这是本仓库的真实状态，不是假设。
 *
 * 只读：面板上没有"拉取"按钮。依赖归 `kharon deps`（pack 管我们的知识，
 * west update 管上游源码，两套依赖模型不混）。
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import { Button } from "../primitives/Button";
import { Input } from "../primitives/Input";
import s from "./WestWorkspace.module.css";

type Snapshot = Awaited<ReturnType<typeof api.west>>;

export function WestWorkspace() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [showMissing, setShowMissing] = useState(false);

  const load = useCallback(async (query: string, refresh = false) => {
    setBusy(true);
    try {
      setData(await api.west(query, refresh));
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load("");
  }, [load]);

  const modules = data?.modules ?? [];
  const shownModules = showMissing ? modules : modules.filter((m) => m.present);
  const missing = modules.length - (data?.module_present ?? 0);
  // 拓扑 / 应用形态的展示文案：后端只给代号（t1/t2/t3、standalone…），翻译给人看。
  const topo = data?.topology
    ? `拓扑 ${data.topology === "t1" ? "T1 仓内清单" : data.topology === "t2" ? "T2 工作区清单" : data.topology === "t3" ? "T3 独立" : data.topology}`
    : "";
  const role = data?.app_role
    ? (data.app_role === "repo" ? "仓库应用" : data.app_role === "workspace" ? "工作区应用" : data.app_role === "standalone" ? "独立应用" : data.app_role)
    : "";

  return (
    <div className={s.wrap}>
      <div className={s.bar}>
        <span className={s.barLabel}>west 工作区</span>
        {topo ? <span className={s.chip} title="双拓扑（清单仓位置）">{topo}</span> : null}
        {role ? <span className={s.chip} title="三形态（应用位置）">{role}</span> : null}
        <span className={s.hint} title={data?.workspace}>
          {data?.workspace || "—"}
        </span>
        <span className={s.spacer} />
        <Input
          value={q}
          placeholder="搜索 board（1206 条，只下发前 200）"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void load(q);
          }}
        />
        <Button variant="toolbar" onClick={() => void load(q)} disabled={busy}>
          搜索
        </Button>
        <Button variant="toolbar" onClick={() => void load(q, true)} disabled={busy}>
          刷新
        </Button>
      </div>

      {err ? <div className={s.err}>{err}</div> : null}
      {data && !data.ok ? (
        <div className={s.err}>
          west 不可用：{data.reason || "未知原因"}（检查 kharon.host.yaml 的 tools.zephyr.*）
        </div>
      ) : null}

      {/* ── 模块 ───────────────────────────────────────────── */}
      <div className={s.card}>
        <div className={s.cardHead}>
          <span className={s.cardTitle}>模块</span>
          <span className={s.badgeStrong}>
            {data?.module_present ?? 0} / {data?.module_total ?? 0} 已拉取
          </span>
          {missing > 0 ? <span className={s.badgeWarn}>{missing} 个未拉取</span> : null}
          <span className={s.spacer} />
          <label className={s.toggle}>
            <input
              type="checkbox"
              checked={showMissing}
              onChange={(e) => setShowMissing(e.target.checked)}
            />
            显示未拉取
          </label>
        </div>
        <div className={s.scroll}>
          <table className={s.table}>
            <thead>
              <tr>
                <th>模块</th>
                <th>路径</th>
                <th>revision</th>
                <th>本地</th>
              </tr>
            </thead>
            <tbody>
              {shownModules.map((m) => (
                <tr key={m.name} className={m.present ? "" : s.missingRow}>
                  <td className={s.mono}>{m.name}</td>
                  <td className={s.path} title={m.path}>
                    {m.path}
                  </td>
                  <td className={s.mono}>{m.revision}</td>
                  <td className={m.present ? s.ok : s.warn}>
                    {m.present ? "✓" : "缺失"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className={s.foot}>
          未拉取的模块 <strong>不影响</strong> 当前构建（Zephyr 只按需取用）；换板前先看这里。
        </div>
      </div>

      {/* ── board ──────────────────────────────────────────── */}
      <div className={s.card}>
        <div className={s.cardHead}>
          <span className={s.cardTitle}>board</span>
          <span className={s.badge}>
            共 {data?.board_total ?? 0} 条 · 显示 {data?.boards_shown ?? 0}
          </span>
          {data?.local_targets?.length ? (
            <span className={s.badgeDim}>
              本仓用到：{data.local_targets.join(" · ")}
            </span>
          ) : null}
        </div>
        <div className={s.boardList}>
          {(data?.boards ?? []).map((b) => {
            const local = (data?.local_targets ?? []).includes(b.split("/")[0]);
            return (
              <span key={b} className={local ? s.boardChipLocal : s.boardChip} title={b}>
                {b}
              </span>
            );
          })}
        </div>
      </div>

      {/* ── SDK ────────────────────────────────────────────── */}
      <div className={s.card}>
        <div className={s.cardHead}>
          <span className={s.cardTitle}>SDK 目标</span>
          <span className={s.hint} title={data?.sdk_path}>
            {data?.sdk_path || "—"}
          </span>
        </div>
        <div className={s.kvGrid}>
          {(data?.sdk ?? []).map((t) => (
            <div key={t.target} className={s.kvRow}>
              <span className={s.mono}>{t.target}</span>
              <span className={s.dim}>{t.gdb || "（无 gdb）"}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ── 构建事实 ───────────────────────────────────────── */}
      <div className={s.card}>
        <div className={s.cardHead}>
          <span className={s.cardTitle}>最近构建事实</span>
          <span className={s.hint}>build_info.yml（cmake 生成，非我们编的）</span>
        </div>
        {(data?.builds ?? []).length === 0 ? (
          <div className={s.foot}>还没有构建过任何工程。</div>
        ) : (
          (data?.builds ?? []).map((b) => (
            <div key={b.project} className={s.buildBlock}>
              <div className={s.buildHead}>
                <span className={s.mono}>{b.project}</span>
                <span className={s.badge}>{b.board}</span>
                {b.qualifiers ? <span className={s.badgeDim}>{b.qualifiers}</span> : null}
                {b.zephyr_version ? (
                  <span className={s.badgeDim}>Zephyr {b.zephyr_version}</span>
                ) : null}
              </div>
              <div className={s.cmd} title="west 命令行原文（按钮即命令的证据）">
                {b.west_command || "—"}
              </div>
              <div className={s.dl}>
                <span className={s.dlKey}>dts</span>
                <span className={s.dlVal}>{b.dts_files.join("  ·  ") || "—"}</span>
                <span className={s.dlKey}>kconfig</span>
                <span className={s.dlVal}>{b.kconfig_files.join("  ·  ") || "—"}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
