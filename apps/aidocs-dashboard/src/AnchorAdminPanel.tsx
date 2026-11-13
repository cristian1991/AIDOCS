import { useEffect, useRef, useState } from "react";

import {
  ANCHOR_OPS,
  ANCHOR_TIERS,
  anchorAdmin,
  type AnchorAdminResult,
  type AnchorOp,
  type AnchorRow,
} from "./dashboardApi";
import { getViewedProjectId } from "./platform/transport";

/** The project a call lands in: the desktop root and the web's viewed project_id. */
function projectScope(projectRoot: string | null): string {
  return `${projectRoot ?? ""}\n${getViewedProjectId() ?? ""}`;
}

/** A preview and exactly what it was made for. */
interface Plan {
  result: AnchorAdminResult;
  scope: string;
  target: string;
  op: AnchorOp;
  to: string;
}

/**
 * #1187 anchor admin: list, preview and apply pin / unpin / downgrade / delete
 * on the anchors one target names (a symbol, a project-relative file or an
 * anchor id) in the viewed project. Org OWNER/ADMIN only; the server judges
 * that on every call.
 *
 * Apply always sends the fingerprint of the preview the operator saw, so a
 * change since then is refused (state_changed) instead of applied blind, and
 * the server's confirm summary is what the operator says yes to.
 *
 * A preview is bound to the project it was made in: two projects can share a
 * fingerprint, so after a project switch the old plan is dropped and an answer
 * that arrives late is ignored. Apply needs a preview of the current project,
 * target, op and tier.
 */
export function AnchorAdminPanel({ projectRoot }: { projectRoot: string | null }) {
  const [target, setTarget] = useState("");
  const [op, setOp] = useState<AnchorOp>("downgrade");
  const [to, setTo] = useState<string>("semantic_guess");
  const [rows, setRows] = useState<{ scope: string; anchors: AnchorRow[] } | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const root = projectRoot ?? undefined;
  const scope = projectScope(projectRoot);
  // A new object on every switch, so after A -> B -> A the first visit's
  // requests are still stale even though the scope reads the same again.
  const visit = useRef({ scope });
  if (visit.current.scope !== scope) visit.current = { scope };
  const needsTier = op === "downgrade" || op === "unpin";
  const tier = needsTier ? to : "";

  useEffect(() => {
    setRows(null);
    setPlan(null);
    setNotice("");
    setBusy(false);
  }, [scope]);

  async function run(step: (live: () => boolean) => Promise<void>) {
    const started = visit.current;
    const live = () => visit.current === started;
    setBusy(true);
    setNotice("");
    try {
      await step(live);
    } catch (err) {
      if (live()) setNotice(String(err));
    } finally {
      if (live()) setBusy(false);
    }
  }

  const list = () =>
    run(async (live) => {
      const at = scope;
      const r = await anchorAdmin("list", target.trim(), { projectRoot: root });
      if (!live()) return;
      setRows({ scope: at, anchors: r.anchors ?? [] });
      setPlan(null);
    });

  const preview = () =>
    run(async (live) => {
      setPlan(null);
      const made = { scope, target: target.trim(), op, to: tier };
      const r = await anchorAdmin("preview", made.target, { op, to: tier, projectRoot: root });
      if (!live()) return;
      setRows({ scope: made.scope, anchors: r.anchors ?? [] });
      setPlan({ result: r, ...made });
    });

  const current =
    plan &&
    plan.scope === scope &&
    plan.target === target.trim() &&
    plan.op === op &&
    plan.to === tier
      ? plan
      : null;

  const apply = () =>
    run(async (live) => {
      const p = current;
      if (!p?.result.fingerprint || p.scope !== projectScope(projectRoot)) return;
      const r = await anchorAdmin("apply", p.target, {
        op: p.op,
        to: p.to,
        fingerprint: p.result.fingerprint,
        projectRoot: root,
      });
      if (!live()) return;
      setPlan(null);
      setNotice(r.message || `Applied: ${r.count ?? 0} anchor(s) changed.`);
      const fresh = await anchorAdmin("list", p.target, { projectRoot: root });
      if (!live()) return;
      setRows({ scope: p.scope, anchors: fresh.anchors ?? [] });
    });

  const anchors = rows && rows.scope === scope ? rows.anchors : null;
  const changes = current?.result.changes ?? [];

  return (
    <div className="card anchor-admin" data-testid="anchor-admin">
      <h3>Anchor admin</h3>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        <input
          aria-label="anchor target"
          data-testid="anchor-target"
          className="min-w-0 flex-1"
          placeholder="symbol, file path or anchor id"
          value={target}
          onChange={(e) => {
            setTarget(e.target.value);
            setPlan(null);
          }}
        />
        <button type="button" disabled={busy || !target.trim()} onClick={list} data-testid="anchor-list">
          List
        </button>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        <select
          aria-label="anchor op"
          data-testid="anchor-op"
          value={op}
          onChange={(e) => {
            setOp(e.target.value as AnchorOp);
            setPlan(null);
          }}
        >
          {ANCHOR_OPS.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        {needsTier && (
          <select
            aria-label="anchor tier"
            data-testid="anchor-to"
            value={to}
            onChange={(e) => {
              setTo(e.target.value);
              setPlan(null);
            }}
          >
            {ANCHOR_TIERS.filter((t) => t !== "operator_pinned").map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          disabled={busy || !target.trim()}
          onClick={preview}
          data-testid="anchor-preview"
        >
          Preview
        </button>
        <button
          type="button"
          disabled={busy || !current || changes.length === 0}
          onClick={apply}
          data-testid="anchor-apply"
        >
          Apply
        </button>
      </div>
      {anchors && (
        <table className="mt-2 w-full text-xs" data-testid="anchor-rows">
          <thead>
            <tr>
              <th>id</th>
              <th>symbol / file</th>
              <th>memory</th>
              <th>tier</th>
              {current && <th>after</th>}
            </tr>
          </thead>
          <tbody>
            {anchors.map((a) => {
              const change = changes.find((c) => c.anchor_id === a.anchor_id);
              return (
                <tr key={a.anchor_id}>
                  <td>{a.anchor_id}</td>
                  <td>{a.symbol_name || a.file_path}</td>
                  <td>{a.memory_path ?? ""}</td>
                  <td>{a.confidence}</td>
                  {current && <td>{change ? change.after : "unchanged"}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {anchors && anchors.length === 0 && (
        <div className="mt-2 text-xs text-castle-mute">No anchor matches this target exactly.</div>
      )}
      {current && changes.length === 0 && (
        <div className="mt-2 text-xs text-castle-mute" data-testid="anchor-nothing">
          This {op} changes no anchor.
        </div>
      )}
      {notice && (
        <div className="mt-2 text-xs" data-testid="anchor-notice">
          {notice}
        </div>
      )}
    </div>
  );
}
