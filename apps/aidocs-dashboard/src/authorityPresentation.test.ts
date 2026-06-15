import { describe, it, expect } from "vitest";
import {
  blastRadiusBadge,
  broadeningWarning,
  refusalBanner,
  ownershipNote,
  t0ConfirmText,
  actionAuthorityLabel,
  eventAuthority,
  statusSeverity,
  actionResultNotice,
} from "./authorityPresentation";

// Fixtures mirror the backend action-result payloads verbatim (the same
// fields cli.py / mcp_server.py / operator_surface.py emit).

describe("blast-radius / broadening (global write warning)", () => {
  it("labels a global write install-wide and broadening", () => {
    const r = { ok: true, scope: "global", blast_radius: "global", broadening: true,
      warning: "GLOBAL (install-wide) write: applies to EVERY project on this install." };
    const badge = blastRadiusBadge(r)!;
    expect(badge.label).toBe("Install-wide");
    expect(badge.severity).toBe("warning");
    expect(badge.tooltip.toLowerCase()).toContain("every project");
    const warn = broadeningWarning(r)!;
    expect(warn.toLowerCase()).toContain("every project");
  });

  it("project/session writes are not broadening", () => {
    expect(blastRadiusBadge({ blast_radius: "project" })!.label).toBe("Project");
    expect(broadeningWarning({ blast_radius: "project", broadening: false })).toBeNull();
    expect(blastRadiusBadge({ blast_radius: "session" })!.label).toBe("Session");
  });
});

describe("T0 confirmation text", () => {
  it("requires explicit confirmation + reason for a dashboard-only key", () => {
    const c = t0ConfirmText("dev.kill_switch");
    expect(c.title).toContain("dev.kill_switch");
    expect(c.body.toLowerCase()).toContain("dashboard-only");
    expect(c.requiresReason).toBe(true);
  });
});

describe("refusal banners (blocked_by → operator-facing, backend-aligned)", () => {
  it("unauthenticated → sign-in banner", () => {
    const b = refusalBanner({ ok: false, blocked_by: "operator_auth" })!;
    expect(b.severity).toBe("danger");
    expect(b.title).toBe("Sign in required");
    expect(b.hint).toContain("ADMIN/SUPERADMIN");
    expect(b.code).toBe("operator_auth");
  });

  it("under-permissioned → missing-permission banner", () => {
    const b = refusalBanner({ ok: false, blocked_by: "missing_admin_manage_config" })!;
    expect(b.title).toBe("Insufficient permission");
    expect(b.message).toContain("admin.manage_config");
  });

  it("cross-project unapproved relation banner", () => {
    const b = refusalBanner({ ok: false, blocked_by: "relation_not_approved" })!;
    expect(b.title).toContain("Cross-project");
    expect(b.message).toContain("approved_external_roots");
  });

  it("session not in project banner names SQL membership authority", () => {
    const b = refusalBanner({ ok: false, blocked_by: "session_not_in_project" })!;
    expect(b.message.toLowerCase()).toContain("session_membership");
  });

  it("dashboard-only (T0) setting refusal", () => {
    const b = refusalBanner({ ok: false, blocked_by: "dashboard_only_setting" })!;
    expect(b.title).toContain("T0");
  });

  it("ok result with no blocked_by → no banner", () => {
    expect(refusalBanner({ ok: true })).toBeNull();
  });

  it("unknown blocked_by → generic refusal carrying the code + reason", () => {
    const b = refusalBanner({ ok: false, blocked_by: "weird_code", reason: "because" })!;
    expect(b.code).toBe("weird_code");
    expect(b.message).toBe("because");
  });
});

describe("ownership truth (owner_grant / ownership_degraded)", () => {
  it("degraded ownership surfaces a warning", () => {
    const n = ownershipNote({ owner_grant: "failed", ownership_degraded: true,
      warning: "ownership is degraded; grant the session_owner role" })!;
    expect(n.status).toBe("failed");
    expect(n.severity).toBe("warning");
    expect(n.message.toLowerCase()).toContain("session_owner");
  });

  it("granted / not_required are informational", () => {
    expect(ownershipNote({ owner_grant: "granted" })!.severity).toBe("info");
    expect(ownershipNote({ owner_grant: "not_required" })!.status).toBe("not_required");
    expect(ownershipNote({})).toBeNull();
  });
});

