import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  ArchitectureBoard,
  ArchitectureProduct,
  ArchitectureResponse,
  ArchitectureTarget,
  PlanPreview,
} from "../api/types";
import { api } from "../api/client";
import { Button } from "../primitives/Button";
import { Input } from "../primitives/Input";
import { Pill, Tag } from "../primitives/Tag";
import { Tooltip } from "../primitives/Tooltip";
import s from "./ArchitectureWorkspace.module.css";

function errmsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function basename(p: string): string {
  return p.split(/[\\/]/).filter(Boolean).pop() ?? p;
}

function cmdText(pv: PlanPreview | null, step = "build"): string {
  const row = pv?.plan.find((x) => x.step === step);
  if (!row) return "";
  return (row.commands[0] ?? [])
    .map((a) => (a.includes(" ") ? `"${a}"` : a))
    .join(" ");
}

type EditState = {
  kind: "product" | "app";
  name: string;
  path: string;
  text: string;
  exists: boolean;
};

type CreateState = {
  name: string;
  chip: string;
  board: string;
  app: string;
  target: string;
  close_on: string;
};

export interface ArchitectureWorkspaceProps {
  selectedProduct: string;
  selectedTarget: string;
  running: boolean;
  onPick: (product: string, target: string) => void;
  /** 新建产品后由 App 刷新目录并选中新工程 */
  onProjectCreated?: (product: string, target: string) => void;
}

