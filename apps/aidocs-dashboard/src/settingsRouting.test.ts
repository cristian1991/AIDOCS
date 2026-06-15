import { describe, it, expect } from "vitest";
import {
  isAdvancedOnly,
  isNormalEditable,
  saveRouteFor,
  partitionRows,
  advancedKeySet,
  assertNoForbiddenInNormal,
  isNormalRowAllowed,
  partitionDirtyForSave,
  profileConfirmToken,
  KNOWN_GUARDRAIL_KEYS,
  type SettingRowFlags,
  type SnapshotEntryFlags,
} from "./settingsRouting";

// Representative rows mirroring operator_surface inspect output.
const GB_FLAG: SettingRowFlags = {
  key: "tools.native_shell_provider_enabled",
  service_managed: "governed_bash",
  dashboard_only: true,
};
const DEPRECATED: SettingRowFlags = {
  key: "security.allow_raw_shell",
  deprecated: "use Governed Bash",
  dashboard_only: true,
};
const DASHBOARD_ONLY: SettingRowFlags = {
  key: "security.egress_allowlist",
  dashboard_only: true,
  security_sensitive: true,
};
const PLAIN: SettingRowFlags = { key: "tools.tool_call_timeout" };

describe("settings routing — guardrail keys are never normal rows", () => {
  it("classifies Governed Bash flags as advanced-only (not normal)", () => {
    expect(isAdvancedOnly(GB_FLAG)).toBe(true);
    expect(isNormalEditable(GB_FLAG)).toBe(false);
  });

  it("classifies deprecated aliases as advanced-only", () => {
    expect(isAdvancedOnly(DEPRECATED)).toBe(true);
  });

  it("classifies dashboard-only guardrail keys as advanced-only", () => {
    expect(isAdvancedOnly(DASHBOARD_ONLY)).toBe(true);
  });

  it("treats plain editable keys as normal", () => {
    expect(isAdvancedOnly(PLAIN)).toBe(false);
    expect(isNormalEditable(PLAIN)).toBe(true);
  });
});

describe("settings routing — save path", () => {
  it("routes service-managed keys to the owning profile, never a raw save", () => {
    expect(saveRouteFor(GB_FLAG)).toBe("profile");
  });
  it("blocks deprecated keys from any write", () => {
    expect(saveRouteFor(DEPRECATED)).toBe("blocked");
  });
  it("routes dashboard-only / security-sensitive to the expert path", () => {
    expect(saveRouteFor(DASHBOARD_ONLY)).toBe("expert");
  });
  it("routes plain keys to a normal save", () => {
    expect(saveRouteFor(PLAIN)).toBe("normal");
  });
});

describe("settings routing — partition + invariant", () => {
  it("partitions guardrail keys away from normal", () => {
    const { normal, advanced } = partitionRows([
      GB_FLAG,
      DEPRECATED,
      DASHBOARD_ONLY,
      PLAIN,
    ]);
    expect(normal.map((r) => r.key)).toEqual(["tools.tool_call_timeout"]);
    expect(advanced.map((r) => r.key)).toEqual([
      "tools.native_shell_provider_enabled",
      "security.allow_raw_shell",
      "security.egress_allowlist",
    ]);
  });

  it("advancedKeySet hides every guardrail key from the normal table", () => {
    const hidden = advancedKeySet([GB_FLAG, DEPRECATED, DASHBOARD_ONLY]);
    expect(hidden.has("tools.native_shell_provider_enabled")).toBe(true);
    expect(hidden.has("security.allow_raw_shell")).toBe(true);
    expect(hidden.has("security.egress_allowlist")).toBe(true);
    expect(hidden.has("tools.tool_call_timeout")).toBe(false);
  });

  it("assertNoForbiddenInNormal throws if a guardrail key leaks in", () => {
    expect(() => assertNoForbiddenInNormal([PLAIN])).not.toThrow();
    expect(() => assertNoForbiddenInNormal([PLAIN, GB_FLAG])).toThrow(
      /advanced-only key/,
    );
  });
});

// The tricky case: a hidden-owned key that is NOT dashboard-only and NOT
// service-managed/deprecated. Flag-based filtering alone would let it
// through; the static KNOWN_GUARDRAIL_KEYS list catches it even when the
// operator surface rows failed to load.
const HIDDEN_OWNED_KEY = "tools.shell_policy_shadow_enabled";

describe("hidden-owned non-dashboard key is sealed everywhere", () => {
  it("is in the static known-guardrail list", () => {
    expect(KNOWN_GUARDRAIL_KEYS.has(HIDDEN_OWNED_KEY)).toBe(true);
  });

  it("is classified advanced-only via the static list", () => {
    expect(isAdvancedOnly({ key: HIDDEN_OWNED_KEY })).toBe(true);
  });

  it("cannot be a normal row even when rows FAILED to load", () => {
    // rowsLoaded=false (failed/slow), advancedKeys empty (no backend data)
    const allowed = isNormalRowAllowed(
      { path: HIDDEN_OWNED_KEY },
      { rowsLoaded: false, advancedKeys: new Set() },
    );
    expect(allowed).toBe(false);
  });

  it("also stays hidden when rows loaded (backend marks it advanced)", () => {
    const allowed = isNormalRowAllowed(
      { path: HIDDEN_OWNED_KEY },
      { rowsLoaded: true, advancedKeys: new Set([HIDDEN_OWNED_KEY]) },
    );
    expect(allowed).toBe(false);
  });
});

