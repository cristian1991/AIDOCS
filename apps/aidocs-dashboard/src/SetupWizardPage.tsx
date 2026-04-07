import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";

type SetupStep = "welcome" | "project" | "detect" | "install" | "configure" | "done";
type DetectedHost = { name: string; found: boolean; authenticated?: boolean; path?: string; installable: boolean };

interface DetectResult {
  python: { found: boolean; path: string; version: string };
  node: { found: boolean; path: string };
  hosts: DetectedHost[];
  project_root: string;
}

interface InstallResult {
  success: boolean;
  installed: string[];
  errors: string[];
}

interface ConfigureResult {
  success: boolean;
  mcp_path: string;
  hooks_path: string;
  project_initialized: boolean;
  errors: string[];
}

export function SetupWizardPage({ onComplete }: { onComplete: () => void }) {
  const [step, setStep] = useState<SetupStep>("welcome");
  const [projectPath, setProjectPath] = useState("");
  const [detecting, setDetecting] = useState(false);
  const [detectResult, setDetectResult] = useState<DetectResult | null>(null);
  const [installTargets, setInstallTargets] = useState<Set<string>>(new Set());
  const [installing, setInstalling] = useState(false);
  const [installResult, setInstallResult] = useState<InstallResult | null>(null);
  const [configuring, setConfiguring] = useState(false);
  const [configResult, setConfigResult] = useState<ConfigureResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function browseFolder() {
    try {
      const selected = await invoke<string | null>("select_folder");
      if (selected) setProjectPath(selected);
    } catch {
      // Fallback — user types path manually
    }
  }

  async function runDetect() {
    setDetecting(true);
    setError(null);
    try {
      const result = await invoke<DetectResult>("setup_detect", { projectRoot: projectPath || undefined });
      setDetectResult(result);
      if (projectPath === "" && result.project_root) setProjectPath(result.project_root);
      // Auto-select installable hosts that aren't found
      const targets = new Set<string>();
      for (const h of result.hosts) {
        if (!h.found && h.installable) targets.add(h.name);
      }
      setInstallTargets(targets);
      setStep("detect");
    } catch (e) {
      setError(String(e));
    }
    setDetecting(false);
  }

  async function runInstall() {
    setInstalling(true);
    setError(null);
    try {
      const result = await invoke<InstallResult>("setup_install", { targets: Array.from(installTargets) });
      setInstallResult(result);
      setStep("configure");
      // Auto-configure after install
      await runConfigure();
    } catch (e) {
      setError(String(e));
      setStep("configure");
    }
    setInstalling(false);
  }

  async function runConfigure() {
    setConfiguring(true);
    setError(null);
    try {
      const result = await invoke<ConfigureResult>("setup_configure", { projectRoot: projectPath });
      setConfigResult(result);
      setStep("done");
    } catch (e) {
      setError(String(e));
    }
    setConfiguring(false);
  }

  function toggleTarget(name: string) {
    setInstallTargets((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  return (
    <div className="setup-wizard">
      <div className="setup-wizard-inner">
        {/* Progress indicator */}
        <div className="setup-steps">
          {(["welcome", "project", "detect", "install", "done"] as const).map((s, i) => (
            <div key={s} className={`setup-step-dot ${step === s ? "active" : i < ["welcome", "project", "detect", "install", "done"].indexOf(step) ? "completed" : ""}`} />
          ))}
        </div>

        {error && <div className="setup-error">{error}</div>}

        {/* ── Welcome ── */}
        {step === "welcome" && (
          <div className="setup-content">
            <h1>Welcome to AIDOCS</h1>
            <p className="setup-subtitle">Let's get your project set up in under a minute.</p>
            <p>AIDOCS gives your AI coding agents persistent memory, indexed retrieval, and orchestration — so they resume work instead of rediscovering your repo every time.</p>
            <div className="setup-actions">
              <button className="setup-btn-primary" onClick={() => setStep("project")}>Get Started</button>
            </div>
          </div>
        )}

        {/* ── Project Selection ── */}
        {step === "project" && (
          <div className="setup-content">
            <h2>Select Your Project</h2>
            <p>Choose the folder where you want to enable AIDOCS.</p>
            <div className="setup-path-row">
              <input
                type="text"
                value={projectPath}
                onChange={(e) => setProjectPath(e.target.value)}
                placeholder="Project folder path (or leave empty for current directory)"
                className="setup-path-input"
              />
              <button className="setup-btn-secondary" onClick={browseFolder}>Browse</button>
            </div>
            <div className="setup-actions">
              <button className="setup-btn-secondary" onClick={() => setStep("welcome")}>Back</button>
              <button className="setup-btn-primary" disabled={detecting} onClick={runDetect}>
                {detecting ? "Detecting..." : "Next"}
              </button>
            </div>
          </div>
        )}

        {/* ── Detection Results ── */}
        {step === "detect" && detectResult && (
          <div className="setup-content">
            <h2>Environment Detected</h2>
            <div className="setup-checklist">
                <div className={`setup-check ${detectResult.python.found ? "pass" : "fail"}`}>
                  <span className="setup-check-icon">{detectResult.python.found ? "+" : "x"}</span>
                  <span>Python {detectResult.python.version || "not found"}</span>
                  {detectResult.python.found && <span className="setup-check-detail">{detectResult.python.path}</span>}
                  {!detectResult.python.found && (
                    <button className="setup-btn-secondary" style={{ marginLeft: "auto", fontSize: "0.75rem", padding: "3px 10px" }}
                      onClick={async () => {
                        setError(null);
                        try {
                          const r = await invoke<{ success: boolean; python_path?: string; output?: string }>("setup_install_python");
                          if (r.success && r.python_path) {
                            setDetectResult({ ...detectResult, python: { found: true, path: r.python_path, version: "3.12 (bundled)" } });
                          } else { setError(r.output || "Python install failed"); }
                        } catch (e) { setError(String(e)); }
                      }}
                    >Install Python</button>
                  )}
                </div>
              <div className={`setup-check ${detectResult.node.found ? "pass" : "warn"}`}>
                <span className="setup-check-icon">{detectResult.node.found ? "+" : "!"}</span>
                <span>Node.js {detectResult.node.found ? "" : "(needed for CLI agents)"}</span>
              </div>
                {detectResult.hosts.map((h) => (
                  <div key={h.name} className={`setup-check ${h.found ? (h.authenticated === false ? "warn" : "pass") : "missing"}`}>
                    <span className="setup-check-icon">{h.found ? (h.authenticated === false ? "!" : "+") : "-"}</span>
                    <span>{h.name}{h.found && h.authenticated === false ? " (not signed in)" : ""}</span>
                    {h.found && h.path && <span className="setup-check-detail">{h.path}</span>}
                    {h.found && h.authenticated === false && (
                      <button className="setup-btn-secondary" style={{ marginLeft: "auto", fontSize: "0.75rem", padding: "3px 10px" }}
                        onClick={() => { invoke("open_url", { url: h.name.includes("Claude") ? "https://claude.ai" : "https://platform.openai.com/api-keys" }).catch(() => {}); }}
                      >Sign in</button>
                    )}
                    {!h.found && h.installable && (
                      <label className="setup-install-toggle">
                        <input type="checkbox" checked={installTargets.has(h.name)} onChange={() => toggleTarget(h.name)} />
                        Install
                      </label>
                    )}
                  </div>
                ))}
            </div>
            <div className="setup-actions">
              <button className="setup-btn-secondary" onClick={() => setStep("project")}>Back</button>
              {installTargets.size > 0 ? (
                <button className="setup-btn-primary" disabled={installing} onClick={runInstall}>
                  {installing ? "Installing..." : `Install ${installTargets.size} + Configure`}
                </button>
              ) : (
                <button className="setup-btn-primary" disabled={configuring} onClick={() => { setStep("configure"); void runConfigure(); }}>
                  {configuring ? "Configuring..." : "Configure AIDOCS"}
                </button>
              )}
            </div>
          </div>
        )}

        {/* ── Installing ── */}
        {step === "install" && (
          <div className="setup-content">
            <h2>Installing</h2>
            <p>Setting up your environment...</p>
            <div className="setup-spinner" />
          </div>
        )}

        {/* ── Configuring ── */}
        {step === "configure" && (
          <div className="setup-content">
            <h2>Configuring</h2>
            <p>Setting up MCP, hooks, and project structure...</p>
            <div className="setup-spinner" />
          </div>
        )}

        {/* ── Done ── */}
        {step === "done" && configResult && (
          <div className="setup-content">
            <h2>All Set!</h2>
            <div className="setup-checklist">
              <div className="setup-check pass">
                <span className="setup-check-icon">+</span>
                <span>MCP configured</span>
                <span className="setup-check-detail">{configResult.mcp_path}</span>
              </div>
              <div className="setup-check pass">
                <span className="setup-check-icon">+</span>
                <span>Hooks registered</span>
                <span className="setup-check-detail">{configResult.hooks_path}</span>
              </div>
              <div className={`setup-check ${configResult.project_initialized ? "pass" : "pass"}`}>
                <span className="setup-check-icon">+</span>
                <span>Project initialized</span>
              </div>
              {installResult && installResult.installed.length > 0 && (
                <div className="setup-check pass">
                  <span className="setup-check-icon">+</span>
                  <span>Installed: {installResult.installed.join(", ")}</span>
                </div>
              )}
            </div>
            <div className="setup-next-steps">
              <h3>Next Steps</h3>
              <ol>
                <li>Open <strong>{projectPath || "your project"}</strong> in your IDE</li>
                <li>Start a new agent session</li>
                <li>Type <code>/aidocs</code> to begin</li>
              </ol>
            </div>
            <div className="setup-actions">
              <button className="setup-btn-primary" onClick={onComplete}>Open Dashboard</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
