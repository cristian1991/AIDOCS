import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { loadGateConnection } from "./webmcpScope";

// #1074 round 6 Phase B -- the signed-in user's per-install native credentials.
//
// Every AIDOCS desktop install enrolls its OWN credential (a server-issued
// `ogni_*` install id bound to a P-256 key that never leaves that machine's
// Python process). This view lists them and revokes them, through the gate's
// EXISTING owner routes, authenticated by the gate bearer this webview already
// holds -- no new auth path:
//
//   GET  /v1/native/installs          -> { installs: [...] }
//   POST /v1/native/installs/revoke   { install_id } -> revokes the install,
//                                     its keys and its token family
//
// "This machine" comes from the kernel's `native_install_status`, which
// forwards ONLY non-secret facts (install id, key id, status). No key material
// is read, received or held here.

const GATE = "https://mcp.codenexus.cloud";
const LIST_URL = GATE + "/v1/native/installs";
const REVOKE_URL = GATE + "/v1/native/installs/revoke";

export type NativeInstall = {
  install_id: string;
  client_id?: string;
  created_at: string;
  last_seen_at: string;
  revoked_at: string;
  key_ids?: string[];
};

export type LocalInstallStatus = {
  status?: string;
  install_id?: string;
  key_id?: string;
  platform?: string;
  message?: string;
};

function bearer(): string {
  const cred = loadGateConnection()?.accessToken;
  if (!cred) throw new Error("Not signed in to the gate — sign in with CodeNexus first.");
  return cred;
}

export async function listNativeInstalls(): Promise<NativeInstall[]> {
  const res = await tauriFetch(LIST_URL, {
    method: "GET",
    headers: { Authorization: "Bearer " + bearer() },
  });
  if (!res.ok) throw new Error("Could not list installs (" + res.status + ")");
  const body = await res.json();
  return Array.isArray(body?.installs) ? (body.installs as NativeInstall[]) : [];
}

export async function revokeNativeInstall(installId: string): Promise<void> {
  const res = await tauriFetch(REVOKE_URL, {
    method: "POST",
    headers: { Authorization: "Bearer " + bearer(), "Content-Type": "application/json" },
    body: JSON.stringify({ install_id: installId }),
  });
  if (!res.ok) throw new Error("Revoke failed (" + res.status + ")");
}

async function localStatus(): Promise<LocalInstallStatus | null> {
  try {
    return await invoke<LocalInstallStatus>("native_install_status", {});
  } catch {
    return null;
  }
}

function day(iso: string): string {
  return iso ? iso.replace("T", " ").replace(/Z$/, " UTC") : "—";
}

export function NativeInstallsPanel({ onNewInstall }: { onNewInstall?: () => void }) {
  const [installs, setInstalls] = useState<NativeInstall[] | null>(null);
  const [mine, setMine] = useState<LocalInstallStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    const [list, st] = await Promise.allSettled([listNativeInstalls(), localStatus()]);
    if (st.status === "fulfilled") setMine(st.value);
    if (list.status === "fulfilled") setInstalls(list.value);
    else setError(list.reason instanceof Error ? list.reason.message : String(list.reason));
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function revoke(inst: NativeInstall) {
    const isMine = mine?.install_id === inst.install_id;
    const warn = isMine
      ? "Revoke THIS machine's install? It is signed out now; the next sign-in enrolls a new install."
      : "Revoke install " + inst.install_id + "? That machine is signed out and must sign in again.";
    if (!window.confirm(warn)) return;
    setBusy(inst.install_id);
    try {
      await revokeNativeInstall(inst.install_id);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="rounded-xl border border-castle-line bg-black/20 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-semibold text-slate-200">Native installs</span>
        {onNewInstall ? (
          <button
            type="button"
            onClick={onNewInstall}
            title="Enroll a NEW install for this machine (a new key and install id)"
            className="rounded border border-castle-line px-2 py-1 text-[11px] text-slate-300 hover:bg-white/[0.06]"
          >
            Sign in as a new install
          </button>
        ) : null}
      </div>
      {mine ? (
        <p className="mb-2 text-[11px] text-castle-mute" data-testid="this-machine-status">
          This machine: {mine.install_id || "no install"} · {mine.status || "unknown"}
          {mine.message ? " — " + mine.message : ""}
        </p>
      ) : null}
      {error ? <p className="text-xs text-castle-deny">{error}</p> : null}
      {installs === null ? (
        <p className="text-[11px] text-castle-mute">Loading installs…</p>
      ) : installs.length === 0 ? (
        <p className="text-[11px] text-castle-mute">No native installs enrolled yet.</p>
      ) : (
        <ul className="space-y-1">
          {installs.map((inst) => {
            const revoked = Boolean(inst.revoked_at);
            const isMine = mine?.install_id === inst.install_id;
            return (
              <li
                key={inst.install_id}
                data-testid={"install-" + inst.install_id}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-slate-300">{inst.install_id}</div>
                  <div className="text-[10px] text-castle-mute">
                    created {day(inst.created_at)} · last used {day(inst.last_seen_at)}
                    {isMine ? " · this machine" : ""}
                  </div>
                </div>
                <span
                  className={
                    "rounded px-1.5 py-0.5 text-[10px] " +
                    (revoked ? "bg-rose-500/10 text-rose-300" : "bg-white/[0.06] text-castle-allow")
                  }
                >
                  {revoked ? "revoked" : "active"}
                </span>
                {revoked ? null : (
                  <button
                    type="button"
                    data-testid={"revoke-" + inst.install_id}
                    disabled={busy !== null}
                    onClick={() => void revoke(inst)}
                    className="rounded border border-rose-400/40 px-2 py-0.5 text-[10px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
                  >
                    {busy === inst.install_id ? "Revoking…" : "Revoke"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
