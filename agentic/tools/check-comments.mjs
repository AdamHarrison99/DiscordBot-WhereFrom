#!/usr/bin/env node
/*
 * Enforces the comment rule in agentic/CLAUDE.md.
 *
 * Usage:
 *   node agentic/tools/check-comments.mjs                         lines this branch changed
 *   COMMENT_LINT_BASE=HEAD node agentic/tools/check-comments.mjs   uncommitted work only
 *   node agentic/tools/check-comments.mjs <path>                   whole-file scan
 *
 * Exit 0 clean, 1 violations found, 2 usage error.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, statSync, readdirSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';

const ROOT = resolve(new URL('../..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));

const BANNED = [
  'because', 'otherwise', 'rather than', 'so that', 'instead of',
  'which is why', 'the reason', 'used to', 'previously', 'would have',
];

const MAX_RUN = 2;
const SOURCE_EXT = new Set(['.cs', '.mjs', '.js', '.ts', '.ps1', '.py']);

const EXCLUDED_DIRS = new Set(['bin', 'obj', '.git', 'node_modules', '.vs']);

function isSource(path) {
  const dot = path.lastIndexOf('.');
  return dot !== -1 && SOURCE_EXT.has(path.slice(dot));
}

function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!EXCLUDED_DIRS.has(entry.name)) walk(join(dir, entry.name), out);
    } else if (isSource(entry.name)) {
      out.push(join(dir, entry.name));
    }
  }
  return out;
}

function changedLines() {
  const base = process.env.COMMENT_LINT_BASE ?? 'origin/HEAD';
  let diff;
  try {
    diff = execFileSync('git', ['diff', '--unified=0', base, '--', '.'], {
      cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    });
  } catch {
    return null;
  }

  const map = new Map();
  let file = null;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++ b/')) {
      file = line.slice(6).trim();
      if (!map.has(file)) map.set(file, new Set());
    } else if (line.startsWith('@@') && file) {
      const m = /\+(\d+)(?:,(\d+))?/.exec(line);
      if (m) {
        const start = Number(m[1]);
        const count = m[2] === undefined ? 1 : Number(m[2]);
        for (let i = 0; i < count; i++) map.get(file).add(start + i);
      }
    }
  }
  return map;
}

// Comment lines only. Not a full parser; a "//" inside a string literal is a false positive.
function commentLines(text) {
  const out = [];
  const lines = text.split('\n');
  let inBlock = false;

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const trimmed = raw.trim();

    if (inBlock) {
      out.push({ n: i + 1, text: trimmed.replace(/^\*+/, '').trim(), kind: 'block' });
      if (trimmed.includes('*/')) inBlock = false;
      continue;
    }
    if (trimmed.startsWith('/*')) {
      out.push({ n: i + 1, text: trimmed.slice(2).replace(/\*\/$/, '').trim(), kind: 'block' });
      if (!trimmed.includes('*/')) inBlock = true;
      continue;
    }
    if (trimmed.startsWith('///')) {
      out.push({ n: i + 1, text: trimmed.slice(3).trim(), kind: 'doc' });
      continue;
    }
    if (trimmed.startsWith('//') || trimmed.startsWith('#')) {
      const body = trimmed.startsWith('//') ? trimmed.slice(2) : trimmed.slice(1);
      out.push({ n: i + 1, text: body.trim(), kind: 'line' });
    }
  }
  return out;
}

function check(file, only) {
  const rel = relative(ROOT, file).split(sep).join('/');
  const text = readFileSync(file, 'utf8');
  const comments = commentLines(text);
  const violations = [];

  const inScope = (n) => !only || only.has(n);

  // Trap lines are exempt. The run rule targets "//" lines; a block comment is one construct.
  const exempt = (c) => c.text.startsWith('!') || c.kind !== 'line';

  let run = [];
  const flushRun = () => {
    if (run.length > MAX_RUN && inScope(run[0].n)) {
      violations.push({
        line: run[0].n,
        rule: 'comment-run',
        message: `${run.length} consecutive comment lines (max ${MAX_RUN}) - this is rationale, move it to agentic/CLAUDE.md`,
      });
    }
    run = [];
  };

  let prev = -10;
  for (const c of comments) {
    if (exempt(c)) { flushRun(); prev = c.n; continue; }

    if (c.n === prev + 1) run.push(c);
    else { flushRun(); run = [c]; }
    prev = c.n;

    if (!inScope(c.n)) continue;

    const lower = c.text.toLowerCase();
    for (const word of BANNED) {
      // Word-boundary match; multi-word entries tolerate a line break.
      const re = new RegExp(`\\b${word.replace(/ /g, '\\s+')}\\b`, 'i');
      if (re.test(lower)) {
        violations.push({
          line: c.n,
          rule: 'rationale-word',
          message: `"${word}" - rationale belongs in agentic/CLAUDE.md`,
        });
      }
    }
  }
  flushRun();

  return violations.length ? { file: rel, violations } : null;
}

function main() {
  const arg = process.argv[2];
  let files;
  let onlyByFile = null;

  if (arg) {
    const target = resolve(ROOT, arg);
    let st;
    try { st = statSync(target); } catch {
      console.error(`check-comments: no such path: ${arg}`);
      process.exit(2);
    }
    files = st.isDirectory() ? walk(target) : [target];
  } else {
    const map = changedLines();
    if (map === null) {
      console.error('check-comments: no git base available; pass a path for a whole-file scan');
      process.exit(2);
    }
    onlyByFile = map;
    files = [...map.keys()]
      .filter(isSource)
      .map((f) => resolve(ROOT, f))
      .filter((f) => { try { return statSync(f).isFile(); } catch { return false; } });
  }

  const results = [];
  for (const file of files) {
    const only = onlyByFile
      ? onlyByFile.get(relative(ROOT, file).split(sep).join('/'))
      : null;
    const r = check(file, only);
    if (r) results.push(r);
  }

  if (!results.length) {
    console.log(`check-comments: clean (${files.length} file${files.length === 1 ? '' : 's'})`);
    process.exit(0);
  }

  let total = 0;
  for (const { file, violations } of results) {
    for (const v of violations.sort((a, b) => a.line - b.line)) {
      console.log(`${file}:${v.line}  [${v.rule}] ${v.message}`);
      total++;
    }
  }
  console.log(`\ncheck-comments: ${total} violation${total === 1 ? '' : 's'} in ${results.length} file${results.length === 1 ? '' : 's'}`);
  process.exit(1);
}

main();
