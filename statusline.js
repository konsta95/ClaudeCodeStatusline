#!/usr/bin/env node
'use strict';

// Claude Code statusline: repo(branch)|model effort|tokens|5h|7d|session uuid|$|version
// Receives the session JSON on stdin. Context, rate limits and effort are read DIRECTLY from
// the payload (context_window / rate_limits / effort) — no transcript parsing, no model-name
// guessing.
//
// Payload schema measured against the running Claude Code 2.1.247 payload builder
// (2026-08-27). Fields shift between versions — this is a measurement, not a contract:
//   context_window: { total_input_tokens, total_output_tokens, context_window_size,
//                     current_usage, used_percentage, remaining_percentage }
//   rate_limits:    { five_hour?: {used_percentage, resets_at}, seven_day?: {...} }  (0-100)
//   effort:         { level }   fast_mode: bool   session_name: string
//
// Wire it up in settings.json:
//   "statusLine": { "type": "command", "command": "node /path/to/statusline.js", "padding": 0 }
//
// The script spawns no subprocesses. It runs on every refresh and can be cancelled
// mid-flight, so every operation here is a bounded file read or pure computation.

const fs = require('fs');
const os = require('os');
const path = require('path');

// Base palette: the codex statusline accent scheme, measured from openai/codex
// codex-rs/tui/src/bottom_pane/status_line_style.rs (Apache-2.0, read 2026-08-25):
// fallback accents Path=green Branch=magenta Usage=green, separators dim, brights
// softened to the NORMAL ansi palette so the terminal theme decides the hues.
// Overlaid on it: the PRESSURE readouts — effort level, context tokens, limit
// PERCENTAGES — escalate green→yellow→red on one three-step scale, while the
// 5h/7d labels stay on the static magenta Limit accent; version colors by
// freshness (green current, yellow stale); the session uuid is white. The model
// NAME wears the clay accent measured from the Claude Code TUI's own dark theme.
// Truecolor is the line's ONE deliberate exception to the normal-ansi softening;
// the theme's own 16-color fallback for that token is redBright, so degradation
// stays on-brand.
const C = {
  reset: '\x1b[0m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  magenta: '\x1b[35m',
  white: '\x1b[37m',
  clay: '\x1b[38;2;215;119;87m',
};

// "105K", "1M" — capital K, matching the "999K/1M" reading style.
function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1_000) return Math.round(n / 1_000) + 'K';
  return String(n);
}

// One three-step escalative scale for every pressure readout: <50 green,
// <75 yellow, >=75 red. Context, rate limits, and numeric effort levels all ride
// this scale, so a colour always means the same amount of pressure regardless of
// which segment it appears in.
function pctColor(pct) {
  return pct >= 75 ? C.red : pct >= 50 ? C.yellow : C.green;
}

// The five effort levels compress onto the same three steps: low green,
// medium/high yellow, xhigh/max red. An unrecognized level falls back to dim —
// Claude's native footer renders effort dim, so no false alarm and no stray hue.
function effortColor(level) {
  if (Number.isFinite(level)) return pctColor(level);
  const s = String(level).toLowerCase();
  if (s === 'xhigh' || s === 'max') return C.red;
  if (s === 'medium' || s === 'high') return C.yellow;
  if (s === 'low') return C.green;
  return C.dim;
}

