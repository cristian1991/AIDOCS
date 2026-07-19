import { execSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

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

// Build-time commit stamp (dashboard-war (b)) — same contract as vite.config.ts.
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

// Emit build-info.json into the bundle so the DEPLOYED artifact carries a
// machine-readable stamp — the drift guard (webapp_bundle_drift.py) reads it.
function emitBuildInfo() {
  return {
    name: "aidocs-emit-build-info",
    closeBundle() {
      const info = {
        sha: buildSha(),
        tag: appVersion(),
        built_at: new Date().toISOString(),
      };
      writeFileSync(
        path.resolve(__dirname, "dist-web", "build-info.json"),
        JSON.stringify(info, null, 2) + "\n",
      );
    },
  };
}

// WEB build of the dashboard — served from the AIDOCS gate (mcp.codenexus.cloud).
// __AIDOCS_WEB__ = true so the mode model hides "local"; Tauri-only modules are aliased to
// browser shims (no Tauri runtime in a browser). Output bundle is served by the gate.
export default defineConfig({
  plugins: [react(), emitBuildInfo()],
  define: {
    __AIDOCS_WEB__: JSON.stringify(true),
    __APP_VERSION__: JSON.stringify(appVersion()),
    __BUILD_SHA__: JSON.stringify(buildSha()),
  },
  base: "/",
  resolve: {
    alias: {
      "@tauri-apps/api/core": path.resolve(__dirname, "src/platform/invoke.ts"),
      "@tauri-apps/plugin-http": path.resolve(__dirname, "src/platform/http.ts"),
    },
  },
  build: {
    outDir: "dist-web",
    emptyOutDir: true,
    target: "es2020",
    // Two HTML entries: index.html = the dashboard SPA (gate serves it ONLY to an
    // authenticated visitor), login.html = the standalone sign-in page (served to anon
    // visitors so the dashboard bundle is never shipped pre-auth). The gate picks which
    // to serve for "/" based on the session cookie.
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        login: path.resolve(__dirname, "login.html"),
      },
    },
  },
});
