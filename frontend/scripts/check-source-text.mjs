#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..', 'src');
const extensions = new Set(['.css', '.json', '.ts', '.tsx']);
const failures = [];

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(file);
    else if (extensions.has(path.extname(entry.name).toLowerCase())) {
      const text = fs.readFileSync(file, 'utf8');
      const reasons = [];
      if (text.includes('\uFFFD')) reasons.push('U+FFFD replacement character');
      if (text.charCodeAt(0) === 0xFEFF) reasons.push('UTF-8 BOM');
      if (path.extname(file).toLowerCase() === '.json') {
        try {
          JSON.parse(text);
        } catch (error) {
          reasons.push(`invalid JSON (${error instanceof Error ? error.message : String(error)})`);
        }
      }
      if (reasons.length) failures.push(`${path.relative(root, file)}: ${reasons.join(', ')}`);
    }
  }
}

walk(root);
if (failures.length) {
  console.error(`Source text integrity failed (${failures.length} file(s)):`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log('Source text integrity: clean');
}
