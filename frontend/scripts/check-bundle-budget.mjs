/**
 * Bundle budget gate.
 *
 * Reads the Vite build output under dist/assets/ and enforces:
 *   - Total JS size < MAX_TOTAL_JS bytes (raw, uncompressed)
 *   - No single JS chunk > MAX_CHUNK_JS bytes
 *
 * Exit 0 on pass, exit 1 on budget exceeded.
 */

import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST_DIR = join(import.meta.dirname, '..', 'dist', 'assets');
const MAX_TOTAL_JS = 16 * 1024 * 1024; // 16 MiB total JS
const MAX_CHUNK_JS = 3.5 * 1024 * 1024; // 3.5 MiB per chunk (mermaid is ~3.3 MiB)

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

console.log(`Bundle budget check:`);
console.log(`  Total JS: ${formatSize(totalJs)} / ${formatSize(MAX_TOTAL_JS)}`);
console.log(`  Chunks:   ${jsFiles.length}`);
console.log(`  Largest:  ${formatSize(Math.max(...jsFiles.map((f) => statSync(join(DIST_DIR, f)).size)))}`);

if (totalJs > MAX_TOTAL_JS) {
  violations.push(`  OVER: Total JS ${formatSize(totalJs)} exceeds budget ${formatSize(MAX_TOTAL_JS)}`);
}

if (violations.length > 0) {
  console.error(`\nBudget EXCEEDED:\n${violations.join('\n')}`);
  process.exit(1);
} else {
  console.log(`\nAll within budget.`);
}
