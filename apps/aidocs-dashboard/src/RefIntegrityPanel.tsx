import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { brokenReferencesCheck, type BrokenReferencesReport } from "./dashboardApi";

type Props = { projectRoot: string | null };

const btn: CSSProperties = {
  borderRadius: 8,
  border: "1px solid #334155",
  padding: "6px 12px",
  fontSize: 13,
  color: "#e2e8f0",
  background: "transparent",
  cursor: "pointer",
};

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div style={{ border: "1px solid #334155", borderRadius: 8, padding: "8px 16px" }}>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
      <div style={{ fontSize: 12, color: "#94a3b8" }}>{label}</div>
    </div>
  );
}

export function RefIntegrityPanel({ projectRoot }: Props) {
  const [report, setReport] = useState<BrokenReferencesReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await brokenReferencesCheck(projectRoot ?? undefined);
      if (!res.ok || !res.report) {
        setError("Ref-integrity read failed — no project bound or the index is unavailable.");
        setReport(null);
      } else {
        setReport(res.report);
      }
    } catch (e) {
      setError(String(e));
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [projectRoot]);

  useEffect(() => {
    void load();
  }, [load]);

  const close = () => {
    window.location.hash = "";
  };

  return (
    <div style={{ padding: 16, overflowY: "auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Reference Integrity</h2>
        <button type="button" onClick={() => void load()} disabled={loading} style={btn}>
          {loading ? "Checking…" : "Refresh"}
        </button>
        <button type="button" onClick={close} style={btn}>
          Close
        </button>
      </div>
      <p style={{ color: "#94a3b8", fontSize: 13, marginTop: 0 }}>
        References whose token has no resolving definition — declared via Language-Descriptor
        <code> reference_patterns</code> + <code>definition_source</code>.
      </p>
      {error ? <div style={{ color: "#f87171", marginBottom: 12 }}>{error}</div> : null}
      {report ? (
        <>
          <div style={{ display: "flex", gap: 16, margin: "12px 0" }}>
            <Stat label="Total broken" value={report.total_broken} />
            <Stat label="Kinds checked" value={report.kinds.length} />
          </div>
          <div style={{ fontSize: 12, color: "#fbbf24", marginBottom: 12 }}>
            {report.evidence.kind} — {report.evidence.limitations}
          </div>
          {report.kinds.map((k) => (
            <details
              key={k.kind}
              open={(k.broken_count ?? 0) > 0}
              style={{
                border: "1px solid #334155",
                borderRadius: 8,
                padding: "8px 12px",
                marginBottom: 8,
              }}
            >
              <summary style={{ cursor: "pointer" }}>
                <strong>{k.kind}</strong>{" "}
                {k.resolvable
                  ? `— ${k.broken_count ?? 0} broken / ${k.reference_count} refs / ${
                      k.definition_count ?? 0
                    } defs`
                  : `— unresolvable (${k.reason ?? "no definition_source"})`}
              </summary>
              {k.broken_sample && k.broken_sample.length > 0 ? (
                <table
                  style={{ width: "100%", fontSize: 13, marginTop: 8, borderCollapse: "collapse" }}
                >
                  <thead>
                    <tr style={{ textAlign: "left", color: "#94a3b8" }}>
                      <th>Path</th>
                      <th>Line</th>
                      <th>Token</th>
                    </tr>
                  </thead>
                  <tbody>
                    {k.broken_sample.map((b, i) => (
                      <tr key={i}>
                        <td>{b.path}</td>
                        <td>{b.line}</td>
                        <td>
                          <code>{b.token}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
              {k.truncated ? (
                <div style={{ color: "#94a3b8", fontSize: 12 }}>…sample truncated</div>
              ) : null}
            </details>
          ))}
          {report.total_broken === 0 ? (
            <div style={{ color: "#4ade80" }}>No broken references. ✓</div>
          ) : null}
        </>
      ) : !error && !loading ? (
        <div style={{ color: "#94a3b8" }}>No data.</div>
      ) : null}
    </div>
  );
}
