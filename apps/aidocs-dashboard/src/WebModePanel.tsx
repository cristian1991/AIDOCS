import { useEffect, useState } from "react";

import { beginLogin, isConnected, logout } from "./platform/webAuth";
import {
  getMode,
  loadGateConnection,
  onScopeChange,
  setMode,
  type Mode,
} from "./webmcpScope";
import { ModeSwitcher } from "./ModeSwitcher";
import { UserMenu } from "./UserMenu";

// The WEB build's mode + connection control (the desktop build keeps WebmcpModePanel's
// Tauri login). Modes are build-filtered (web = webmcp + cloudagent). Login is the browser
// PKCE flow (webAuth). The Conductor is the only mode-gated capability — locked here in
// both web modes (webmcp permanently; cloudagent until ADB AI-agent + dockerization).
export function WebModePanel() {
  const [mode, setModeState] = useState<Mode>(() => getMode());
  const [connected, setConnected] = useState<boolean>(() => isConnected());
  useEffect(
    () =>
      onScopeChange(() => {
        setModeState(getMode());
        setConnected(isConnected());
      }),
    [],
  );

  const conn = loadGateConnection();

  function pick(m: Mode) {
    setMode(m);
    setModeState(m);
  }

  return (
    <div className="webmode-panel" style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <ModeSwitcher mode={mode} onPick={pick} />

      <div className="webmode-conn" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {connected ? (
          <>
            <UserMenu
              email={conn?.email}
              scope={conn?.scope}
              onSignOut={() => {
                logout();
                setConnected(false);
              }}
            />
          </>
        ) : (
          <>
            <span className="muted">Not connected.</span>
            <button
              className="rounded-lg border border-castle-allow/40 bg-castle-allow/15 px-3 py-1.5 text-sm font-medium text-castle-allow transition hover:bg-castle-allow/25"
              onClick={() => void beginLogin()}
            >
              Sign in with CodeNexus
            </button>
          </>
        )}
      </div>

      {/* GitHub onboarding moved to the empty-state link + the console card — keep the header compact */}
      {false ? (
        <details className="rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium text-slate-200">
            Connect a GitHub account (for private-repo import)
          </summary>
          <div className="mt-2 space-y-2 text-castle-mute">
            <p>
              To import private repos with the <strong>+</strong> button, your org needs a
              GitHub credential connected. The token is stored on the CodeNexus identity
              service — it never touches the gate.
            </p>
            <ol className="ml-4 list-decimal space-y-1">
              <li>
                Create a GitHub Personal Access Token — classic with <code>repo</code> scope,
                or a fine-grained token with read access to the repos you'll import.
              </li>
              <li>
                Open the CodeNexus console and paste it under your org's GitHub connection
                (superadmin only).
              </li>
              <li>
                Back here, click <strong>+</strong> and paste a repository URL to import it.
              </li>
            </ol>
            <div className="flex flex-wrap gap-3 pt-1">
              <a
                className="text-castle-allow underline"
                href="https://github.com/settings/tokens"
                target="_blank"
                rel="noopener noreferrer"
              >
                Create a GitHub token →
              </a>
              <a
                className="text-castle-allow underline"
                href="https://codenexus.cloud"
                target="_blank"
                rel="noopener noreferrer"
              >
                Manage GitHub connection in the console →
              </a>
              <a
                className="text-castle-mute underline"
                href="https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens"
                target="_blank"
                rel="noopener noreferrer"
              >
                Token docs →
              </a>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  );
}
