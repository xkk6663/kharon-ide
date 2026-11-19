/**
 * 矩阵工作区（Kharon）：多板并排对照。
 *
 * 为什么这是 west 给不了的东西（产品存在的理由之一）：
 *   一个 app × 四块板同时构建、比对 FLASH/RAM/耗时，在终端里要开四个窗口手工抄。
 *
 * 三条纪律：
 *  1. **数据只来自留档**：`/api/matrix` 读的是 run 的 jsonl 与构建产物，
 *     所以断网重进页面数字仍在（不是内存状态）；
 *  2. **并发有预算**：串行触发（一块板跑完再跑下一块）。上游实测本机 16 核
 *     `-j16/-j18` 会把提交内存打光，四路并发必然 OOM —— 顺序跑只多几分钟，不炸；
 *  3. **部分失败不灰整表**：某板失败只标红那一行，其余照常显示。
 */

import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import { Button } from "../primitives/Button";
import s from "./MatrixWorkspace.module.css";

type Row = Awaited<ReturnType<typeof api.matrix>>["groups"][number]["projects"][number];
type Group = Awaited<ReturnType<typeof api.matrix>>["groups"][number];

function kb(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(1)}K` : `${n} B`;
}

function errmsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 结论 → 样式类（显式映射：CSS module 的索引访问在 TS 下不可靠） */
function verdictClass(
  verdict: Row["verdict"],
  built: boolean,
  css: Record<string, string>,
): string {
  if (verdict === "ok") return css.vOk;
  if (verdict === "failed") return css.vFail;
  if (verdict === "running") return css.vRunning;
  return built ? css.vBuilt : css.vUnknown;
}

export function MatrixWorkspace({ running }: { running: boolean }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.matrix();
      setGroups(d.groups);
      setErr("");
    } catch (e) {
      setErr(errmsg(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 顺序构建一组：一块板跑完再跑下一块（避免 4 路并发 OOM）。 */
  async function buildGroup(g: Group) {
    setBusy(g.group);
    setErr("");
    setNote(`开始：${g.group}（${g.projects.length} 个工程，串行执行）`);
    try {
      for (const p of g.projects) {
        setNote(`正在构建 ${p.name}（${p.board || p.chip}）…`);
        try {
          await api.startRun({ project: p.name, steps: ["build"] });
        } catch (e) {
          if (e instanceof ApiError) {
            setNote(`启动 ${p.name} 失败：${e.message}`);
            break;
          }
          throw e;
        }
        // 轮询到该工程不在 active 列表为止（最多 20 分钟）
        const t0 = Date.now();
        for (;;) {
          await new Promise((r) => setTimeout(r, 2000));
          const a = await api.runsActive();
          if (!a.active.some((x) => x.project === p.name)) break;
          if (Date.now() - t0 > 20 * 60 * 1000) break;
        }
      }
      setNote("全部结束，正在刷新矩阵…");
      await load();
      setNote("");
    } catch (e) {
      setErr(errmsg(e));
    } finally {
      setBusy("");
    }
  }

  const empty = groups.length === 0;

  return (
    <div className={s.wrap}>
      <div className={s.bar}>
        <span className={s.barLabel}>多板对照</span>
        <span className={s.hint}>
          同组 = 同一份源码在不同板上的结果（projects yaml 的 <code>matrix:</code>）
        </span>
        <span className={s.spacer} />
        <Button variant="toolbar" onClick={() => void load()} disabled={Boolean(busy) || running}>
          刷新
        </Button>
      </div>

      {err ? <div className={s.err}>{err}</div> : null}
      {note ? <div className={s.note}>{note}</div> : null}

      {empty ? (
        <div className={s.empty}>
          还没有同组构建记录。给两个以上工程写同一个 <code>matrix: &lt;组名&gt;</code>，
          然后点下面的「全部构建」。
        </div>
      ) : null}

      {groups.map((g) => (
        <div key={g.group} className={s.group}>
          <div className={s.groupHead}>
            <span className={s.groupName}>{g.group}</span>
            <span className={s.groupCount}>{g.projects.length} 个工程</span>
            <span className={s.spacer} />
            <Button
              variant="toolbar"
              onClick={() => void buildGroup(g)}
              disabled={Boolean(busy) || running}
            >
              {busy === g.group ? "构建中…" : "全部构建"}
            </Button>
          </div>
          <table className={s.table}>
            <thead>
              <tr>
                <th>工程</th>
                <th>板 / 芯片</th>
                <th>FLASH</th>
                <th>RAM</th>
                <th>耗时</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>
              {g.projects.map((p: Row) => {
                const flash = p.memory?.FLASH;
                const ram = p.memory?.RAM;
                return (
                  <tr key={p.name} className={p.verdict === "failed" ? s.rowFail : ""}>
                    <td className={s.mono}>{p.name}</td>
                    <td title={p.target || p.chip}>
                      <span className={s.board}>{p.board || p.chip}</span>
                      {p.part ? <span className={s.part}> · {p.part}</span> : null}
                    </td>
                    <td className={s.num}>
                      {flash ? (
                        <>
                          {kb(flash.used)}
                          <span className={s.pct}> / {flash.pct}%</span>
                        </>
                      ) : (
                        <span className={s.dim}>待测量</span>
                      )}
                    </td>
                    <td className={s.num}>
                      {ram ? (
                        <>
                          {kb(ram.used)}
                          <span className={s.pct}> / {ram.pct}%</span>
                        </>
                      ) : (
                        <span className={s.dim}>待测量</span>
                      )}
                    </td>
                    <td className={s.num}>
                      {p.elapsed_s ? `${p.elapsed_s.toFixed(1)}s` : <span className={s.dim}>—</span>}
                    </td>
                    <td>
                      <span className={verdictClass(p.verdict, p.built, s)}>
                        {p.verdict === "ok"
                          ? "✓ 通过"
                          : p.verdict === "failed"
                            ? "✗ 失败"
                            : p.verdict === "running"
                              ? "运行中"
                              : p.built
                                ? "已构建"
                                : "未构建"}
                      </span>
                      {p.run ? <span className={s.runId}>{p.run}</span> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {g.projects.some((p) => p.memory_source === "zephyr-elf") ? (
            <div className={s.source}>
              内存读数为 <strong>实测</strong>：来自各工程构建产物的 <code>zephyr.elf</code>
              （与链接器自报逐字节一致），不是芯片 yaml 的声明。
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