export function ArchitectureWorkspace({
  selectedProduct,
  selectedTarget,
  running,
  onPick,
  onProjectCreated,
}: ArchitectureWorkspaceProps) {
  const [data, setData] = useState<ArchitectureResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pv, setPv] = useState<PlanPreview | null>(null);
  const [pvErr, setPvErr] = useState<string | null>(null);
  const [edit, setEdit] = useState<EditState | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [editErr, setEditErr] = useState<string | null>(null);
  const [editMsg, setEditMsg] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [create, setCreate] = useState<CreateState>({
    name: "", chip: "", board: "", app: "", target: "default", close_on: "",
  });
  const [createBusy, setCreateBusy] = useState(false);
  const [createPrev, setCreatePrev] = useState("");
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [createMsg, setCreateMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      setData(await api.architecture());
    } catch (e) {
      setErr(errmsg(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const boards = data?.boards ?? [];
  const apps = data?.apps ?? [];
  const products = data?.products ?? [];
  const chips = data?.chips ?? [];

  const picked = useMemo<ArchitectureTarget | null>(() => {
    for (const p of products) {
      const t = p.targets.find((x) => x.product === selectedProduct
        && (x.target || "") === (selectedTarget || ""));
      if (t) return t;
    }
    return null;
  }, [products, selectedProduct, selectedTarget]);

  const preview = async (t: ArchitectureTarget) => {
    setPv(null);
    setPvErr(null);
    try {
      setPv(await api.plan({
        project: t.product,
        target: t.target || undefined,
        steps: ["build"],
      }));
    } catch (e) {
      setPvErr(errmsg(e));
    }
  };

  const openSource = async (kind: "product" | "app", name: string) => {
    setEditBusy(true);
    setEditErr(null);
    setEditMsg(null);
    try {
      const src = await api.architectureSource(kind, name);
      setEdit({ kind, name, path: src.path, text: src.text, exists: src.exists });
    } catch (e) {
      setEditErr(errmsg(e));
    } finally {
      setEditBusy(false);
    }
  };

  const saveSource = async () => {
    if (!edit) return;
    setEditBusy(true);
    setEditErr(null);
    setEditMsg(null);
    try {
      const saved = await api.architectureSave(edit.kind, edit.name, edit.text);
      setEdit({ ...edit, path: saved.path, exists: true });
      setEditMsg(`已保存 ${saved.bytes} B；拓扑已重新扫描`);
      await load();
    } catch (e) {
      setEditErr(errmsg(e));
    } finally {
      setEditBusy(false);
    }
  };

  const openCreate = () => {
    /* 新建工程从空白开始：不从当前选中产品复制芯片/APP，避免“想建 F411
       却带着 ESP32 的 APP 一路点下去”的误配。 */
    setCreate({
      name: "",
      chip: "",
      board: "",
      app: "",
      target: "default",
      close_on: "",
    });
    setCreatePrev("");
    setCreateErr(null);
    setCreateMsg(null);
    setCreateOpen(true);
  };

  const createBody = (preview: boolean) => ({
    name: create.name.trim(),
    chip: create.chip,
    board: create.board,
    app: create.app,
    target: create.target.trim() || "default",
    close_on: create.close_on.trim(),
    preview,
  });

  const previewCreate = async () => {
    setCreateBusy(true);
    setCreateErr(null);
    setCreateMsg(null);
    try {
      const r = await api.architectureCreate(createBody(true));
      setCreatePrev(r.text);
    } catch (e) {
      setCreateErr(errmsg(e));
    } finally {
      setCreateBusy(false);
    }
  };

  const writeCreate = async () => {
    setCreateBusy(true);
    setCreateErr(null);
    setCreateMsg(null);
    try {
      const r = await api.architectureCreate(createBody(false));
      setCreateMsg(`已创建 ${r.name}（${r.target}）→ ${r.path}`);
      await load();
      onProjectCreated?.(r.name, r.target);
    } catch (e) {
      setCreateErr(errmsg(e));
    } finally {
      setCreateBusy(false);
    }
  };

  const boardCard = (b: ArchitectureBoard) => (
    <article key={`${b.source}-${b.id}`} className={s.card} data-board={b.id}>
      <header className={s.cardHead}>
        <strong className={s.title}>{b.id}</strong>
        <Pill tone={b.source === "kharon" ? "green" : "neutral"}>
          {b.source === "kharon" ? "自研板" : "上游板"}
        </Pill>
      </header>
      <div className={s.sub}>{b.full_name || b.name}</div>
      <div className={s.tags}>
        <Tag dense mono>{b.vendor || "vendor?"}</Tag>
        {b.socs.map((soc) => <Tag key={soc} dense mono tone="blue">{soc}</Tag>)}
      </div>
      <dl className={s.facts}>
        <dt>console</dt><dd>{b.dts.console || "—"}</dd>
        <dt>aliases</dt><dd>{b.dts.aliases.slice(0, 6).join(" · ") || "—"}</dd>
        <dt>user props</dt><dd>{b.dts.user_props.join(" · ") || "—"}</dd>
      </dl>
      <div className={s.rel}>
        <span>{b.apps.length} APP</span>
        <span>{b.products.length} target</span>
        <span title={b.board_yml}>{b.files.length} 个板级文件</span>
      </div>
      {b.apps.length ? (
        <div className={s.overlay}>APP：{b.apps.map((a) => a.name).join(" · ")}</div>
      ) : null}
    </article>
  );

  const appCard = (a: ArchitectureResponse["apps"][number]) => (
    <article key={a.name} className={s.card} data-app={a.name}>
      <header className={s.cardHead}>
        <strong className={s.title}>{a.name}</strong>
        <Tag tone={a.kind === "lab" ? "amber" : a.kind === "sample" ? "violet" : "green"} dense>
          {a.kind}
        </Tag>
        <Button size="sm" variant="toolbar" disabled={editBusy}
          onClick={() => void openSource("app", a.name)}>
          {a.app_yaml ? "编辑 app.yaml" : "新建 app.yaml"}
        </Button>
      </header>
      <div className={s.sub} title={a.root}>{basename(a.root)}</div>
      <div className={s.tags}>
        {a.board_scope ? <Tag dense mono>{a.board_scope}</Tag> : null}
        {a.declared_board ? <Tag dense mono tone="blue">{a.declared_board}</Tag> : null}
        {a.requires.map((r) => <Tag key={r} dense tone="neutral">{r}</Tag>)}
      </div>
      {a.overlays.length ? (
        <div className={s.overlay}>overlay：{a.overlays.join(" · ")}</div>
      ) : (
        <div className={s.overlay}>无板级 overlay（板级差异在 Board / product）</div>
      )}
      <div className={s.rel}>
        <span>{a.products.length} 个产品 target</span>
      </div>
      {a.products.length ? (
        <div className={s.overlay}>
          {a.products.map((p) => `${p.product}${p.target ? `/${p.target}` : ""}`).join(" · ")}
        </div>
      ) : null}
    </article>
  );

  const targetRow = (p: ArchitectureProduct, t: ArchitectureTarget) => {
    const active = t.product === selectedProduct && (t.target || "") === (selectedTarget || "");
    return (
      <div key={`${p.name}-${t.target || "default"}`}
        className={[s.targetRow, active ? s.targetActive : ""].filter(Boolean).join(" ")}>
        <div className={s.targetMain}>
          <span className={s.targetId}>{t.target || "default"}</span>
          <span className={s.targetArrow}>→</span>
          <span className={s.targetBoard} title={t.board_target}>{t.board}</span>
          <span className={s.targetArrow}>/</span>
          <span className={s.targetApp}>{t.app}</span>
        </div>
        <div className={s.targetMeta}>
          <span>{t.chip}</span>
          {active ? <Pill tone="green">当前</Pill> : null}
        </div>
        <div className={s.targetActions}>
          <Button
            size="sm"
            variant={active ? "primary" : "toolbar"}
            disabled={running}
            onClick={() => onPick(t.product, t.target)}
          >
            {active ? "已选中" : "设为当前"}
          </Button>
          <Button size="sm" variant="toolbar" onClick={() => void preview(t)}>
            预览 build
          </Button>
        </div>
      </div>
    );
  };

  if (err) {
    return (
      <div className={s.wrap}>
        <div className={s.error}>架构视图加载失败：{err}</div>
        <Button onClick={() => void load()} disabled={busy}>重试</Button>
      </div>
    );
  }

  const createChip = chips.find((c) => c.id === create.chip);
  const createBoards = createChip
    ? boards.filter((b) => b.id === createChip.board)
    : boards;
  /**
   * APP 过滤：声明了 board_scope 的只留当前板 / portable；未声明的（历史工程）
   * 保持可见，避免把“没写 app.yaml”的老 APP 一刀切藏掉。
   */
  const createApps = create.board
    ? apps.filter((a) => !a.board_scope
        || a.board_scope === "portable"
        || a.board_scope === create.board)
    : apps;

  return (
    <div className={s.wrap}>
      <div className={s.bar}>
        <div>
          <span className={s.heading}>Board / APP / Product</span>
          <span className={s.root} title={data?.zephyr_root}>{data?.zephyr_root || ""}</span>
        </div>
        <div className={s.stats}>
          <Pill tone="blue">{boards.length} 板</Pill>
          <Pill tone="green">{apps.length} APP</Pill>
          <Pill tone="amber">{products.length} 产品</Pill>
          <Button size="sm" variant="primary" onClick={openCreate}>
            ＋ 新建工程
          </Button>
          <Button size="sm" variant="toolbar" onClick={() => void load()} disabled={busy}>
            {busy ? "扫描中…" : "刷新"}
          </Button>
        </div>
      </div>

      {data?.warnings.length ? (
        <div className={s.warn}>
          {data.warnings.map((w) => <div key={w}>{w}</div>)}
        </div>
      ) : null}

      <div className={s.columns}>
        <section className={s.col}>
          <h3 className={s.colTitle}>板级真相 <span>kharon-boards 优先</span></h3>
          {boards.map(boardCard)}
        </section>
        <section className={s.col}>
          <h3 className={s.colTitle}>APP <span>按 APP 目录 / app.yaml</span></h3>
          {apps.map(appCard)}
        </section>
        <section className={s.col}>
          <h3 className={s.colTitle}>产品 target <span>Product × Target × App</span></h3>
          {products.map((p) => (
            <div key={p.name} className={s.product}>
              <div className={s.productHead}>
                <strong>{p.name}</strong>
                <span>{p.targets.length} target</span>
                <Button size="sm" variant="toolbar" disabled={editBusy}
                  onClick={() => void openSource("product", p.name)}>
                  编辑 manifest
                </Button>
              </div>
              {p.targets.map((t) => targetRow(p, t))}
            </div>
          ))}
        </section>
      </div>

      <div className={s.detail}>
        <div className={s.detailHead}>
          <strong>{picked ? `${picked.product} / ${picked.target || "default"}` : "选一个产品 target"}</strong>
          {picked ? <Tag dense mono>{picked.board_target}</Tag> : null}
        </div>
        {picked ? (
          <>
            <div className={s.detailGrid}>
              <span>chip</span><code>{picked.chip}</code>
              <span>board</span><code>{picked.board}</code>
              <span>APP</span><code>{picked.app}</code>
              <span>root</span><code title={picked.root}>{picked.root}</code>
              <span>work_dir</span><code title={picked.work_dir}>{picked.work_dir}</code>
            </div>
            <Tooltip side="bottom" tip="只读预览；真正执行请用阶段轨 / CLI">
              <Button size="sm" variant="toolbar" onClick={() => void preview(picked)}>
                预览 build
              </Button>
            </Tooltip>
            {pvErr ? <div className={s.error}>{pvErr}</div> : null}
            {pv ? (
              <pre className={s.command}>{cmdText(pv) || "（build 步骤无命令，见 plan.note）"}</pre>
            ) : null}
          </>
        ) : (
          <div className={s.sub}>
            点右侧任一 target 的「设为当前」，阶段轨就会按该 target 解析命令；这里有板级真相、APP 归属和 target 映射三层证据。
          </div>
        )}
      </div>

      {createOpen ? (
        <div className={s.modalOverlay} role="dialog" aria-modal="true"
          aria-label="新建工程">
          <div className={s.modal}>
            <div className={s.modalHead}>
              <div>
                <strong>新建工程 · 芯片 → 板级真相 → APP</strong>
                <div className={s.modalPath}>
                  三层选择都来自已安装芯片包 / topology；路径与 work_dir 由后端生成。
                </div>
              </div>
              <Button size="sm" variant="toolbar"
                onClick={() => { setCreateOpen(false); setCreateErr(null); setCreateMsg(null); }}>
                关闭
              </Button>
            </div>

            <div className={s.formGrid}>
              <label>
                <span>工程名</span>
                <Input value={create.name} placeholder="例如 f411_dac_adc"
                  onChange={(e) => { setCreate({ ...create, name: e.target.value }); setCreatePrev(""); }} />
              </label>
              <label>
                <span>芯片</span>
                <select value={create.chip}
                  onChange={(e) => {
                    const chip = chips.find((c) => c.id === e.target.value);
                    setCreate({ ...create, chip: e.target.value, board: chip?.board || "" });
                    setCreatePrev("");
                  }}>
                  <option value="">选芯片</option>
                  {chips.map((c) => (
                    <option key={c.id} value={c.id}>{c.part || c.id} · {c.target}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>板级真相</span>
                <select value={create.board}
                  disabled={!create.chip}
                  onChange={(e) => { setCreate({ ...create, board: e.target.value, app: "", target: "default" }); setCreatePrev(""); }}>
                  <option value="">{create.chip ? "选板" : "先选芯片"}</option>
                  {createBoards.map((b) => (
                    <option key={`${b.source}-${b.id}`} value={b.id}>
                      {b.id} · {b.source === "kharon" ? "自研板" : "上游板"}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>APP 层</span>
                <select value={create.app}
                  disabled={!create.board}
                  onChange={(e) => {
                    const app = createApps.find((a) => a.name === e.target.value);
                    setCreate({
                      ...create,
                      app: e.target.value,
                      /* APP 已有产品 target 时直接沿用（f411_dac_adc -> pwm_adc），
                         省掉用户手打；没有就落 default。 */
                      target: app?.products?.[0]?.target || "default",
                    });
                    setCreatePrev("");
                  }}>
                  <option value="">{create.board ? "选 APP" : "先选板级真相"}</option>
                  {createApps.map((a) => (
                    <option key={a.name} value={a.name}>{a.name} · {a.kind}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>target id</span>
                <Input value={create.target} placeholder="default / pwm_adc"
                  onChange={(e) => { setCreate({ ...create, target: e.target.value }); setCreatePrev(""); }} />
              </label>
              <label>
                <span>串口闭环关键字（可选）</span>
                <Input value={create.close_on} placeholder="例如 wave t="
                  onChange={(e) => { setCreate({ ...create, close_on: e.target.value }); setCreatePrev(""); }} />
              </label>
            </div>

            <div className={s.modalHint}>
              芯片决定 `west -b` 与芯片包，板级真相决定 DTS/调试器，APP 决定源码根；
              target id 决定 `<code>build/…/&lt;target&gt;</code>`。这里不复制任何一条路径。
            </div>
            {createErr ? <div className={s.error}>{createErr}</div> : null}
            {createMsg ? <div className={s.okMsg}>{createMsg}</div> : null}
            {createPrev ? (
              <pre className={s.command}>{createPrev}</pre>
            ) : null}
            <div className={s.modalActions}>
              <Button size="sm" variant="toolbar" disabled={createBusy}
                onClick={() => void previewCreate()}>
                {createBusy ? "生成中…" : "预览 manifest"}
              </Button>
              <Button size="sm" variant="primary" disabled={createBusy || !create.name || !create.chip || !create.board || !create.app}
                onClick={() => void writeCreate()}>
                {createBusy ? "创建中…" : "创建工程"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {editErr && !edit ? <div className={s.error}>读取失败：{editErr}</div> : null}

      {edit ? (
        <div className={s.modalOverlay} role="dialog" aria-modal="true"
          aria-label={`${edit.kind} ${edit.name}`}>
          <div className={s.modal}>
            <div className={s.modalHead}>
              <div>
                <strong>{edit.kind === "product" ? "产品 manifest" : "APP app.yaml"} · {edit.name}</strong>
                <div className={s.modalPath} title={edit.path}>{edit.path}</div>
              </div>
              <Button size="sm" variant="toolbar"
                onClick={() => { setEdit(null); setEditErr(null); setEditMsg(null); }}>
                关闭
              </Button>
            </div>
            <div className={s.modalHint}>
              编辑器是 YAML 原文；保存前由后端解析校验，路径只按 topology 反查
              （产品必须在 <code>kharon-manifest/products</code>，APP 必须在 <code>applications/&lt;name&gt;</code>）。
              {edit.kind === "product" ? " 产品 name 不能在这里改。" : ""}
              {edit.exists ? "" : " 文件尚不存在，保存即新建。"}
            </div>
            <textarea
              className={s.editor}
              spellCheck={false}
              value={edit.text}
              onChange={(e) => { setEdit({ ...edit, text: e.target.value }); setEditMsg(null); }}
            />
            {editErr ? <div className={s.error}>{editErr}</div> : null}
            {editMsg ? <div className={s.okMsg}>{editMsg}</div> : null}
            <div className={s.modalActions}>
              <Button size="sm" variant="toolbar" disabled={editBusy}
                onClick={() => void api.architectureSource(edit.kind, edit.name)
                  .then((src) => { setEdit({ ...edit, path: src.path, text: src.text,
                    exists: src.exists }); setEditErr(null); setEditMsg(null); })
                  .catch((e) => setEditErr(errmsg(e)))}>
                重新读取
              </Button>
              <Button size="sm" variant="primary" disabled={editBusy}
                onClick={() => void saveSource()}>
                {editBusy ? "保存中…" : "保存并重扫描"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
