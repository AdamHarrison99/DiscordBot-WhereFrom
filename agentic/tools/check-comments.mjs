#!/usr/bin/env node
/*
 * Enforces the comment rule in agentic/CLAUDE.md.
 *
 * Usage:
 *   node agentic/tools/check-comments.mjs                         lines this branch changed
 *   COMMENT_LINT_BASE=HEAD node agentic/tools/check-comments.mjs   uncommitted work only
 *   node agentic/tools/check-comments.mjs <path>                   whole-file scan
 *   node agentic/tools/check-comments.mjs .                        the whole repo
 *
 * Bare, it only sees the lines this branch changed - it reports clean while the
 * rest of the tree is dirty. An audit passes a path.
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

// A comment is context, not an explanation: one line, two at the outside.
const MAX_RUN = 2;
const MAX_COMMENT_CHARS = 120;

// A docstring says what something is. What it is for goes in agentic/CLAUDE.md.
const MAX_DOC_LINES = 2;
const MAX_DOC_CHARS = 160;

// A module docstring orients a reader arriving at the file, so it gets a little more.
const MAX_MODULE_DOC_LINES = 3;
const MAX_MODULE_DOC_CHARS = 200;

const SOURCE_EXT = new Set(['.cs', '.mjs', '.js', '.ts', '.ps1', '.py']);

const EXCLUDED_DIRS = new Set(['bin', 'obj', '.git', 'node_modules', '.vs',
                               '.venv', 'venv', '__pycache__']);

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

const DEF_RE = /^\s*(async\s+)?(def|class)\b/;
const QUOTE_RE = /^(?:[rbuf]{0,2})("""|''')/i;

// Python docstrings. A def's signature can span lines, so the colon ends it.
function pythonDocstrings(text) {
  const lines = text.split('\n');
  const out = [];
  let expect = 'module';

  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    if (!trimmed) continue;

    if (expect === 'module') {
      // Anything real before the string means the module has no docstring.
      if (trimmed.startsWith('#')) continue;
      expect = QUOTE_RE.test(trimmed) ? 'read' : null;
      if (expect === 'read') { i = readDoc(lines, i, 'module', out) ; expect = null; }
      continue;
    }

    if (DEF_RE.test(trimmed)) {
      let j = i;
      while (j < lines.length && !lines[j].trimEnd().endsWith(':')) j++;
      let k = j + 1;
      while (k < lines.length && !lines[k].trim()) k++;
      if (k < lines.length && QUOTE_RE.test(lines[k].trim())) {
        i = readDoc(lines, k, 'def', out);
      }
    }
  }
  return out;
}

function readDoc(lines, start, kind, out) {
  const quote = QUOTE_RE.exec(lines[start].trim())[1];
  const first = lines[start].trim().replace(QUOTE_RE, '');
  if (first.trimEnd().endsWith(quote)) {
    out.push({ n: start + 1, kind, body: [first.slice(0, -quote.length).trim()] });
    return start;
  }
  const body = [first.trim()];
  let i = start + 1;
  for (; i < lines.length; i++) {
    const t = lines[i].trim();
    if (t.endsWith(quote)) {
      body.push(t.slice(0, -quote.length).trim());
      break;
    }
    body.push(t);
  }
  out.push({ n: start + 1, kind, body });
  return i;
}

function bannedIn(text) {
  const hits = [];
  for (const word of BANNED) {
    // Word-boundary match; multi-word entries tolerate a line break.
    const re = new RegExp(`\\b${word.replace(/ /g, '\\s+')}\\b`, 'i');
    if (re.test(text)) hits.push(word);
  }
  return hits;
}

function check(file, only) {
  const rel = relative(ROOT, file).split(sep).join('/');
  const text = readFileSync(file, 'utf8');
  const violations = [];
  const inScope = (n) => !only || only.has(n);

  const comments = commentLines(text);
  // Trap lines are exempt. The run rule targets "//" lines; a block comment is one construct.
  const exempt = (c) => c.text.startsWith('!') || c.kind !== 'line';

  let run = [];
  const flushRun = () => {
    if (run.length && inScope(run[0].n)) {
      if (run.length > MAX_RUN) {
        violations.push({
          line: run[0].n,
          rule: 'comment-run',
          message: `${run.length} consecutive comment lines (max ${MAX_RUN}) - this is rationale, move it to agentic/CLAUDE.md`,
        });
      }
      const chars = run.map((c) => c.text).join(' ').length;
      if (chars > MAX_COMMENT_CHARS) {
        violations.push({
          line: run[0].n,
          rule: 'comment-length',
          message: `${chars}-character comment (max ${MAX_COMMENT_CHARS}) - say less, or move it to agentic/CLAUDE.md`,
        });
      }
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
    for (const word of bannedIn(c.text)) {
      violations.push({
        line: c.n,
        rule: 'rationale-word',
        message: `"${word}" - rationale belongs in agentic/CLAUDE.md`,
      });
    }
  }
  flushRun();

  if (file.endsWith('.py')) {
    for (const doc of pythonDocstrings(text)) {
      if (!inScope(doc.n)) continue;
      const isModule = doc.kind === 'module';
      // A tool's "Usage:" block is its --help text, not an explanation.
      const usage = doc.body.findIndex((l) => /^usage:/i.test(l));
      const prose = usage === -1 ? doc.body : doc.body.slice(0, usage);
      const lines = prose.filter((l) => l).length;
      const chars = prose.join(' ').trim().length;
      const maxLines = isModule ? MAX_MODULE_DOC_LINES : MAX_DOC_LINES;
      const maxChars = isModule ? MAX_MODULE_DOC_CHARS : MAX_DOC_CHARS;

      if (lines > maxLines) {
        violations.push({
          line: doc.n,
          rule: 'docstring-lines',
          message: `${lines}-line docstring (max ${maxLines}) - it is documenting, move it to agentic/CLAUDE.md`,
        });
      }
      if (chars > maxChars) {
        violations.push({
          line: doc.n,
          rule: 'docstring-length',
          message: `${chars}-character docstring (max ${maxChars}) - say what it is, not why`,
        });
      }
      for (const word of bannedIn(prose.join(' '))) {
        violations.push({
          line: doc.n,
          rule: 'rationale-word',
          message: `"${word}" in a docstring - rationale belongs in agentic/CLAUDE.md`,
        });
      }
    }
  }

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
