#!/usr/bin/env node
'use strict';

// Parse every inline classic JavaScript block in tracked HTML pages. This catches
// syntax errors (including literal "\\n" accidentally inserted between lines)
// before a broken script can disable unrelated UI handlers such as admin login.
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const ignored = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__']);
const htmlFiles = [];

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!ignored.has(entry.name)) walk(path.join(dir, entry.name));
    } else if (entry.isFile() && entry.name.toLowerCase().endsWith('.html')) {
      htmlFiles.push(path.join(dir, entry.name));
    }
  }
}

walk(root);
let checked = 0;
const errors = [];

for (const file of htmlFiles) {
  const html = fs.readFileSync(file, 'utf8');
  const scriptTag = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
  let match;
  let block = 0;

  while ((match = scriptTag.exec(html)) !== null) {
    const attributes = match[1];
    const code = match[2];
    if (!code.trim() || /\btype\s*=\s*["']?module\b/i.test(attributes)) continue;
    if (/\bsrc\s*=/i.test(attributes)) continue;

    checked += 1;
    block += 1;
    try {
      // Compile only; browser APIs are not executed in this check.
      new Function(code);
    } catch (error) {
      errors.push(`${path.relative(root, file)} inline script #${block}: ${error.message}`);
    }
  }
}

if (errors.length) {
  console.error('Inline JavaScript syntax check failed:');
  for (const error of errors) console.error(` - ${error}`);
  process.exit(1);
}

console.log(`Inline JavaScript syntax check passed (${checked} script block(s), ${htmlFiles.length} HTML file(s)).`);
