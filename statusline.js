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

const rgb = (r, g, b) => '\x1b[38;2;' + r + ';' + g + ';' + b + 'm';

// ── Color schemes ───────────────────────────────────────────────────────────
// A scheme fills the SAME semantic slots; builders never name a color
// directly, so a scheme is data and per-item overrides can layer over it.
// Slots: path (directory / repo), branch, github, label (5h/7d), model,
// session, cost, sep, dim, fast, ok/mid/high (the one pressure scale),
// fresh/stale (version freshness).
//   codex        the original look described above — NORMAL ansi so the
//                terminal theme decides hues, clay the one truecolor
//                exception.
//   claude-code  the Claude Code TUI's own dark theme, measured out of the
//                installed binary (bin/claude.exe, read 2026-09-01); the
//                source theme key is quoted beside each slot. All truecolor
//                by design — the point is the CLI's exact hues.
//   mono         no accents at all: every slot empty, pressure told by the
//                numbers alone. paint() below emits ZERO bytes for empty
//                slots, so mono output is byte-clean, not reset-littered.
const SCHEMES = {
  codex: {
    path: C.green, branch: C.magenta, github: C.magenta, label: C.magenta,
    model: C.clay, session: C.white, cost: C.green, sep: C.dim, dim: C.dim,
    fast: C.dim, ok: C.green, mid: C.yellow, high: C.red,
    fresh: C.green, stale: C.yellow,
  },
  'claude-code': {
    path: rgb(71, 130, 200),      // 'ide'
    branch: rgb(175, 135, 255),   // 'autoAccept'
    github: rgb(177, 185, 249),   // 'permission'
    label: rgb(177, 185, 249),    // 'permission'
    model: rgb(215, 119, 87),     // 'claude' — the clay itself
    session: rgb(153, 153, 153),  // 'inactive'
    cost: rgb(78, 186, 101),      // 'success'
    sep: rgb(80, 80, 80),         // 'subtle'
    dim: C.dim,
    fast: rgb(255, 106, 0),       // 'fastMode'
    ok: rgb(78, 186, 101),        // 'success'
    mid: rgb(255, 193, 7),        // 'warning'
    high: rgb(255, 107, 128),     // 'error'
    fresh: rgb(78, 186, 101),     // 'success'
    stale: rgb(255, 193, 7),      // 'warning'
  },
  mono: {
    path: '', branch: '', github: '', label: '', model: '', session: '',
    cost: '', sep: '', dim: '', fast: '', ok: '', mid: '', high: '',
    fresh: '', stale: '',
  },
};

// Wrap text in a color only when the slot actually carries one — an empty
// slot must contribute zero escape bytes.
function paint(code, text) {
  return code ? code + text + C.reset : text;
}

// Per-item override spec: a named normal-intensity ansi color or "#RRGGBB".
// Anything else resolves to null and the scheme slot stands — a typo in the
// config must never take the bar down or leak half an escape sequence.
const NAMED = {
  red: C.red, green: C.green, yellow: C.yellow, magenta: C.magenta,
  white: C.white, dim: C.dim, blue: '\x1b[34m', cyan: '\x1b[36m',
};
function colorSpec(spec) {
  if (typeof spec !== 'string') return null;
  if (Object.prototype.hasOwnProperty.call(NAMED, spec)) return NAMED[spec];
  const m = spec.match(/^#([0-9a-fA-F]{6})$/);
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return rgb((n >> 16) & 255, (n >> 8) & 255, n & 255);
}

// "105K", "1M" — capital K, matching the "999K/1M" reading style.
function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1_000) return Math.round(n / 1_000) + 'K';
  return String(n);
}

// One three-step escalative scale for every pressure readout: <50 ok,
// <75 mid, >=75 high, in the active scheme's hues. Context, rate limits, and
// numeric effort levels all ride this scale, so a colour always means the
// same amount of pressure regardless of which segment it appears in. Pressure
// colors stay semantic: per-item overrides never touch them.
function pctColor(pct, pal) {
  return pct >= 75 ? pal.high : pct >= 50 ? pal.mid : pal.ok;
}

