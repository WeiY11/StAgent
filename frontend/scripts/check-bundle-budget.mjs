/**
 * Bundle budget gate.
 *
 * Reads the Vite build output under dist/assets/ and enforces:
 *   - Total JS size < MAX_TOTAL_JS bytes (raw, uncompressed)
 *   - No single JS chunk > MAX_CHUNK_JS bytes
 *
 * Exit 0 on pass, exit 1 on budget exceeded.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST_DIR = join(import.meta.dirname, '..', 'dist', 'assets');
const MAX_TOTAL_JS = 16 * 1024 * 1024; // 16 MiB total JS
const MAX_CHUNK_JS = 3.5 * 1024 * 1024; // 3.5 MiB per chunk (mermaid is ~3.3 MiB)
const CHAT_DEFERRED_IMPORTS = [
  /^GenUiInline-/,
  /^GenUiRegistry-/,
  /^genUiCaptureDom-/,
  /^recharts-/,
];

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

let files;
try {
  files = readdirSync(DIST_DIR);
} catch {
  console.error(`ERROR: dist/assets/ not found. Run 'npm run build' first.`);
  process.exit(1);
}

const jsFiles = files.filter((f) => f.endsWith('.js'));
let totalJs = 0;
const violations = [];

for (const file of jsFiles) {
  const size = statSync(join(DIST_DIR, file)).size;
  totalJs += size;
  if (size > MAX_CHUNK_JS) {
    violations.push(`  OVER: ${file} (${formatSize(size)} > ${formatSize(MAX_CHUNK_JS)})`);
  }
}

let chatGenUiDeferred = false;
const chatChunks = jsFiles.filter((file) => file.startsWith('ChatView-'));
if (chatChunks.length !== 1) {
  violations.push(`  CHAT: expected one ChatView chunk, found ${chatChunks.length}`);
} else {
  const chatSource = readFileSync(join(DIST_DIR, chatChunks[0]), 'utf8');
  const staticImports = Array.from(
    chatSource.matchAll(/\bimport[^;]+\bfrom["']\.\/([^"']+)["']/g),
    (match) => match[1],
  );
  const eagerGenUiImports = staticImports.filter((file) =>
    CHAT_DEFERRED_IMPORTS.some((pattern) => pattern.test(file)),
  );
  if (eagerGenUiImports.length > 0) {
    violations.push(
      `  CHAT: deferred GenUI dependencies became static: ${eagerGenUiImports.join(', ')}`,
    );
  } else {
    chatGenUiDeferred = true;
  }
}

console.log(`Bundle budget check:`);
console.log(`  Total JS: ${formatSize(totalJs)} / ${formatSize(MAX_TOTAL_JS)}`);
console.log(`  Chunks:   ${jsFiles.length}`);
console.log(`  Largest:  ${formatSize(Math.max(...jsFiles.map((f) => statSync(join(DIST_DIR, f)).size)))}`);
console.log(`  Chat GenUI: ${chatGenUiDeferred ? 'deferred' : 'INVALID'}`);

if (totalJs > MAX_TOTAL_JS) {
  violations.push(`  OVER: Total JS ${formatSize(totalJs)} exceeds budget ${formatSize(MAX_TOTAL_JS)}`);
}

if (violations.length > 0) {
  console.error(`\nBudget EXCEEDED:\n${violations.join('\n')}`);
  process.exit(1);
} else {
  console.log(`\nAll within budget.`);
}
