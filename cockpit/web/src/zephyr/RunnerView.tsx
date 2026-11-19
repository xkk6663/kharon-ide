/**
 * 调试图：`zephyr/runners.yaml` 的面板化投影。
 *
 * 这份文件是**构建系统**（`board.cmake`）在 configure 阶段生成的，里面把
 * runner 清单、默认 flash/debug runner、GDB 与 OpenOCD 的绝对路径都算好了。
 * 所以这里只显示、不提供"手工填探针"的入口 —— 想换 runner 就改板级/工程配置
 * 后重新 configure，让事实回到同一个源头（否则就是第二套真相）。
 */

import { useEffect, useState } from "react";
import { api } from "../api/client";
import s from "./ZephyrDashboard.module.css";

export function RunnerView({ project }: { project: string }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.zephyrRunners>> | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.zephyrRunners(project)
      .then((r) => { if (alive) setData(r); })
      .catch((e: unknown) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [project]);

  if (err) return <div className={s.warn}>读取失败：{err}</div>;
  if (!data) return <div className={s.note}>读取 runners.yaml…</div>;
  if (!data.ok) {
    return (
      <div className={s.note}>
        还没有 <code>zephyr/runners.yaml</code>。它由 configure 阶段生成
        （<code>board.cmake</code> 声明可用 runner），先跑一次构建。
      </div>
    );
  }

  const cfg = data.config ?? {};
  const text = (k: string): string => {
    const v = cfg[k];
    if (Array.isArray(v)) return v.join("  ·  ");
    return v || "—";
  };
  const argsEntries = Object.entries(data.args ?? {});

  return (
    <div className={s.runnerWrap}>
      <div className={s.runnerTop}>
        <div className={s.runnerCard}>
          <div className={s.runnerCap}>默认 debug runner</div>
          <div className={s.runnerBig}>{data.debug_runner || "—"}</div>
        </div>
        <div className={s.runnerCard}>
          <div className={s.runnerCap}>默认 flash runner</div>
          <div className={s.runnerBig}>{data.flash_runner || "—"}</div>
        </div>
        <div className={s.runnerCard}>
          <div className={s.runnerCap}>可用 runner</div>
          <div className={s.runnerChips}>
            {data.runners.map((r) => (
              <span key={r} className={s.chip}>{r}</span>
            ))}
          </div>
        </div>
      </div>

      <div className={s.runnerGrid}>
        <div className={s.runnerBlock}>
          <div className={s.blockHead}>工具与产物（构建系统解析出的绝对路径）</div>
          <div className={s.kvRow}><span className={s.kvKey}>GDB</span><span className={s.kvVal}>{text("gdb")}</span></div>
          <div className={s.kvRow}><span className={s.kvKey}>OpenOCD</span><span className={s.kvVal}>{text("openocd")}</span></div>
          <div className={s.kvRow}><span className={s.kvKey}>scripts</span><span className={s.kvVal}>{text("openocd_search")}</span></div>
          <div className={s.kvRow}><span className={s.kvKey}>ELF</span><span className={s.kvVal}>{text("elf_file")}</span></div>
          <div className={s.kvRow}><span className={s.kvKey}>board_dir</span><span className={s.kvVal}>{text("board_dir")}</span></div>
        </div>

        <div className={s.runnerBlock}>
          <div className={s.blockHead}>各 runner 的默认参数</div>
          {argsEntries.map(([name, args]) => (
            <div key={name} className={s.argRow}>
              <span className={s.argName}>{name}</span>
              <span className={s.argVal}>
                {Array.isArray(args) && args.length ? args.join(" ") : "（无）"}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className={s.hint}>
        这份清单由 <code>board.cmake</code> 在 configure 阶段生成 ——
        想换探针就改板级/工程配置后重新 configure，别在面板里手填第二份参数。
      </div>
    </div>
  );
}
