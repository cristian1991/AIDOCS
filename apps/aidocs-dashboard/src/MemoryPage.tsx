/**
 * MemoryPage — the memory knowledge-graph, IN the dashboard (dashboard-war (d), #200).
 *
 * Integrates the proven standalone graph (agents/memory-kg.html, built by
 * scratch/memory_kg_export.py) as a live page in BOTH builds: desktop reads
 * via the pure-Rust memory_kg_* commands; web reads the same shapes from the
 * gate (outer_gate_dashboard_reads.memory_kg_graph/get).
 *
 * Progressive disclosure (#200 clause 3): EXPLORE mode starts with memory
 * nodes only and GROWS the graph as you click — selecting a node reveals its
 * connected units/keywords/memories; reaching a leaf reveals that leaf's
 * branch. "Show all" restores the classic full view. Selecting a memory also
 * loads its FULL body (title/kind/content) in the side panel (#200 clause 1).
 *
 * Add/edit (#200 clause 2) is deliberately NOT a local write path: memory
 * writes go through the governed memory_capture path in BOTH builds
 * (promotion / source-classification doctrine — "law enters only through the
 * throne"): desktop → dashboard-memory-capture CLI, web → the gate's
 * memory_capture tool (two-phase confirm). The capture form below is a THIN
 * client of that path — the durability rubric, kind aliasing and sovereign
 * guard all live server-side in MemoryStore.capture_memory.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataSet } from "vis-network/standalone/esm/vis-network";
import { Network } from "vis-network/standalone/esm/vis-network";
import {
  memoryCapture,
  memoryKgGet,
  memoryKgGraph,
  type MemoryDetail,
  type MemoryKgEdge,
  type MemoryKgNode,
} from "./dashboardApi";
import { MemoryAnchorHealthPanel } from "./MemoryAnchorHealthPanel";

export type MemoryPageProps = {
  projectRoot: string | null;
};

const KIND_COLORS: Record<string, string> = {
  rule: "#f0883e",
  doctrine: "#a371f7",
  system: "#f85149",
  project: "#3fb950",
  reference: "#58a6ff",
  aidocs: "#8b949e",
  personality: "#db61a2",
  spec: "#e3b341",
  user: "#39c5cf",
  feedback: "#bc8cff",
};
// Canonical capture kinds (memory_store._ACCEPTED_KINDS) — aliases resolve
// server-side; the form only offers the strict enum.
const CAPTURE_KINDS = [
  "workflow-rule",
  "invariant",
  "preference",
  "infrastructure",
  "caveat",
  "related-project",
];

const UNIT_COLOR = "#1f6feb";
const KW_COLOR = "#484f58";
const DIM = { background: "#1c2128", border: "#1c2128" };

function nodeColor(n: MemoryKgNode): { background: string; border: string } {
  if (n.type === "unit") return { background: UNIT_COLOR, border: "#388bfd" };
  if (n.type === "keyword") return { background: KW_COLOR, border: "#6e7681" };
  const c = KIND_COLORS[n.group] ?? "#6e7681";
  return { background: c, border: c };
}

// Cheap, deterministic signature of the graph payload — node ids/types/labels
// and edge endpoints/types folded into a djb2 hash. Two fetches with the same
// signature carry the SAME memory; the live refresh uses this to skip the
// expensive destroy+recreate+physics rebuild when nothing changed (operator
// bug 2026-07-06: the 30s interval re-laid-out the graph even when memory was
// unchanged). O(total chars) — negligible next to a vis force-atlas layout.
function graphSignature(g: { nodes?: MemoryKgNode[]; edges?: MemoryKgEdge[] }): string {
  const nodes = g.nodes ?? [];
  const edges = g.edges ?? [];
  let h = 5381;
  const mix = (s: string) => {
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  };
  for (const n of nodes) mix(`${n.id}${n.type}${n.label}`);
  for (const e of edges) mix(`${e.from}${e.to}${e.type}`);
  return `${nodes.length}:${edges.length}:${h >>> 0}`;
}

export function MemoryPage({ projectRoot }: MemoryPageProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const netRef = useRef<Network | null>(null);
  const nodesRef = useRef<DataSet<Record<string, unknown>> | null>(null);
  const graphRef = useRef<{ nodes: MemoryKgNode[]; edges: MemoryKgEdge[] }>({ nodes: [], edges: [] });
  const adjRef = useRef<Record<string, string[]>>({});
  const revealedRef = useRef<Set<string>>(new Set());
  const exploreRef = useRef(true);
  // Signature of the graph currently RENDERED — the live refresh compares
  // against it and only rebuilds on a real memory delta.
  const sigRef = useRef<string>("");

  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [counts, setCounts] = useState<string>("");
  const [explore, setExplore] = useState(true);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [selected, setSelected] = useState<MemoryKgNode | null>(null);
  const [nonce, setNonce] = useState(0);
  // Governed capture form (#200 clause 2) — thin client of memory_capture.
  const [showCapture, setShowCapture] = useState(false);
  const [capKind, setCapKind] = useState("workflow-rule");
  const [capContent, setCapContent] = useState("");
  const [capHint, setCapHint] = useState("");
  const [capBusy, setCapBusy] = useState(false);
  const [capMsg, setCapMsg] = useState<string | null>(null);
  exploreRef.current = explore;

  const submitCapture = useCallback(async () => {
    const content = capContent.trim();
    if (!content) {
      setCapMsg("content is required");
      return;
    }
    setCapBusy(true);
    setCapMsg(null);
    try {
      const out = await memoryCapture(capKind, content, capHint.trim() || undefined, projectRoot ?? undefined);
      if (out && out.ok) {
        setCapMsg(`captured → ${out.target ?? "(stored)"}`);
        setCapContent("");
        setNonce((n) => n + 1); // re-pull the graph so the new memory appears
      } else {
        setCapMsg(String(out?._detail || out?.message || "capture rejected"));
      }
    } catch (e) {
      setCapMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setCapBusy(false);
    }
  }, [capContent, capHint, capKind, projectRoot]);

  const applyVisibility = useCallback(() => {
    const nodes = nodesRef.current;
    if (!nodes) return;
    const q = search.trim().toLowerCase();
    const revealed = revealedRef.current;
    const exploring = exploreRef.current;
    nodes.update(
      graphRef.current.nodes.map((n) => {
        let hidden = false;
        if (exploring && n.type !== "memory" && !revealed.has(n.id)) hidden = true;
        if (exploring && n.type === "memory" && revealed.size > 0 && !revealed.has(n.id)) {
          // Once exploration starts, non-connected memories stay visible but
          // only the explored branch keeps full color (handled via dim below).
        }
        if (q && !n.id.toLowerCase().includes(q) && !n.label.toLowerCase().includes(q)) hidden = true;
        return { id: n.id, hidden };
      }),
    );
  }, [search]);

  const revealNeighbors = useCallback((id: string) => {
    const revealed = revealedRef.current;
    revealed.add(id);
    for (const nb of adjRef.current[id] ?? []) revealed.add(nb);
  }, []);

  const highlight = useCallback((id: string | null) => {
    const nodes = nodesRef.current;
    if (!nodes) return;
    const byId = new Map(graphRef.current.nodes.map((n) => [n.id, n]));
    if (!id) {
      nodes.update(graphRef.current.nodes.map((n) => ({ id: n.id, color: nodeColor(n) })));
      return;
    }
    const conn = new Set<string>([id, ...(adjRef.current[id] ?? [])]);
    nodes.update(
      graphRef.current.nodes.map((n) => ({
        id: n.id,
        color: conn.has(n.id) ? nodeColor(byId.get(n.id) ?? n) : DIM,
      })),
    );
  }, []);

  const selectNode = useCallback(
    async (id: string) => {
      const node = graphRef.current.nodes.find((n) => n.id === id) ?? null;
      setSelected(node);
      revealNeighbors(id);
      applyVisibility();
      highlight(id);
      if (node?.type === "memory" && node.path) {
        try {
          setDetail(await memoryKgGet(node.path, projectRoot ?? undefined));
        } catch (e) {
          setDetail({ ok: false, _detail: e instanceof Error ? e.message : String(e) });
        }
      } else {
        setDetail(null);
      }
    },
    [applyVisibility, highlight, projectRoot, revealNeighbors],
  );

  // Load + render the graph.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const g = await memoryKgGraph(projectRoot ?? undefined);
        if (cancelled || !containerRef.current) return;
        sigRef.current = graphSignature(g); // record what we're about to render
        graphRef.current = { nodes: g.nodes ?? [], edges: g.edges ?? [] };
        // Keep the explored branch across live refreshes (stale ids are
        // harmless — they just no longer match a node). Only a project
        // switch starts exploration over.
        const liveIds = new Set(graphRef.current.nodes.map((n) => n.id));
        revealedRef.current = new Set([...revealedRef.current].filter((id) => liveIds.has(id)));
        const adj: Record<string, string[]> = {};
        for (const e of graphRef.current.edges) {
          (adj[e.from] = adj[e.from] ?? []).push(e.to);
          (adj[e.to] = adj[e.to] ?? []).push(e.from);
        }
        adjRef.current = adj;
        const memConn = (id: string) =>
          new Set((adj[id] ?? []).filter((x) => x.startsWith("mem:"))).size;
        const nodes = new DataSet(
          graphRef.current.nodes.map((n) => ({
            id: n.id,
            label: `${n.label}  (${memConn(n.id)})`,
            shape: n.type === "unit" ? "diamond" : n.type === "keyword" ? "box" : "dot",
            color: nodeColor(n),
            value: (adj[n.id] ?? []).length || 1,
            hidden: exploreRef.current && n.type !== "memory" && !revealedRef.current.has(n.id),
          })),
        );
        const edges = new DataSet(
          graphRef.current.edges.map((e, i) => ({
            id: i,
            from: e.from,
            to: e.to,
            color:
              e.type === "link"
                ? { color: "#3fb950", opacity: 0.8 }
                : e.type === "anchor"
                  ? { color: UNIT_COLOR, opacity: 0.5 }
                  : { color: KW_COLOR, opacity: 0.35 },
            width: e.type === "link" ? 2 : 1,
          })),
        );
        nodesRef.current = nodes as unknown as DataSet<Record<string, unknown>>;
        try {
          netRef.current?.destroy();
        } catch {
          // an already-torn-down network must not kill the reload
        }
        netRef.current = null;
        // Physics on a huge graph can wedge/crash the webview — cap it.
        // (vis simulates hidden nodes too, so key off the TOTAL count.)
        const heavyGraph = graphRef.current.nodes.length > 1500;
        const net = new Network(
          containerRef.current,
          { nodes, edges },
          {
            nodes: { font: { color: "#e6edf3", size: 12 }, borderWidth: 1, scaling: { min: 6, max: 30 } },
            edges: { smooth: { enabled: true, type: "continuous", roundness: 0.5 }, arrows: { to: { enabled: false } } },
            physics: heavyGraph
              ? { enabled: false }
              : {
                  solver: "forceAtlas2Based",
                  stabilization: { iterations: 160 },
                  forceAtlas2Based: { gravitationalConstant: -45, springLength: 90, springConstant: 0.07 },
                },
            interaction: { hover: true, hideEdgesOnDrag: true },
          },
        );
        net.on("selectNode", (p: { nodes: string[] }) => {
          if (p.nodes[0]) void selectNode(p.nodes[0]);
        });
        net.on("deselectNode", () => {
          setSelected(null);
          setDetail(null);
          highlight(null);
        });
        netRef.current = net;
        const c = g.counts ?? {};
        setCounts(
          `${c.memories ?? 0} memories · ${c.units ?? 0} units · ${c.keywords ?? 0} keywords · ${c.edges ?? 0} edges`,
        );
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      try {
        netRef.current?.destroy();
      } catch {
        // teardown must never throw during unmount
      }
      netRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectRoot, nonce]);

  useEffect(() => {
    applyVisibility();
  }, [applyVisibility, explore]);

  // Live refresh (#200 residual): the graph read is cheap (rusqlite on
  // desktop, gate read on web), so ride a visibility-aware interval — no
  // refresh while the tab is hidden. The interval does the cheap fetch +
  // signature compare itself and only bumps `nonce` (which triggers the
  // expensive destroy+recreate+physics rebuild) when memory ACTUALLY changed
  // since the last render — so an unchanged graph is never re-laid-out
  // (operator bug 2026-07-06). Manual selection/exploration survive because a
  // no-delta tick does nothing at all.
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void (async () => {
        try {
          const g = await memoryKgGraph(projectRoot ?? undefined);
          if (graphSignature(g) !== sigRef.current) setNonce((n) => n + 1);
        } catch {
          // transient read failure — skip this tick, try again next interval
        }
      })();
    }, 30_000);
    return () => window.clearInterval(t);
  }, [projectRoot]);

  const connected = useMemo(() => {
    if (!selected) return { memory: [] as MemoryKgNode[], unit: [] as MemoryKgNode[], keyword: [] as MemoryKgNode[] };
    const byId = new Map(graphRef.current.nodes.map((n) => [n.id, n]));
    const g = { memory: [] as MemoryKgNode[], unit: [] as MemoryKgNode[], keyword: [] as MemoryKgNode[] };
    for (const cid of new Set(adjRef.current[selected.id] ?? [])) {
      const cn = byId.get(cid);
      if (cn) g[cn.type].push(cn);
    }
    return g;
  }, [selected]);

  const inputCls =
    "w-full rounded-lg border border-castle-line bg-black/30 px-2.5 py-1.5 text-sm text-slate-100 placeholder:text-castle-mute focus:border-castle-allow/50 focus:outline-none";
  const btnCls =
    "rounded-lg border border-castle-line bg-white/[0.035] px-3 py-1.5 text-xs font-bold text-slate-300 transition hover:bg-white/[0.07]";

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      {/* ── Graph canvas ─────────────────────────────────────────────
          The vis container must have NO React children: vis-network owns
          and wipes its DOM, and React removing a child vis already deleted
          is a hard removeChild crash. The loading overlay is a SIBLING. */}
      <div className="relative min-w-0 flex-1">
        <div ref={containerRef} className="absolute inset-0" />
        {loading ? (
          <div className="absolute inset-0 grid place-items-center bg-black/40 text-sm text-castle-mute">
            Loading graph…
          </div>
        ) : null}
        {error ? (
          <div className="absolute left-4 top-4 max-w-md rounded-xl border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-xs text-castle-deny">
            {error}
          </div>
        ) : null}
      </div>

      {/* ── Sidebar: controls + detail ─────────────────────────────── */}
      <aside className="flex w-[340px] shrink-0 flex-col overflow-y-auto border-l border-castle-line bg-black/15">
        <div className="space-y-2.5 border-b border-castle-line p-3">
          <input
            type="search"
            placeholder="Search memories, units, keywords…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyUp={() => applyVisibility()}
            className={inputCls}
          />
          <label
            className="flex cursor-pointer items-center gap-2 text-xs text-castle-mute"
            title="Start from memories only; the graph grows as you explore"
          >
            <input
              type="checkbox"
              checked={explore}
              onChange={(e) => setExplore(e.target.checked)}
              className="accent-emerald-500"
            />
            Explore mode — graph grows as you click
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={btnCls} onClick={() => netRef.current?.fit({ animation: true })}>
              Fit
            </button>
            <button type="button" className={btnCls} onClick={() => setNonce((n) => n + 1)}>
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setShowCapture((v) => !v)}
              className={
                "rounded-lg border px-3 py-1.5 text-xs font-bold transition " +
                (showCapture
                  ? "border-castle-line text-castle-mute hover:text-slate-200"
                  : "border-castle-allow/40 bg-castle-allow/15 text-castle-allow hover:bg-castle-allow/25")
              }
            >
              {showCapture ? "Close capture" : "+ Capture"}
            </button>
          </div>
          {counts ? <div className="text-[11px] text-castle-mute">{counts}</div> : null}
        </div>

        {showCapture ? (
          <div className="space-y-2 border-b border-castle-line p-3">
            <div>
              <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
                Capture memory
              </div>
              <div className="mt-0.5 text-[11px] text-castle-mute">
                Governed write — durability rubric + sovereign guard enforced server-side.
              </div>
            </div>
            <select
              value={capKind}
              onChange={(e) => setCapKind(e.target.value)}
              disabled={capBusy}
              className={inputCls}
            >
              {CAPTURE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <textarea
              rows={5}
              placeholder="The durable fact (plans/logs/bug reports are rejected by doctrine)"
              value={capContent}
              onChange={(e) => setCapContent(e.target.value)}
              disabled={capBusy}
              className={inputCls + " resize-y"}
            />
            <input
              type="text"
              placeholder="Target hint (optional, e.g. rules/workflow.md)"
              value={capHint}
              onChange={(e) => setCapHint(e.target.value)}
              disabled={capBusy}
              className={inputCls}
            />
            <button
              type="button"
              onClick={() => void submitCapture()}
              disabled={capBusy}
              className="rounded-lg border border-castle-allow/40 bg-castle-allow/15 px-3 py-1.5 text-xs font-bold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-40"
            >
              {capBusy ? "Capturing…" : "Capture"}
            </button>
            {capMsg ? <div className="break-all text-[11px] text-castle-mute">{capMsg}</div> : null}
          </div>
        ) : null}

        <div className="flex-1 p-3">
          {selected ? (
            <>
              <div className="pb-2">
                <div className="text-sm font-black text-slate-100">{selected.label}</div>
                <div className="mt-0.5 text-[11px] text-castle-mute">
                  {selected.type}
                  {selected.type === "memory" ? ` · ${selected.kind}` : ""}
                  {selected.path ? ` · ${selected.path}` : selected.file ? ` · ${selected.file}` : ""}
                </div>
              </div>
              {detail?.ok && detail.content ? (
                <div className="rounded-xl border border-castle-line bg-castle-card p-3">
                  <div className="text-sm font-bold text-slate-100">{detail.title || detail.path}</div>
                  <pre className="mt-2 max-h-[45vh] overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-slate-300">
                    {detail.content}
                  </pre>
                  <div className="mt-2 text-[10px] text-castle-mute">
                    source: {detail.source || "?"} · status: {detail.status || "?"} · updated: {detail.updated_at || "?"}
                  </div>
                  <button
                    type="button"
                    className="mt-2 rounded-lg border border-castle-info/40 bg-castle-info/10 px-2.5 py-1 text-[11px] font-bold text-castle-info transition hover:bg-castle-info/20"
                    onClick={() => {
                      // Doctrine edit-flow: editing a memory IS re-capturing
                      // into its canonical file — pre-target the capture form
                      // at the selected memory (append semantics, governed).
                      setCapHint(detail.path ?? "");
                      if (detail.kind && CAPTURE_KINDS.includes(detail.kind)) setCapKind(detail.kind);
                      setShowCapture(true);
                    }}
                  >
                    Edit — append to this memory
                  </button>
                </div>
              ) : detail && !detail.ok ? (
                <div className="rounded-xl border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-xs text-castle-deny">
                  {detail._detail || "memory body unavailable"}
                </div>
              ) : null}
              {(["memory", "unit", "keyword"] as const).map((t) =>
                connected[t].length ? (
                  <details key={t} open className="mt-3">
                    <summary className="cursor-pointer text-[10px] font-black uppercase tracking-widest text-castle-mute">
                      {t === "memory" ? "Memories" : t === "unit" ? "Code units" : "Keywords"} ({connected[t].length})
                    </summary>
                    <ul className="mt-1 max-h-48 overflow-y-auto">
                      {connected[t].map((cn) => (
                        <li
                          key={cn.id}
                          onClick={() => void selectNode(cn.id)}
                          className="cursor-pointer truncate rounded px-2 py-1 text-xs text-castle-mute transition hover:bg-white/[0.04] hover:text-slate-200"
                        >
                          {cn.label}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null,
              )}
            </>
          ) : (
            <>
              {/* #237: the anchor-health stats live HERE now (they used to
                  render raw under every page, breaking layouts). */}
              <MemoryAnchorHealthPanel projectRoot={projectRoot} />
              <div className="mt-3 text-xs leading-5 text-castle-mute">
                Select a node to read the full memory and grow its branch.
                <br />
                <br />
                Memory writes stay governed: capture/edit via the{" "}
                <code className="text-slate-300">memory_capture</code> tool (agent or gate surface) —
                the dashboard shows truth, the throne seals law.
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

export default MemoryPage;
