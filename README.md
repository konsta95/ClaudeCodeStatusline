# claude-code-statusline-picker

An interactive status line picker for [Claude Code](https://github.com/anthropics/claude-code) —
toggle components on and off, reorder them, and see a live preview of the assembled bar
before you save.

This is a working prototype built to demonstrate that a built-in `/statusline` picker is
mostly assembly-and-UI over data Claude Code already computes on every render. It runs
against Claude Code **2.1.247** (measured 2026-08-27).

```
claude-code-statusline-picker(main)|Opus5 xhigh|83K/1M|5h 25%|7d 5%|3ec15a89-…|$1.23|v2.1.247
```

## Why this exists

Claude Code's `statusLine` setting runs an external command on every render and pipes it a
JSON payload. That is strictly more powerful than a fixed set of built-in components — and
strictly less accessible: to get *any* status line at all you must write, debug, and maintain
a render script, and nothing in the product tells you what the payload contains.

Codex ships an in-TUI `/statusline` checklist with a live preview. Claude Code's `/statusline`
is a slash command that spends a conversational turn asking the model to spawn an agent which
derives a line from your shell prompt and edits `settings.json`.

This repo is the missing middle, built outside the product: a renderer that **self-describes
its own components**, and a picker that reads that description, previews candidate
configurations through the real renderer, and writes an ordered selection the renderer picks
up on its next render — without restarting or interrupting the running session.

The one thing a prototype cannot fix is the thing the request is actually about: this picker
needs a controlling terminal, so you have to leave your Claude Code session to configure your
Claude Code status line.

## Install

Requires Node.js and Python 3. No dependencies, no build step, no network access.

```bash
git clone https://github.com/konsta95/claude-code-statusline-picker
cd claude-code-statusline-picker
```

Point Claude Code at the renderer in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "node /absolute/path/to/statusline.js",
    "padding": 0
  }
}
```

Then pick your components:

```bash
python3 statusline_picker.py
```

| Key | Action |
| --- | --- |
| `↑` `↓` | move the cursor |
| `space` | toggle the component on/off |
| `←` `→` | reorder |
| `c` | toggle colours |
| `Enter` | save |
| `q` / `Esc` / `Ctrl-C` | cancel without saving |

Non-interactive uses:

```bash
python3 statusline_picker.py --show      # render a preview and exit; no TTY needed
python3 statusline_picker.py --selftest  # 26 checks including pty coverage
python3 statusline_picker.py --payload FILE   # preview against a captured payload
```

Exit codes: `0` success, `1` selftest failures, `2` environment or usage error.

## Components

The registry lives in exactly one place — `statusline.js` — and the picker discovers it by
running `node statusline.js --segments`. There is no second copy to drift.

| id | shows |
| --- | --- |
| `git-branch` | repo + branch, or the directory when not in a repo |
| `model` | model name, effort level, fast-mode flag |
| `context` | context tokens used / window size |
| `five-hour-limit` | 5-hour rate limit percentage |
| `weekly-limit` | 7-day rate limit percentage |
| `session` | full session UUID — the exact string `claude --resume` takes |
| `cost` | session cost in USD |
| `version` | running version, and a restart hint if the install has moved on |

A component that has no data is omitted rather than rendered empty. Colour is one three-step
pressure scale — green under 50%, yellow under 75%, red at or above — applied to context,
rate limits and effort alike, so a colour always means the same amount of pressure wherever
it appears.

## Configuration contract

`~/.claude/statusline-config.json`, or wherever `STATUSLINE_CONFIG` points:

```json
{ "items": ["git-branch", "model", "context"], "colors": true }
```

The semantics are modelled on Codex's `status_line_setup.rs`, and they are the whole contract:

1. `items` is both the **selection and the render order**.
2. An unknown id is skipped.
3. A duplicate id is dropped.
4. A missing or unparseable file falls back to all components in default order.
5. An empty `items` array is a valid explicit choice — an empty bar, not an error.

Because the picker previews by invoking the real renderer with `STATUSLINE_CONFIG` pointed at
a candidate file, the preview cannot drift from what saving actually produces.

## The payload

`statusline.js` reads everything from the JSON on stdin — no transcript parsing, no
model-name guessing, and no subprocesses. The script runs on every refresh and can be
cancelled mid-flight, so every operation is a bounded file read or pure computation. Even the
git branch is read directly from `.git/HEAD` by walking up the tree, because spawning `git`
per render is not affordable.

Fields observed on 2.1.247: `model`, `effort`, `fast_mode`, `thinking`, `session_id`,
`session_name`, `prompt_id`, `transcript_path`, `cwd`, `version`, `output_style`, `cost`,
`context_window`, `exceeds_200k_tokens`, `rate_limits`, `workspace`, plus the conditional
`vim`, `agent`, `pr` and `worktree` objects.

To see what your version actually sends, set `STATUSLINE_PAYLOAD_DUMP` — to `1` for the
default location (`~/.claude/statusline-payload-last.json`, which follows `$HOME` if you
redirect it), or to an explicit path. **This is off by default and opt-in for a reason** — a
captured payload contains your session id, working directory, cost and rate-limit state.

## Edge cases

Each of these was a real observed failure while hardening the picker, not a hypothetical. They
are listed because they are the argument for building this once, in-process, rather than
asking every user to rediscover them in their own render script:

- Entering raw mode with `tcsetattr`'s default flush **silently discards a keystroke** typed
  while the picker is still launching. Drain semantics fix it.
- CSI sequences must be consumed through their final byte, or a modified arrow key leaks its
  tail bytes into the menu as phantom keypresses.
- A lone `Esc` needs a short settle timeout to distinguish "cancel" from the head of an
  escape sequence.
- The preview renderer is user code, so it can hang (timeout plus an in-bar notice, never a
  crash), print garbage (named error, nonzero exit), or emit lines wider than the terminal
  (autowrap must be off during frames or the menu shreds).
- Config saves need unique-tempfile-plus-rename. A fixed temp name corrupts reads under two
  concurrent writers.
- The terminal must be restored on **every** exit path, `Ctrl-C` included.

## The lab

`lab/picker_lab.py` drives the real picker binary through a pty pair, under sandboxed `HOME`
directories, across 28 cases: launch-window keystrokes, ESC/CSI/SS3 parsing, hung and garbage
renderers, concurrent saves, narrow terminals, and teardown.

```bash
python3 lab/picker_lab.py
```

A check earns the right to be called evidence only once it has been observed *failing*
against a known-bad input. Both records are kept:

| file | what it is |
| --- | --- |
| `lab/picker-lab-prefix-known-bad.log` | the run against the pre-fix picker — the observed-failing baseline |
| `lab/picker-lab-postfix.log` | the run after the fixes, where those comparators flip to `ok` |

Both logs are verbatim run output, with absolute paths redacted and nothing else edited —
which is why the truncated tails, raw escape sequences and uneven column widths are still
there. The pre-fix log was captured when the lab still ran out of a volatile scratch
directory, so its two redactions (`SCRATCH-REDACTED`) covered a path carrying a session
UUID; the lab now scratches inside the repo, so the post-fix log's two redactions
(`REPO-REDACTED`) cover only a checkout path and it contains no identifiers at all.

## Status

This is a prototype demonstrating feasibility, not a supported tool. It is deliberately small
and deliberately outside the product — the point is that the built-in version would be
*smaller still*, because Claude Code already computes every value on this line.

## License

MIT — see [LICENSE](LICENSE). Licensed permissively on purpose: if any of this is useful
upstream, take it.