describe("audit / event row authority (ExecutionPage)", () => {
  it("maps a refused control-plane mutation to actor/scope/target/status/reason", () => {
    const row = eventAuthority({
      event_kind: "control_plane_mutation",
      status: "refused",
      capability_name: "dashboard-gate-msg-set",
      target_entity: "dashboard-gate-msg-set",
      payload: { reason: "missing_admin_manage_config", source: "dashboard_admin" },
    });
    expect(row.target).toBe("dashboard-gate-msg-set");
    expect(row.status).toBe("refused");
    expect(row.statusSeverity).toBe("danger");
    expect(row.reason).toBe("missing_admin_manage_config");
    expect(row.actor).toBe("system"); // no user on a refused-unauth row
  });

  it("maps an applied global config write with actor + blast-radius badge", () => {
    const row = eventAuthority({
      event_kind: "config_set",
      status: "applied",
      target_entity: "dev.kill_switch",
      user_id: "op",
      effective_role: "super_admin",
      scope_id: "/proj",
      payload: { blast_radius: "global", broadening: true, role: "super_admin" },
    });
    expect(row.actor).toBe("op (super_admin)");
    expect(row.scope).toBe("/proj");
    expect(row.status).toBe("applied");
    expect(row.statusSeverity).toBe("info");
    expect(row.blastRadius!.label).toBe("Install-wide");
  });

  it("status severity matches the backend status vocabulary", () => {
    expect(statusSeverity("applied")).toBe("info");
    expect(statusSeverity("allowed_degraded")).toBe("warning");
    expect(statusSeverity("refused")).toBe("danger");
    expect(statusSeverity("rolled_back")).toBe("danger");
  });
});

describe("action result notice (session delete/connect results)", () => {
  it("a refused delete (operator_auth) is a danger notice, NOT success", () => {
    const n = actionResultNotice(
      { ok: false, blocked_by: "operator_auth" }, "Deleted session: s1");
    expect(n.ok).toBe(false);
    expect(n.severity).toBe("danger");
    expect(n.text).toContain("Sign in required");
    expect(n.text).not.toContain("Deleted");
  });

  it("a cross-project refusal surfaces the relation banner", () => {
    const n = actionResultNotice(
      { ok: false, blocked_by: "relation_not_approved" }, "Connected");
    expect(n.ok).toBe(false);
    expect(n.text).toContain("approved_external_roots");
  });

  it("an ok result reads as success", () => {
    const n = actionResultNotice({ ok: true }, "Deleted session: s1");
    expect(n.ok).toBe(true);
    expect(n.text).toBe("Deleted session: s1");
  });

  it("a granted-ownership create is a plain success (no degraded warning)", () => {
    const n = actionResultNotice(
      { ok: true, session_id: "s3", owner_grant: "granted" },
      "Created session: s3");
    expect(n.ok).toBe(true);
    expect(n.severity).toBe("info");
    expect(n.text).toBe("Created session: s3");
  });

  it("a degraded-ownership success appends the ownership warning", () => {
    const n = actionResultNotice(
      { ok: true, owner_grant: "failed", ownership_degraded: true,
        warning: "ownership is degraded; grant the session_owner role" },
      "Created session: s2");
    expect(n.ok).toBe(true);
    expect(n.severity).toBe("warning");
    expect(n.text).toContain("Created session: s2");
    expect(n.text.toLowerCase()).toContain("session_owner");
  });
});

describe("read-only vs mutating action labels", () => {
  it("read-only endpoints are labeled read-only (no admin wall implied)", () => {
    for (const a of ["auth_status", "binding_list", "mcp_list", "toml_editability"]) {
      const la = actionAuthorityLabel(a);
      expect(la.kind).toBe("read-only");
      expect(la.t0).toBe(false);
    }
  });

  it("mutating actions are admin; T0 ones require confirm", () => {
    const m = actionAuthorityLabel("delete_session");
    expect(m.kind).toBe("mutating");
    expect(m.label).toBe("Admin");
    const t0 = actionAuthorityLabel("set_config", { dashboardOnly: true });
    expect(t0.t0).toBe(true);
    expect(t0.label).toContain("confirm");
    expect(t0.severity).toBe("danger");
  });
});