// Find the git root by walking upward; read the branch straight from .git/HEAD
// (no git process is spawned — the statusline runs on every refresh).
// The payload's workspace.repo / workspace.git_worktree do not carry the branch, so they
// cannot replace this.
function gitInfo(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12 && dir; i++) {
    const gitPath = path.join(dir, '.git');
    try {
      const st = fs.statSync(gitPath);
      let headFile = path.join(gitPath, 'HEAD');
      if (st.isFile()) {
        // worktree: .git is a file pointing at the real git directory
        const m = fs.readFileSync(gitPath, 'utf8').match(/gitdir:\s*(.+)/);
        if (m) headFile = path.join(m[1].trim(), 'HEAD');
      }
      const head = fs.readFileSync(headFile, 'utf8').trim();
      const ref = head.match(/^ref:\s*refs\/heads\/(.+)$/);
      return { repo: path.basename(dir), branch: ref ? ref[1] : head.slice(0, 7) };
    } catch (_) { /* no .git here — keep walking up */ }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

// Context tokens from the payload's context_window field, rendered "100K/1M" — no fill
// bar, no percent. The CLI computes total_input_tokens as input + cache_creation +
// cache_read and states the window size directly, so neither has to be derived from the
// transcript. The whole span escalates on the three-step scale; missing token fields mean
// the segment is omitted — no crash, no fallback to transcript parsing.
function contextSeg(cw) {
  if (!cw) return null;
  const size = cw.context_window_size;
  const used = cw.total_input_tokens;
  if (!Number.isFinite(size) || size <= 0 || !Number.isFinite(used)) return null;
  let pct = cw.used_percentage;
  if (!Number.isFinite(pct)) pct = (used / size) * 100;
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  return pctColor(p) + fmtTokens(used) + '/' + fmtTokens(size) + C.reset;
}

// Rate-limit windows (5h / 7d). Only five_hour and seven_day reach the statusline — the API's
// opus/sonnet/overage sub-windows are not part of this payload.
// NOTE: used_percentage is born from the API's utilization*100 and utilization is nullable,
// but null*100 === 0, so empty data is indistinguishable from a genuine 0% — nothing in the
// payload separates them. 0% is shown as-is (a fresh 5h window really is ~0%); the guard
// below covers the cases that ARE distinguishable: missing field, NaN, wrong type.
// The label is identity, the number is pressure: label on the static Limit accent,
// percentage on the three-step scale.
function limitSeg(label, win) {
  if (!win || !Number.isFinite(win.used_percentage)) return null;
  const p = Math.max(0, Math.round(win.used_percentage));
  return C.magenta + label + C.reset + ' ' + pctColor(p) + p + '%' + C.reset;
}

// Locate the `claude` executable without spawning anything: an explicit override first,
// then a scan of PATH entries. Returns null when nothing is found, which makes the
// freshness check degrade to "unverified" instead of guessing a path that may not exist.
// Guessing here would reproduce the exact defect this project argues against — a wrong
// path that fails silently rather than visibly.
function findClaudeBin() {
  const override = process.env.STATUSLINE_CLAUDE_BIN;
  if (override) return override;
  const dirs = (process.env.PATH || '').split(path.delimiter);
  for (const d of dirs) {
    if (!d) continue;
    const candidate = path.join(d, 'claude');
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch (_) { /* not here — keep scanning */ }
  }
  return null;
}

// Version + install skew: the payload's version is the RUNNING process's version, while the
// installed package.json says which version a fresh launch would resolve to. A difference
// means a stale process is still running. This is worth surfacing: an install that updates
// underneath a long-lived session is invisible otherwise, and the session keeps running the
// old build for hours. Spawn-free like the rest of the script: realpath plus one
// package.json read, with every error skipped rather than raised.
function versionSeg(running) {
  if (typeof running !== 'string' || !running) return null;
  let fresh = null;
  try {
    const bin = findClaudeBin();
    if (bin) {
      const exe = fs.realpathSync(bin);
      const pkg = path.join(path.dirname(path.dirname(exe)), 'package.json');
      fresh = JSON.parse(fs.readFileSync(pkg, 'utf8')).version || null;
    }
  } catch (_) { /* install unreadable — show just the running version */ }
  if (fresh && fresh !== running) {
    return C.yellow + 'v' + running + '→' + fresh + ' restart' + C.reset;
  }
  if (fresh === running) return C.green + 'v' + running + C.reset;
  // install unreadable: freshness unverified, so neither green nor yellow is honest
  return C.dim + 'v' + running + C.reset;
}

// ── Segment registry + selection config ─────────────────────────────────────
// Mirrors Codex's status_line model (codex-rs/tui/src/bottom_pane/status_line_setup.rs,
// read 2026-08-25): the items array is the selection AND the render order; an unknown id is
// skipped, a duplicate is dropped, and a missing or broken file means all segments in default
// order. An empty items array is a deliberate choice (an empty line), not an error state.
// STATUSLINE_CONFIG overrides the path: the picker previews a candidate config through THIS
// same renderer, so a preview cannot drift away from the real line.
const SEGMENTS = [
  ['git-branch', 'Repo + branch (dir when not a repo)'],
  ['model', 'Model + effort + fast'],
  ['context', 'Context tokens used/window'],
  ['five-hour-limit', '5h rate limit'],
  ['weekly-limit', '7d rate limit'],
  ['session', 'Session full id'],
  ['cost', 'Session cost USD'],
  ['version', 'Version + install skew'],
];
const SEGMENT_IDS = SEGMENTS.map((s) => s[0]);
const CONFIG_PATH = process.env.STATUSLINE_CONFIG
  || path.join(os.homedir(), '.claude', 'statusline-config.json');

function loadConfig() {
  const def = { items: SEGMENT_IDS.slice(), colors: true };
  let text;
  try { text = fs.readFileSync(CONFIG_PATH, 'utf8'); } catch (_) { return def; }
  try {
    const cfg = JSON.parse(text);
    const items = Array.isArray(cfg.items)
      ? cfg.items.filter((id, i) => SEGMENT_IDS.includes(id) && cfg.items.indexOf(id) === i)
      : def.items;
    return { items, colors: cfg.colors !== false };
  } catch (_) { return def; }
}

// The picker's only id source — no copy of the registry exists anywhere else.
if (process.argv.includes('--segments')) {
  process.stdout.write(JSON.stringify(SEGMENTS.map((s) => ({ id: s[0], label: s[1] }))));
  process.exit(0);
}

let raw = '';
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', () => {
  let out = '';
  try {
    // A PowerShell 5.1 pipe may prepend a UTF-8 BOM — strip it before parsing
    if (raw.charCodeAt(0) === 0xfeff) raw = raw.slice(1);
    const input = JSON.parse(raw);

    // Optional empirical probe: keep the latest payload on disk so you can see what the
    // RUNNING version actually sends, rather than what the docs claim it sends. Fields
    // shift between versions, so a measurement beats a document here.
    //
    // OFF BY DEFAULT and opt-in only. The payload contains your session id, working
    // directory, cost and rate-limit state — set STATUSLINE_PAYLOAD_DUMP only if you want
    // that written to disk. "1"/"true" writes the default location under $HOME (so a
    // redirected HOME redirects the probe with it); any other value is used as the path.
    // Never let the probe take the line down.
    const dumpSetting = process.env.STATUSLINE_PAYLOAD_DUMP;
    if (dumpSetting) {
      const dumpPath = (dumpSetting === '1' || dumpSetting === 'true')
        ? path.join(os.homedir(), '.claude', 'statusline-payload-last.json')
        : dumpSetting;
      // Unique temp name, not a fixed ".tmp": the status line re-renders on every
      // refresh and several sessions can render at once, so a shared temp name lets
      // one writer's partial bytes get renamed into place by another.
      const tmp = dumpPath + '.' + process.pid + '.tmp';
      try {
        fs.writeFileSync(tmp, JSON.stringify(input, null, 1));
        fs.renameSync(tmp, dumpPath);
      } catch (_) {
        // the probe is a side channel — rendering continues
        try { fs.unlinkSync(tmp); } catch (_) { }
      }
    }

    const cwd = (input.workspace && input.workspace.current_dir) || input.cwd || process.cwd();
    const modelId = (input.model && input.model.id) || '';
    const modelName = (input.model && input.model.display_name) || modelId || '?';

    // Builders by id; null means the segment stays off the line (no placeholders).
    const builders = {
      // Fused repo(branch), unspaced: the repo carries the Path accent and "(branch)" the
      // Branch accent — the two accents codex gives the dir and branch items it keeps
      // separate.
      'git-branch': () => {
        const git = gitInfo(cwd);
        return git
          ? C.green + git.repo + C.reset + C.magenta + '(' + git.branch + ')' + C.reset
          : C.green + path.basename(cwd) + C.reset;
      },
      // effort appears in the payload only for models that support it, so absence is the
      // normal state, not an error. The display name is compressed: "Fable 5" -> "Fable5".
      // The name wears the clay accent; fast stays dim; the effort level keeps its
      // escalative colour.
      'model': () => {
        let model = C.clay + modelName.replace(/\s+/g, '') + C.reset;
        const level = input.effort && input.effort.level;
        if ((typeof level === 'string' && level) || Number.isFinite(level)) {
          model += ' ' + effortColor(level) + level + C.reset;
        }
        if (input.fast_mode === true) model += ' ' + C.dim + 'fast' + C.reset;
        return model;
      },
      'context': () => contextSeg(input.context_window),
      'five-hour-limit': () => limitSeg('5h', input.rate_limits && input.rate_limits.five_hour),
      'weekly-limit': () => limitSeg('7d', input.rate_limits && input.rate_limits.seven_day),
      // Session id: the bare FULL UUID — the exact string `claude --resume <id>` takes and
      // the stem of the transcript filename (<session_id>.jsonl), copyable straight off the
      // line. The /rename session_name is deliberately NOT rendered. White is normal
      // intensity 37, so the no-brights softening holds.
      'session': () => {
        const sid = typeof input.session_id === 'string' && input.session_id
          ? input.session_id
          : null;
        return sid ? C.white + sid + C.reset : null;
      },
      'cost': () => {
        const cost = input.cost && input.cost.total_cost_usd;
        return typeof cost === 'number' && cost > 0
          ? C.green + '$' + cost.toFixed(2) + C.reset
          : null;
      },
      'version': () => versionSeg(input.version),
    };

    const cfg = loadConfig();
    const parts = [];
    for (const id of cfg.items) {
      const seg = builders[id]();
      if (seg) parts.push(seg);
    }
    out = parts.join(C.dim + '|' + C.reset);
    if (!cfg.colors) out = out.replace(/\x1b\[[0-9;]*m/g, '');
  } catch (e) {
    // Never leave the bar blank: a named error is debuggable, an empty line is not.
    out = C.dim + 'statusline error: ' + e.message + C.reset;
  }
  process.stdout.write(out);
});
