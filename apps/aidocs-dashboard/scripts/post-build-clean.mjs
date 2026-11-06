#!/usr/bin/env node
/**
 * Post-build artifact cleanup for the Tauri dashboard.
 *
 * Cargo never prunes its own `target/` dir, so repeated `tauri build` runs
 * accumulate stale artifacts indefinitely (debug + release profiles, per-build
 * incremental caches, and superseded dependency object files). On this repo
 * that grew `src-tauri/target` to ~5 GB.
 *
 * This script runs AFTER a successful build (wired via the `tauri:build` npm
 * script with `&&`, so it only fires when the build itself succeeded) and
 * reclaims space WITHOUT touching the release output you just produced:
 *
 *   - removes the `debug/` profile entirely (dev artifacts; a release build
 *     does not need them, and the next `npm run dev` rebuilds as needed),
 *   - removes `release/incremental/` (incremental-compilation cache; only
 *     speeds up RE-builds, safe to drop after a finished build),
 *   - preserves `release/bundle/` and the release binary (the actual output).
 *
 * It is intentionally conservative: it only deletes well-known regenerable
 * subtrees, never the bundle, and is a no-op if nothing is present.
 */

import { existsSync, rmSync, statSync, readdirSync } from "node:fs"
import { join, dirname } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const targetDir = join(here, "..", "src-tauri", "target")

function dirBytes(p) {
  if (!existsSync(p)) return 0
  let total = 0
  for (const name of readdirSync(p)) {
    const child = join(p, name)
    let st
    try {
      st = statSync(child)
    } catch {
      continue
    }
    total += st.isDirectory() ? dirBytes(child) : st.size
  }
  return total
}

function reclaim(p, label) {
  if (!existsSync(p)) return 0
  const before = dirBytes(p)
  try {
    rmSync(p, { recursive: true, force: true })
    console.log(`  removed ${label}: ${(before / 1_048_576).toFixed(1)} MB`)
    return before
  } catch (err) {
    console.warn(`  could not remove ${label}: ${err.message}`)
    return 0
  }
}

if (!existsSync(targetDir)) {
  console.log("post-build-clean: no target/ dir — nothing to do")
  process.exit(0)
}

console.log("post-build-clean: pruning stale Cargo artifacts (release output preserved)")
let freed = 0
freed += reclaim(join(targetDir, "debug"), "debug profile")
freed += reclaim(join(targetDir, "release", "incremental"), "release/incremental cache")
console.log(`post-build-clean: reclaimed ${(freed / 1_048_576).toFixed(1)} MB`)