describe("fail-closed normal-row rule", () => {
  it("hides security-sensitive keys until rows load", () => {
    const entry = { path: "some.sensitive.key", security_sensitive: true };
    expect(
      isNormalRowAllowed(entry, { rowsLoaded: false, advancedKeys: new Set() }),
    ).toBe(false);
    // once rows load, a plain security-sensitive (non-guardrail) key may show
    expect(
      isNormalRowAllowed(entry, { rowsLoaded: true, advancedKeys: new Set() }),
    ).toBe(true);
  });

  it("always hides dashboard_only / is_t0 keys regardless of load state", () => {
    for (const rowsLoaded of [true, false]) {
      expect(
        isNormalRowAllowed(
          { path: "x", dashboard_only: true },
          { rowsLoaded, advancedKeys: new Set() },
        ),
      ).toBe(false);
      expect(
        isNormalRowAllowed(
          { path: "y", is_t0: true },
          { rowsLoaded, advancedKeys: new Set() },
        ),
      ).toBe(false);
    }
  });

  it("allows a plain key once rows load", () => {
    expect(
      isNormalRowAllowed(
        { path: "tools.tool_call_timeout" },
        { rowsLoaded: true, advancedKeys: new Set() },
      ),
    ).toBe(true);
  });
});

describe("Save All routing — guardrail drafts are quarantined", () => {
  const PLAIN_E: SnapshotEntryFlags = { path: "tools.tool_call_timeout" };
  const DASH_E: SnapshotEntryFlags = {
    path: "security.egress_allowlist",
    dashboard_only: true,
    security_sensitive: true,
  };
  const SENS_E: SnapshotEntryFlags = {
    path: "some.sensitive.key",
    security_sensitive: true,
  };
  const GB_E: SnapshotEntryFlags = { path: "tools.shell_policy_shadow_enabled" };

  it("never lets a dashboard-only draft into Save All (rows loaded)", () => {
    const { savable, quarantined } = partitionDirtyForSave(
      [PLAIN_E, DASH_E],
      { rowsLoaded: true, advancedKeys: new Set() },
    );
    expect(savable.map((e) => e.path)).toEqual(["tools.tool_call_timeout"]);
    expect(quarantined.map((e) => e.path)).toEqual(["security.egress_allowlist"]);
  });

  it("quarantines a draft made BEFORE rows loaded (dashboard-only + sensitive)", () => {
    // rowsLoaded=false simulates a draft created before operator rows came
    // back; both the dashboard-only and the security-sensitive draft are
    // held out of the normal save.
    const { savable, quarantined } = partitionDirtyForSave(
      [PLAIN_E, DASH_E, SENS_E],
      { rowsLoaded: false, advancedKeys: new Set() },
    );
    // even the plain key is unsavable while !rowsLoaded? No — plain stays
    // savable; only guardrail + sensitive are held.
    expect(savable.map((e) => e.path)).toEqual(["tools.tool_call_timeout"]);
    expect(quarantined.map((e) => e.path).sort()).toEqual(
      ["security.egress_allowlist", "some.sensitive.key"].sort(),
    );
  });

  it("a security-sensitive draft becomes savable once rows load", () => {
    const before = partitionDirtyForSave([SENS_E], {
      rowsLoaded: false,
      advancedKeys: new Set(),
    });
    expect(before.quarantined.map((e) => e.path)).toEqual([
      "some.sensitive.key",
    ]);
    const after = partitionDirtyForSave([SENS_E], {
      rowsLoaded: true,
      advancedKeys: new Set(),
    });
    expect(after.savable.map((e) => e.path)).toEqual(["some.sensitive.key"]);
  });

  it("always quarantines a hidden-owned guardrail draft, both states", () => {
    for (const rowsLoaded of [true, false]) {
      const { savable, quarantined } = partitionDirtyForSave([GB_E], {
        rowsLoaded,
        advancedKeys: new Set(),
      });
      expect(savable).toEqual([]);
      expect(quarantined.map((e) => e.path)).toEqual([
        "tools.shell_policy_shadow_enabled",
      ]);
    }
  });
});

describe("dangerous profile confirm token", () => {
  it("mirrors the backend phrase", () => {
    expect(profileConfirmToken("breakglass_flavor")).toBe(
      "confirm-apply breakglass_flavor",
    );
    expect(profileConfirmToken("authority_border")).toBe(
      "confirm-apply authority_border",
    );
  });
});
