import { execSync } from "node:child_process";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build-time version = the latest git release tag (e.g. "v2.3.0b5") so the shell footer
// matches the published release, not the independent/stale package.json. Falls back to
// package.json (npm_package_version) then "dev" when git/tags are unavailable.
function appVersion(): string {
  try {
    return execSync("git describe --tags --abbrev=0", {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    const pv = process.env.npm_package_version;
    return pv ? `v${pv}` : "dev";
  }
}

// Build-time commit stamp (dashboard-war (b)): the exact commit the bundle was
// built from. The release tag above answers "which release"; this answers
// "which code" — the drift guard compares it against HEAD.
function buildSha(): string {
  try {
    return execSync("git rev-parse --short=10 HEAD", {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "unknown";
  }
}

export default defineConfig({
  plugins: [react()],
  // Desktop/Tauri build: __AIDOCS_WEB__ is false (the web build sets it true).
  // Defined in BOTH builds so the token is always replaced (no runtime ReferenceError).
  define: {
    __AIDOCS_WEB__: JSON.stringify(false),
    __APP_VERSION__: JSON.stringify(appVersion()),
    __BUILD_SHA__: JSON.stringify(buildSha()),
  },
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
});
