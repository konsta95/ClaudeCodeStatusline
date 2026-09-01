# claude-code-statusline-picker

An interactive status line picker for [Claude Code](https://github.com/anthropics/claude-code) —
customize what your status line shows: toggle components on and off, reorder them, and see a
live preview of the assembled bar before you save.

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
needs a controlling terminal, so it cannot live inside the Claude Code TUI. The `/statusline`
command in this repo gets as close as an outside tool can — a tmux pane stacked above your
session, so you no longer leave the terminal — but that is a workaround with a dependency, not
a fix. A built-in picker would need neither.

## Install

Requires Node.js and Python 3. No dependencies, no build step, no network access.

**Platforms.** Linux and macOS. The interactive picker needs a POSIX terminal — it puts the
tty in raw mode through `termios` — so on Windows use WSL; `--show` and `--selftest` run
anywhere, and a Windows launch exits `2` with that explanation rather than a traceback. The
`/statusline` command additionally wants `tmux`, and its watchdog wants GNU `timeout`, which
base macOS does not ship: install coreutils (Homebrew names it `gtimeout`) and the command
finds either. Without one it still runs, but the wait for the pane is then unbounded.

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
| `v` | hand off to Claude Code's built-in statusline setup |
| `Enter` | save |
| `q` / `Esc` / `Ctrl-C` | cancel without saving |

Non-interactive uses:

```bash
python3 statusline_picker.py --show      # render a preview and exit; no TTY needed
python3 statusline_picker.py --selftest  # 30 checks including pty coverage
python3 statusline_picker.py --payload FILE   # preview against a captured payload
```

Exit codes: `0` **saved**, `1` selftest failures, `2` environment or usage error, `3` the human
pressed `v` and the caller should hand off to the built-in setup, `4` cancelled with nothing
written.

Cancel gets its own code rather than sharing `0` with a save. Callers usually run the picker in
a pane they cannot read — the outcome line goes to that pane's screen, not back to whoever
opened it — so the exit code is the only channel out, and a shared `0` would leave "saved" and
"cancelled" indistinguishable.

## Use it from inside Claude Code

`commands/statusline.md` is a slash command that opens the picker in a tmux pane above your
session. Copy it to `~/.claude/commands/statusline.md`. It assumes the clone lives at
`~/claude-code-statusline-picker`; if yours does not, change the path in every block of that
file, since each runs in its own shell and none inherits from another. A user command shadows
the built-in of the same name, so `/statusline` reaches it.

```bash
mkdir -p ~/.claude/commands
cp commands/statusline.md ~/.claude/commands/statusline.md
```

It does not replace Claude Code's built-in workflow — it composes with it. Pressing `v` in the
picker leaves your config untouched, exits `3`, and the command spawns the built-in
`statusline-setup` agent for you, after telling you which `statusLine` value that agent is about
to overwrite.

The mechanism is worth one line because it is not obvious: `tmux split-window` returns when the
pane *exists*, not when its command finishes, so the picker's exit code needs a sentinel file
plus `tmux wait-for` to reach the caller at all. That carrier was verified against controlled
arms — including the case where the human quits instantly and the signal beats the wait, and a
known-bad arm with no `wait-for` that loses the code entirely. The command documents both, and
the guards against the pane dying without ever signalling. The sentinel is *published* by rename
rather than written in place, so a pane that dies mid-write strands its fragment under a `.part`
name and the outcome reads as unknown — rather than a truncated `127` arriving as a confident,
wrong `1`. The pane also pre-flights its stderr file before launching, because a shell that
cannot open a redirect never runs the command and exits `1` on its own — which would report the
picker as defective for a failure that happened before it started. And when the watchdog expires
the command asks tmux whether the pane is still there before saying anything: a live pane usually
means a human taking their time, so the command reports the pane rather than an outcome, leaves it
and its files alone, and does not manufacture a result. It is careful not to claim more than that —
a live pane proves the pane exists and nothing else, not even that the picker is still running,
since a picker that has just written your config and is a step away from publishing its exit code
looks identical from outside.

Requires tmux. Without it the command shows your current bar and prints the line to run in your
own terminal, rather than opening a pane somewhere you cannot see.

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

## The statusLine JSON payload

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

## The pty regression lab

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

### The exit-code contract

```bash
python3 lab/exit_contract_lab.py
```

Exit `1` means *a defect in the picker — do not retry*; exit `2` means *an environment problem
you can fix*. They carry opposite instructions, and any uncaught exception in `main()` exits
`1` — so every environmental failure that escapes uncaught is silently relabelled as a
permanent defect. This lab pins the two places that happened: a save the filesystem refuses,
and a platform with no `termios`.

It generates its own known-bad by **mutating the current source** — deleting the guard under
test — rather than by checking out an older revision, because a history-based baseline stops
being a known-bad the moment the fix merges. Each mutation asserts its anchor appears exactly
once and aborts if it does not, so a refactor that moves a guard breaks this lab loudly
instead of letting it pass while measuring nothing.

The unsavable-directory fixture is built from `ENOTDIR` — a regular file used as the config's
parent — and not from a `0o555` mode. Permission bits are discretionary and root ignores them,
so under `sudo` the mode-based fixture would let both arms save happily, and the lab would
report failures that say nothing about the picker. A precondition proves the fixture is
genuinely unsavable before any arm is read.

### The carrier lab — the tmux pane handoff

```bash
python3 lab/carrier_lab.py
```

The exit-code lab covers what the picker does. This covers what the `/statusline` command does
with it — the pane, the sentinel file, the bounded wait, and the classification of whatever
comes back — across eleven live tmux arms. The carrier under test is **extracted from
`commands/statusline.md`** rather than retyped, so a passing arm says something about the
shipped text and not about a copy that drifted, and the four pre-fix controls are built by
reverting one region each of that same extracted text. One region each is the part that has
to be enforced rather than asserted: `F'` reverts the tail and `F"` reverts the cleanup
guard, and only the second puts any weight on that guard at all — under the old tail the
report is `missing`, so the fixed cleanup line deletes `$ERR` unaided and a control that
reverted both regions at once would have proved nothing about either.

Two of the arms are the same run reported twice. `F` holds a picker sleeping past the
watchdog; `G` holds one that has already saved and exited, with the sentinel's publication
widened until it is observable rather than raced for. Both come back `still-open` over a live
pane, and they differ only in something the carrier cannot see — so the pair is what
establishes that `still-open` carries no picker state at all. One arm alone would have read
as an edge case.

The probe behind that pair matches the picker's pid *and* its `argv`, because a reused pid
would otherwise read as still running, and it matches the exact picker path rather than a
pattern, because the pane's own shell carries that path in its command line — a `pgrep -f`
test reports the picker alive in every arm, forever.

That precision is also what makes this lab **Linux-only**. It reads `/proc/<pid>/cmdline`, and
the portable substitute — `ps -o args=` — joins argv with spaces, which the exact match cannot
survive. Where `/proc` is missing the lab therefore refuses with exit `2` instead of degrading.
Degrading is the worse option, and not symmetrically: without `/proc` the probe answers "not
running" to every question, so `F` reports a defect that is not there — loud, and someone will
chase it — while `G` passes for a reason with nothing to do with what it tests. The quiet one
is the one that would survive.

It refuses on an unusable **tmux** for the same reason. Whether a `tmux` binary exists is a
different question from the one the arms need answered: the carrier reads a pane id out of
`split-window -P -F`, and tells a live pane from a dead one with `list-panes -a -f` over a
`#{==:…}` format comparison. A tmux lacking either fails *inside* the pane, where nothing is
checking a return code — so `$PANE` comes back empty or the liveness test matches nothing, the
arm reports `missing`, and the lab exits `1`, blaming the carrier for the environment. The
preflight therefore exercises those options against a throwaway session instead of parsing
`tmux -V`. No minimum version is published here on purpose: a version string says what was
compiled rather than what this server will accept, and the capability has been measured on
exactly one tmux build — which is not enough to publish a bound.

It also requires **`timeout` or `gtimeout`** on `PATH`, and refuses with exit `2` without
them — which is a prerequisite of the *lab*, not of the carrier. The carrier tolerates their
absence by design and falls back to an unbounded `tmux wait-for`, because macOS ships neither.
But arms `F`, `F'`, `F"` and `G` exist to watch the **bound** fire while the pane is still
alive, and they do it by rewriting the carrier's own `"$TMO" 900 tmux` down to two seconds.
With `TMO` empty that rewrite lands on a branch which is never taken: the wait runs to
completion, the arms read back the stub's exit code instead of `still-open`, and four arms
disagree — reported as exit `1`, a carrier defect, for a missing coreutils. `brew install
coreutils` provides `gtimeout`.

The same refusal covers the carrier document itself. The lab extracts the shell block from
`commands/statusline.md` at *import* — before `main()`, and therefore before every guard
`main()` installs — so a document that cannot be read raised straight through Python's
blanket "uncaught exception exits `1`", reporting a carrier disagreement about a file it
never opened. That read is now guarded and exits `2` naming the path.

Failure paths were watched firing rather than assumed: breaking the carrier's reporting line
in a throwaway copy of the repo produced `CARRIER LAB FAILED (9 arms disagreed)` and exit `1`,
and removing a mutation anchor produced exit `2` — the lab refusing to build a control that
would no longer revert what it names. An anchor that occurs *more* than once is refused the
same way, because `str.replace` rewrites every occurrence: a control whose anchor quietly
stopped being unique would revert more than it claims, which is no more use than reverting
less.

### The failed-save probe

```bash
python3 lab/save_failure_probe.py
```

The command documents that nothing is written for any picker exit code other than `0`. Three
of the four non-zero codes never reach the save path at all, so the whole claim rests on `2`,
which is what a failed save exits with. This drives the real picker over a pty against a
read-only config directory and checks the config byte-for-byte afterwards, along with the
directory it writes its temp into. It refuses to report at all under `sudo`: mode `0o555` does
not bind for root, the save would succeed, and a pass measured that way would be vacuous. It
refuses on a platform with no `os.geteuid` for the same reason, and before trusting any result
it writes a witness file into the directory to confirm the mode actually took — `chmod` can
report success and still not bind, and each way that happens ends in a green pass that measured
nothing. A `chmod` the filesystem refuses outright is the other half of that, and exits `2` as
well: it is not a picker defect that a mount will not take POSIX modes. So does a failure to
restore the mode afterwards — except that one is only *reported*, because by then the
measurement has already been made, and an exception from a `finally` does not travel beside the
pending return, it replaces it.

The exit code alone cannot carry this claim. The picker has **eight** routes to exit `2` and
only one of them is the failed save — the other seven fire before `save_config` is reached.
A missing `node`, an absent tty, a config that will not load all
arrive with the config untouched and no temp stranded — precisely the footprint of a clean
pass. So the probe also requires the save-specific `could not save` diagnostic, and when `2`
arrives without it the probe returns `2` rather than `1`: a picker that never reached the save
leaves the question unanswered, which is not the same as the guarantee being broken.

## Status

This is a prototype demonstrating feasibility, not a supported tool. It is deliberately small
and deliberately outside the product — the point is that the built-in version would be
*smaller still*, because Claude Code already computes every value on this line.

## License

MIT — see [LICENSE](LICENSE). Licensed permissively on purpose: if any of this is useful
upstream, take it.
