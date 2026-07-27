#!/usr/bin/env node
// Re-applies the Windows EPERM rename-retry patch to app-builder-lib after `npm ci`.
//
// electron-builder's atomic directory rename (electronGet.js) can fail with
// EPERM/EACCES/EBUSY on Windows when antivirus real-time scanning transiently
// locks freshly-extracted executables. `npm ci` restores the pristine package,
// wiping any manual edit, so this script re-injects a bounded retry loop.
//
// Idempotent (marker-guarded) and non-fatal: if the target snippet is absent
// (upstream changed), it warns and exits 0 — the Defender exclusions added by
// build-win.ps1 remain the primary defense.

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const target = join(
  scriptDir,
  "..",
  "electron",
  "node_modules",
  "app-builder-lib",
  "out",
  "util",
  "electronGet.js",
);

const MARKER = "[leagent-patch] rename-retry";

if (!existsSync(target)) {
  console.warn(`[patch-electron-builder] target not found, skipping: ${target}`);
  process.exit(0);
}

let src = readFileSync(target, "utf8");

if (src.includes(MARKER)) {
  console.log("[patch-electron-builder] already patched — nothing to do.");
  process.exit(0);
}

// Match the pristine `rm(dir) + rename(tmpDir, dir)` pair with tolerant whitespace.
const pattern =
  /await fs\.rm\(dir, \{ recursive: true, force: true \}\);\s*\n(\s*)await fs\.rename\(tmpDir, dir\);/;

const match = src.match(pattern);
if (!match) {
  console.warn(
    "[patch-electron-builder] rename snippet not found (upstream may have changed); " +
      "relying on Defender exclusions instead.",
  );
  process.exit(0);
}

const indent = match[1];
const replacement =
  `await fs.rm(dir, { recursive: true, force: true });\n` +
  `${indent}// ${MARKER}: Windows AV can transiently lock freshly-extracted files,\n` +
  `${indent}// making the atomic rename fail with EPERM/EACCES/EBUSY. Retry with backoff.\n` +
  `${indent}for (let attempt = 1; ; attempt++) {\n` +
  `${indent}    try {\n` +
  `${indent}        await fs.rename(tmpDir, dir);\n` +
  `${indent}        break;\n` +
  `${indent}    }\n` +
  `${indent}    catch (renameErr) {\n` +
  `${indent}        const code = renameErr && renameErr.code;\n` +
  `${indent}        if (attempt >= 10 || (code !== "EPERM" && code !== "EACCES" && code !== "EBUSY")) {\n` +
  `${indent}            throw renameErr;\n` +
  `${indent}        }\n` +
  `${indent}        builder_util_1.log.warn({ tmpDir, dir, code, attempt }, "rename blocked (likely antivirus scan); retrying");\n` +
  `${indent}        await new Promise(r => setTimeout(r, 500 * attempt));\n` +
  `${indent}    }\n` +
  `${indent}}`;

src = src.replace(pattern, replacement);
writeFileSync(target, src, "utf8");
console.log(`[patch-electron-builder] applied rename-retry patch to ${target}`);