// The five effort levels compress onto the same three steps: low ok,
// medium/high mid, xhigh/max high. An unrecognized level falls back to dim —
// Claude's native footer renders effort dim, so no false alarm and no stray hue.
function effortColor(level, pal) {
  if (Number.isFinite(level)) return pctColor(level, pal);
  const s = String(level).toLowerCase();
  if (s === 'xhigh' || s === 'max') return pal.high;
  if (s === 'medium' || s === 'high') return pal.mid;
  if (s === 'low') return pal.ok;
  return pal.dim;
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

// GitHub org/repo from the origin remote, spawn-free: .git/config is read
// directly (bounded file reads; a linked worktree's config lives in the
// common git dir, reached through its commondir pointer file). Repos without
// an origin, and origins that are not github, yield null — the segment stays
// off the line rather than guessing.
function gitRemote(startDir) {
  let dir = startDir;
  for (let i = 0; i < 12 && dir; i++) {
    const gitPath = path.join(dir, '.git');
    try {
      const st = fs.statSync(gitPath);
      let gitDir = gitPath;
      if (st.isFile()) {
        const m = fs.readFileSync(gitPath, 'utf8').match(/gitdir:\s*(.+)/);
        if (!m) return null;
        gitDir = path.resolve(dir, m[1].trim());
      }
      let cfg = path.join(gitDir, 'config');
      if (!fs.existsSync(cfg)) {
        const common = path.join(gitDir, 'commondir');
        cfg = path.join(
          path.resolve(gitDir, fs.readFileSync(common, 'utf8').trim()),
          'config');
      }
      const text = fs.readFileSync(cfg, 'utf8');
      const sec = text.match(/\[remote "origin"\][^[]*/);
      if (!sec) return null;
      const url = sec[0].match(/url\s*=\s*(.+)/);
      if (!url) return null;
      const gh = url[1].trim().match(/github\.com[:/]+([^/\s]+\/[^/\s]+?)(?:\.git)?\/?$/);
      return gh ? gh[1] : null;
    } catch (_) { /* no .git or unreadable config — keep walking up */ }
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
function contextSeg(cw, pal) {
  if (!cw) return null;
  const size = cw.context_window_size;
  const used = cw.total_input_tokens;
  if (!Number.isFinite(size) || size <= 0 || !Number.isFinite(used)) return null;
  let pct = cw.used_percentage;
  if (!Number.isFinite(pct)) pct = (used / size) * 100;
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  return paint(pctColor(p, pal), fmtTokens(used) + '/' + fmtTokens(size));
}

// Rate-limit windows (5h / 7d). Only five_hour and seven_day reach the statusline — the API's
// opus/sonnet/overage sub-windows are not part of this payload.
// NOTE: used_percentage is born from the API's utilization*100 and utilization is nullable,
// but null*100 === 0, so empty data is indistinguishable from a genuine 0% — nothing in the
// payload separates them. 0% is shown as-is (a fresh 5h window really is ~0%); the guard
// below covers the cases that ARE distinguishable: missing field, NaN, wrong type.
// The label is identity, the number is pressure: label on the Limit accent
// (per-item overridable), percentage on the three-step scale (never
// overridable).
function limitSeg(label, win, pal, labelColor) {
  if (!win || !Number.isFinite(win.used_percentage)) return null;
  const p = Math.max(0, Math.round(win.used_percentage));
  return paint(labelColor, label) + ' ' + paint(pctColor(p, pal), p + '%');
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
function versionSeg(running, pal) {
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
    return paint(pal.stale, 'v' + running + '→' + fresh + ' restart');
  }
  if (fresh === running) return paint(pal.fresh, 'v' + running);
  // install unreadable: freshness unverified, so neither fresh nor stale is honest
  return paint(pal.dim, 'v' + running);
}

// ── Segment registry + selection config ─────────────────────────────────────
// Mirrors Codex's status_line model (codex-rs/tui/src/bottom_pane/status_line_setup.rs,
// read 2026-08-25): the items array is the selection AND the render order; an unknown id is
// skipped, a duplicate is dropped, and a missing or broken file means all segments in default
// order. An empty items array is a deliberate choice (an empty line), not an error state.
// STATUSLINE_CONFIG overrides the path: the picker previews a candidate config through THIS
// same renderer, so a preview cannot drift away from the real line.
// Third column: colorable — whether item_colors may override the segment's
// identity accent. context and version are pure semantics (pressure and
// freshness), so an override there would repaint meaning, not identity.
const SEGMENTS = [
  ['git-branch', 'Repo + branch fused (dir when not a repo)', true],
  ['directory', 'Directory name', true],
  ['branch', 'Git branch (repos only)', true],
  ['github', 'GitHub org/repo from origin', true],
  ['model', 'Model + effort + fast', true],
  ['context', 'Context tokens used/window', false],
  ['five-hour-limit', '5h rate limit', true],
  ['weekly-limit', '7d rate limit', true],
  ['session', 'Session full id', true],
  ['cost', 'Session cost USD', true],
  ['version', 'Version + install skew', false],
];
const SEGMENT_IDS = SEGMENTS.map((s) => s[0]);
const COLORABLE = new Set(SEGMENTS.filter((s) => s[2]).map((s) => s[0]));
// The granular git segments are opt-in: the no-config default keeps the
// original eight, so an existing bar renders byte-identically after an
// upgrade. Toggling directory/branch/github on is the picker's job.
const DEFAULT_IDS = SEGMENT_IDS.filter(
  (id) => id !== 'directory' && id !== 'branch' && id !== 'github');
const CONFIG_PATH = process.env.STATUSLINE_CONFIG
  || path.join(os.homedir(), '.claude', 'statusline-config.json');

function loadConfig() {
  const def = { items: DEFAULT_IDS.slice(), colors: true, scheme: 'codex', itemColors: {} };
  let text;
  try { text = fs.readFileSync(CONFIG_PATH, 'utf8'); } catch (_) { return def; }
  try {
    const cfg = JSON.parse(text);
    const items = Array.isArray(cfg.items)
      ? cfg.items.filter((id, i) => SEGMENT_IDS.includes(id) && cfg.items.indexOf(id) === i)
      : def.items;
    const scheme = typeof cfg.scheme === 'string'
      && Object.prototype.hasOwnProperty.call(SCHEMES, cfg.scheme)
      ? cfg.scheme : 'codex';
    // item_colors: {"segment-id": "green" | "#87afff", ...} — unknown ids,
    // non-colorable ids and unparseable specs are dropped one by one, never
    // the whole map and never the line.
    const itemColors = {};
    if (cfg.item_colors && typeof cfg.item_colors === 'object' && !Array.isArray(cfg.item_colors)) {
      for (const id of Object.keys(cfg.item_colors)) {
        const code = colorSpec(cfg.item_colors[id]);
        if (code && COLORABLE.has(id)) itemColors[id] = code;
      }
    }
    return { items, colors: cfg.colors !== false, scheme, itemColors };
  } catch (_) { return def; }
}

// The picker's only id source — no copy of the registry exists anywhere else.
// colorable rides along so the picker knows where its accent submode applies.
if (process.argv.includes('--segments')) {
  process.stdout.write(JSON.stringify(
    SEGMENTS.map((s) => ({ id: s[0], label: s[1], colorable: s[2] }))));
  process.exit(0);
}

// The picker's only scheme-name source, same single-carrier rule as above.
// Order here is the picker's cycling order.
if (process.argv.includes('--schemes')) {
  process.stdout.write(JSON.stringify(Object.keys(SCHEMES)));
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

    const cfg = loadConfig();
    const pal = SCHEMES[cfg.scheme];
    // Identity accents honor per-item overrides; pressure/freshness never do.
    const accent = (id, slot) => cfg.itemColors[id] || slot;

    // Builders by id; null means the segment stays off the line (no placeholders).
    const builders = {
      // Fused repo(branch), unspaced: the repo carries the Path accent and "(branch)" the
      // Branch accent — the two accents codex gives the dir and branch items it keeps
      // separate. An override unifies both halves: the fused segment is ONE item.
      'git-branch': () => {
        const git = gitInfo(cwd);
        const ov = cfg.itemColors['git-branch'];
        return git
          ? paint(ov || pal.path, git.repo) + paint(ov || pal.branch, '(' + git.branch + ')')
          : paint(ov || pal.path, path.basename(cwd));
      },
      // The granular trio behind the fused segment, each independently
      // toggleable: directory always renders, branch and github only where a
      // repo / github origin actually exists.
      'directory': () => paint(accent('directory', pal.path), path.basename(cwd)),
      'branch': () => {
        const git = gitInfo(cwd);
        return git ? paint(accent('branch', pal.branch), git.branch) : null;
      },
      'github': () => {
        const remote = gitRemote(cwd);
        return remote ? paint(accent('github', pal.github), remote) : null;
      },
      // effort appears in the payload only for models that support it, so absence is the
      // normal state, not an error. The display name is compressed: "Fable 5" -> "Fable5".
      // The name wears the model accent; fast wears the fast slot; the effort level keeps
      // its escalative colour.
      'model': () => {
        let model = paint(accent('model', pal.model), modelName.replace(/\s+/g, ''));
        const level = input.effort && input.effort.level;
        if ((typeof level === 'string' && level) || Number.isFinite(level)) {
          model += ' ' + paint(effortColor(level, pal), String(level));
        }
        if (input.fast_mode === true) model += ' ' + paint(pal.fast, 'fast');
        return model;
      },
      'context': () => contextSeg(input.context_window, pal),
      'five-hour-limit': () => limitSeg('5h', input.rate_limits && input.rate_limits.five_hour,
        pal, accent('five-hour-limit', pal.label)),
      'weekly-limit': () => limitSeg('7d', input.rate_limits && input.rate_limits.seven_day,
        pal, accent('weekly-limit', pal.label)),
      // Session id: the bare FULL UUID — the exact string `claude --resume <id>` takes and
      // the stem of the transcript filename (<session_id>.jsonl), copyable straight off the
      // line. The /rename session_name is deliberately NOT rendered. White is normal
      // intensity 37, so the no-brights softening holds.
      'session': () => {
        const sid = typeof input.session_id === 'string' && input.session_id
          ? input.session_id
          : null;
        return sid ? paint(accent('session', pal.session), sid) : null;
      },
      'cost': () => {
        const cost = input.cost && input.cost.total_cost_usd;
        return typeof cost === 'number' && cost > 0
          ? paint(accent('cost', pal.cost), '$' + cost.toFixed(2))
          : null;
      },
      'version': () => versionSeg(input.version, pal),
    };

    const parts = [];
    for (const id of cfg.items) {
      const seg = builders[id]();
      if (seg) parts.push(seg);
    }
    out = parts.join(paint(pal.sep, '|'));
    if (!cfg.colors) out = out.replace(/\x1b\[[0-9;]*m/g, '');
  } catch (e) {
    // Never leave the bar blank: a named error is debuggable, an empty line is not.
    out = C.dim + 'statusline error: ' + e.message + C.reset;
  }
  process.stdout.write(out);
});
