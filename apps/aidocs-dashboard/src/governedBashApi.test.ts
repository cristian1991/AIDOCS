import { describe, it, expect, vi, beforeEach } from "vitest";

// Capture every Tauri invoke so we can assert the UI ↔ backend contract:
// the dashboard must drive the ONE action + signed-card approval, never the
// removed path/hash/signature wizard.
const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (cmd: string, args: Record<string, unknown>) => {
    calls.push({ cmd, args });
    return Promise.resolve({ ok: true });
  },
}));

import { governedBashStatus, governedBashEnable, governedBashDisable } from "./dashboardApi";

beforeEach(() => {
  calls.length = 0;
});

describe("governed-bash UI/backend parity", () => {
  it("the one action sends no path/hash/signature wizard args", async () => {
    await governedBashEnable({ projectRoot: "/p" });
    expect(calls).toHaveLength(1);
    const { cmd, args } = calls[0];
    expect(cmd).toBe("governed_bash_enable");
    // The wizard fields are gone — only project/scope/(optional card).
    expect("providerPath" in args).toBe(false);
    expect("hashPin" in args).toBe(false);
    expect("requireOsSignature" in args).toBe(false);
    expect(args.approvalCardJson).toBeUndefined();
    expect(args.scope).toBe("global");
  });

  it("approving a candidate echoes the signed card verbatim", async () => {
    const card = {
      provider_path: "C:/Users/me/scoop/bash.exe",
      sha256: "ab".repeat(32),
      nonce: "n0nce",
      issued_at: 1000,
      expiry: 1300,
      token: "tok",
    };
    await governedBashEnable({ projectRoot: "/p", approvalCardJson: JSON.stringify(card) });
    const { cmd, args } = calls[0];
    expect(cmd).toBe("governed_bash_enable");
    expect(JSON.parse(String(args.approvalCardJson))).toEqual(card);
    expect("providerPath" in args).toBe(false);
  });

  it("status is read-only (no mutation args)", async () => {
    await governedBashStatus("/p");
    expect(calls[0].cmd).toBe("governed_bash_status");
    expect(calls[0].args).toEqual({ projectRoot: "/p" });
  });

  it("disable carries only project + scope", async () => {
    await governedBashDisable("/p");
    expect(calls[0].cmd).toBe("governed_bash_disable");
    expect(calls[0].args).toEqual({ projectRoot: "/p", scope: "global" });
  });
});
