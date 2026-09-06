#!/usr/bin/env node
/**
 * Sync the WEB dashboard bundle into the gate's serving dir.
 *
 * dist-web/ (vite.config.web.ts output, stamped with build-info.json)
 *   -> mcp/server/aidocs_mcp/templates/webapp/
 *
 * The gate serves the web dashboard from templates/webapp as committed
 * build artifacts. Before this script existed the copy was a manual,
 * undocumented step — the exact mechanism by which the web dashboard
 * drifted from the desktop app (dashboard-war (b)). Run via:
 *
 *   npm run sync:web        (build:web + this copy)
 *
 * The copy is replace-in-full: stale hashed chunks from previous builds
 * are removed so the served bundle is exactly one build.
 */

import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const distWeb = join(here, "..", "dist-web");
const webappDir = join(
  here, "..", "..", "..", "mcp", "server", "aidocs_mcp", "templates", "webapp",
);

if (!existsSync(join(distWeb, "index.html"))) {
  console.error(`sync-web-bundle: no build at ${distWeb} — run \`npm run build:web\` first`);
  process.exit(1);
}
if (!existsSync(join(distWeb, "build-info.json"))) {
  console.error("sync-web-bundle: dist-web has no build-info.json — stale vite.config.web.ts?");
  process.exit(1);
}

rmSync(webappDir, { recursive: true, force: true });
mkdirSync(webappDir, { recursive: true });
cpSync(distWeb, webappDir, { recursive: true });

const info = JSON.parse(readFileSync(join(webappDir, "build-info.json"), "utf-8"));
console.log(`sync-web-bundle: templates/webapp now serves ${info.tag} @${info.sha} (built ${info.built_at})`);
