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
 * writes go through the governed memory_capture tool (promotion /
 * source-classification doctrine — "law enters only through the throne").
 * The panel links the operator to that path instead of minting an ungated
 * dashboard write. Desktop-native capture UI is a filed follow-up.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataSet } from "vis-network/standalone/esm/vis-network";
import { Network } from "vis-network/standalone/esm/vis-network";
import {
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
const UNIT_COLOR = "#1f6feb";
const KW_COLOR = "#484f58";
const DIM = { background: "#1c2128", border: "#1c2128" };

function nodeColor(n: MemoryKgNode): { background: string; border: string } {
  if (n.type === "unit") return { background: UNIT_COLOR, border: "#388bfd" };
  if (n.type === "keyword") return { background: KW_COLOR, border: "#6e7681" };
  const c = KIND_COLORS[n.group] ?? "#6e7681";
  return { background: c, border: c };
}

export function MemoryPage({ projectRoot }: MemoryPageProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const netRef = useRef<Network | null>(null);
  const nodesRef = useRef<DataSet<Record<string, unknown>> | null>(null);
  const graphRef = useRef<{ nodes: MemoryKgNode[]; edges: MemoryKgEdge[] }>({ nodes: [], edges: [] });
  const adjRef = useRef<Record<string, string[]>>({});
  const revealedRef = useRef<Set<string>>(new Set());
  const exploreRef = useRef(true);

  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [counts, setCounts] = useState<string>("");
  const [explore, setExplore] = useState(true);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [selected, setSelected] = useState<MemoryKgNode | null>(null);
  const [nonce, setNonce] = useState(0);
  exploreRef.current = explore;

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
        graphRef.current = { nodes: g.nodes ?? [], edges: g.edges ?? [] };
        revealedRef.current = new Set();
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
            hidden: exploreRef.current && n.type !== "memory",
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

  return (
    <div className="memory-page">
      <div className="memory-toolbar">
        <input
          type="search"
          placeholder="search memories, units, keywords…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyUp={() => applyVisibility()}
        />
        <label title="Start from memories only; the graph grows as you explore">
          <input type="checkbox" checked={explore} onChange={(e) => setExplore(e.target.checked)} />
          explore mode
        </label>
        <button type="button" onClick={() => netRef.current?.fit({ animation: true })}>
          fit
        </button>
        <button type="button" onClick={() => setNonce((n) => n + 1)}>
          refresh
        </button>
        <span className="memory-counts">{counts}</span>
      </div>
      {error ? <div className="memory-error">{error}</div> : null}
      <div className="memory-body">
        {/* The vis container must have NO React children: vis-network owns and
            wipes its DOM, and React removing a child vis already deleted is a
            hard removeChild crash. The loading overlay is a SIBLING. */}
        <div className="memory-net-wrap">
          <div ref={containerRef} className="memory-net" />
          {loading ? <div className="memory-loading">Loading graph…</div> : null}
        </div>
        <div className="memory-side">
          {selected ? (
            <>
              <div className="memory-dhead">
                <b>{selected.label}</b>
                <div className="memory-dsub">
                  {selected.type}
                  {selected.type === "memory" ? ` · ${selected.kind}` : ""}
                  {selected.path ? ` · ${selected.path}` : selected.file ? ` · ${selected.file}` : ""}
                </div>
              </div>
              {detail?.ok && detail.content ? (
                <div className="memory-content">
                  <h2>{detail.title || detail.path}</h2>
                  <pre>{detail.content}</pre>
                  <div className="memory-meta">
                    source: {detail.source || "?"} · status: {detail.status || "?"} · updated: {detail.updated_at || "?"}
                  </div>
                </div>
              ) : detail && !detail.ok ? (
                <div className="memory-error">{detail._detail || "memory body unavailable"}</div>
              ) : null}
              {(["memory", "unit", "keyword"] as const).map((t) =>
                connected[t].length ? (
                  <details key={t} open>
                    <summary>
                      {t === "memory" ? "Memories" : t === "unit" ? "Code units" : "Keywords"} ({connected[t].length})
                    </summary>
                    <ul>
                      {connected[t].map((cn) => (
                        <li key={cn.id} onClick={() => void selectNode(cn.id)}>
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
              <div className="memory-hint">
                Select a node to read the full memory and grow its branch.
                <br />
                <br />
                Memory writes stay governed: capture/edit via the <code>memory_capture</code> tool
                (agent or gate surface) — the dashboard shows truth, the throne seals law.
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default MemoryPage;
