// Entitlement model + upgrade-intent bus for the dashboard.
//
// 120% doctrine (scratch/co-co reports/120%.md §8 "no fake levers", §22 "law
// cannot be tiered", §23 "FAIL AS FAKE LEVER"): the GATE is the source of truth
// AND the enforcer. This module only reflects what the gate session already
// proves, and ROUTES the operator to /upgrade. It never fabricates entitlement
// it cannot see, and it never grants capability the gate would refuse — so
// deleting any UI gate in devtools just reveals controls the gate still rejects.
//
// Provisioning (CloudAgent runtime, WebAgent seats) is tiered. Law (RBAC, policy,
// audit, isolation) is not — those stay enforced server-side regardless of plan.

import { getMode, loadGateConnection, isWebBuild, type Mode } from "./webmcpScope";

// ── SKUs + upgrade destination ──────────────────────────────────────────────
export type Sku = "webmcp-seats" | "cloudagent";

export const UPGRADE_BASE = "https://codenexus.cloud/upgrade";

/** Deep-link to the billing page with the right plan preselected. */
export function upgradeUrl(sku: Sku): string {
  return `${UPGRADE_BASE}?sku=${encodeURIComponent(sku)}`;
}

// ── feature gating ──────────────────────────────────────────────────────────
// A feature is "CloudAgent" (cloud conductor / orchestration) or "WebAgent seats"
// (org + RBAC multi-user collaboration on a project).
export type Feature = Sku;

export type Entitlements = {
  mode: Mode;
  email?: string;
  scopes: string[];
  canEdit: boolean; // tier_m_edit granted
  canRun: boolean; // project_run granted
  canImport: boolean; // project_import granted
  /** CloudAgent runtime live AND entitled. Pre-launch this is false everywhere
   *  (the gate refuses conductor_* ops in web/webmcp), so it is truthful, not a
   *  hardcoded lie — it flips to a real read once the gate exposes the flag. */
  cloudAgentReady: boolean;
  planLabel: string;
};

export function readEntitlements(): Entitlements {
  const conn = loadGateConnection();
  const scopes = (conn?.scope || "").split(/\s+/).filter(Boolean);
  const mode = getMode();
  return {
    mode,
    email: conn?.email,
    scopes,
    canEdit: scopes.includes("tier_m_edit"),
    canRun: scopes.includes("project_run"),
    canImport: scopes.includes("project_import"),
    cloudAgentReady: false, // CloudAgent not shipped; gate rejects conductor ops in web
    planLabel: isWebBuild() ? "WebAgent" : "Local",
  };
}

/** Whether the operator can USE a feature right now. CloudAgent: only when the
 *  runtime is live + entitled (false pre-launch). WebAgent seats: the client can
 *  never prove the org's seat count, so we DON'T pre-judge it here — the gate is
 *  asked and, on an upgrade_required rejection, the UI routes to /upgrade. This
 *  keeps the seat moat server-enforced (no client-side guess = no fake lever). */
export function hasFeature(feature: Feature, e: Entitlements = readEntitlements()): boolean {
  if (feature === "cloudagent") return e.cloudAgentReady;
  // webmcp-seats is gated by the gate at action time, not pre-judged here.
  return true;
}

/** Config WRITES (settings, skills, sessions, shell policy, TOML, vocab) are not yet
 *  wired over the web gate — only reads, escalation approve/deny, and project import are.
 *  So the WebAgent web console is read-only for config: surfaces should DISABLE their write
 *  controls with a truthful label rather than let a click hit a gate refusal (§8). Flip
 *  this (or make it per-tool) once the gate exposes config writes over the web. */
export function configEditingAvailable(): boolean {
  return !isWebBuild();
}

// ── upgrade-intent bus ──────────────────────────────────────────────────────
// Lets ANY surface — a gated button, or a non-React transport rejection — ask
// the app to open the upgrade modal for a SKU, with no prop-drilling.
export type UpgradeIntent = { sku: Sku; reason?: string };

const UPGRADE_EVENT = "aidocs-upgrade-intent";

export function openUpgrade(sku: Sku, reason?: string): void {
  window.dispatchEvent(new CustomEvent<UpgradeIntent>(UPGRADE_EVENT, { detail: { sku, reason } }));
}

/** Subscribe to upgrade intents (returns an unsubscribe). */
export function onUpgradeIntent(cb: (intent: UpgradeIntent) => void): () => void {
  const handler = (e: Event) => cb((e as CustomEvent<UpgradeIntent>).detail);
  window.addEventListener(UPGRADE_EVENT, handler);
  return () => window.removeEventListener(UPGRADE_EVENT, handler);
}
