# /statusline

**Input:** $ARGUMENTS

Customize the status line without leaving the conversation. The default interface is
**AskUserQuestion popups** — they surface on top of the input box, exactly where a permission
prompt surfaces, with each candidate bar rendered through the real renderer as its preview.
The tmux pane TUI remains available **on request** for free-form reordering; inside it `v`
hands off to Claude Code's own built-in statusline setup.

Copy this file to `~/.claude/commands/statusline.md`. A user command shadows the built-in of
the same name, so `/statusline` reaches this instead. If you cloned the repo somewhere other
than `~/ClaudeCodeStatusline`, change the path in **every** block below — each one
runs in its own shell, so none of them can inherit the path from another.

**Do not spawn the `statusline-setup` agent up front, and do not edit `settings.json`.** That
key already points at this repo's `statusline.js` and does not need to change. What changes is
the *component selection* in `~/.claude/statusline-config.json`, which the renderer re-reads on
every refresh — so a save lands on the next repaint, with no restart and no interruption to the
running session. The one path that legitimately reaches `statusline-setup` is exit code 3.

## Default — pick with AskUserQuestion popups

Take this path for a bare `/statusline` and for any argument that names components or
preferences. It works everywhere Claude Code runs — native Windows included — because
nothing in it needs a TTY or tmux: the popups render inside the conversation, and the save
goes through the picker's validated non-interactive path.

1. **Measure, never remember.** Fetch the registry and the current state:

   ```bash
   node "$HOME/ClaudeCodeStatusline/statusline.js" --segments
   ```

   ```bash
   python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py" --show
   ```

2. **Build 3–4 candidate selections** spanning the request — the current bar, and
   variations that follow what the argument asked for (drop noisy components, lead with
   what they watch, a minimal bar). Write each as `{"items": [...], "colors": true}` —
   plus `"scheme"` and/or `"item_colors"` whenever the request named a scheme or a
   per-component colour, so the preview shows the look actually being chosen — to
   its own temp file and render it through the **real renderer**:

   ```bash
   python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py" --show --config /tmp/statusline-candidate-1.json
   ```

   The `preview:` line of each run is that candidate's measured bar. Previews are
   rendered, never imagined.

3. **Ask.** One AskUserQuestion, one option per candidate, the rendered bar in each
   option's `preview` field, the selection list in its description. "Other" takes a
   hand-typed list, so say so in the question. Ask about colors or the colour scheme only
   when the request left it open. If the human's answer implies changes, re-render and re-ask — bounded,
   and never more than a couple of rounds before offering the pane instead.

4. **Save through the picker — never hand-write the config:**

   ```bash
   python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py" --apply "git-branch,model,context" --colors on
   ```

   `--scheme NAME` may ride along (or run alone) to pick a colour scheme — the names come
   from `node statusline.js --schemes`, never from a hardcoded list. When the request (and
   so the previewed candidates) named a scheme, PASS it here: a save that omits `--scheme`
   keeps whatever the config already holds, which is not necessarily what was just shown.

   `--apply` is strict where the config-file parse is forgiving: unknown or duplicate ids —
   and an unknown `--scheme` name — exit 2 naming them, and nothing is written. Exit 2 is
   not one condition, so read the stderr line before acting: `unknown segment id` /
   `duplicate segment id` — re-fetch the registry and fix the list; `unknown scheme` /
   `cannot validate --scheme` — re-fetch the scheme list, or drop the flag if the renderer
   is too old to answer `--schemes`; `could not save` — report the save error (a read-only or full
   disk is not a selection problem); anything else is a renderer or environment failure —
   report it verbatim. Never retry blind, and never write the JSON by hand to get around a
   refusal.

5. **Confirm.** Run `--show` again and report the new bar. The renderer re-reads the
   config on every refresh, so the change lands on the next repaint with no restart.

**What this path cannot do:** AskUserQuestion holds at most 4 options with static
previews, so it offers *candidate sets*, not per-component toggling, and free-form
reordering through popup rounds is clumsy. When the human wants to rearrange components
by hand, offer the pane below instead of another round of popups.

## Full-reorder TUI — the tmux pane (on request)

Open the pane when the human asks for it — "pane", "tui", "full picker", or free-form
reordering — or when popup rounds start fighting the request. Every component sits on one
screen: `space` toggles, arrows reorder, the preview updates per keystroke. The pane itself
needs tmux; native Windows has none, so there use the popup path above — or run the picker
directly in your own terminal, which it drives natively on Windows 10+ (msvcrt + VT).

Run this with a Bash `timeout` of `600000`. A human reads the bar, thinks, and toggles; the
default 120s tool timeout would otherwise background the call mid-edit. If it does get
backgrounded that is not a failure — the pane keeps running and the exit code still arrives in
the output file when it finishes.

```bash
PICKER="$HOME/ClaudeCodeStatusline/statusline_picker.py"
CHAN="statusline-picker-$$"
SENT="/tmp/statusline-picker-$$.rc"; ERR="/tmp/statusline-picker-$$.err"
rm -f "$SENT" "$SENT.part" "$ERR"
TMO=$(command -v timeout || command -v gtimeout || true)   # macOS has neither by default
PANE=$(tmux split-window -v -f -l 16 -P -F '#{pane_id}' \
  "if : 2>>\"$ERR\" && exec 9>>\"$ERR\"; then python3 \"$PICKER\" 2>&9; RC=\$?; else RC=setup; fi
   echo \$RC > \"$SENT.part\" && mv \"$SENT.part\" \"$SENT\"; tmux wait-for -S $CHAN") \
  && if [ -n "$TMO" ]; then "$TMO" 900 tmux wait-for "$CHAN"
     else tmux wait-for "$CHAN"; fi

if [ -f "$SENT" ]; then
  RC=$(cat "$SENT")
  case "$RC" in 0|1|2|3|4) ;; *) RC="unclassified:[$RC]" ;; esac
elif [ -n "$(tmux list-panes -a -f "#{==:#{pane_id},$PANE}" -F '#{pane_id}' 2>/dev/null)" ]; then
  RC=still-open
else
  RC=missing
fi
echo "picker-exit=$RC"
[ -s "$ERR" ] && { echo "picker-stderr:"; cat "$ERR"; }
[ "$RC" = still-open ] || rm -f "$SENT" "$SENT.part" "$ERR"
```

`-v -f` opens the pane at the terminal's TRUE bottom — full width, below every existing
pane, beside where the status line itself lives. Plain `-v` splits only the current pane,
so in an already-split window the picker would land mid-stack (measured on tmux 3.6: from
a top pane, `-v` placed it at row 15 of 40, `-v -f` at row 35). `-l 16` gives it the ~14
lines it needs; a side-by-side `-h` split is too narrow and the preview line clips
mid-word. Focus moves to the pane, which is the point — the human types there.

Keys once it opens: `↑`/`↓` move, `space` toggles, `←`/`→` reorder within the enabled block,
`c` cycles colour schemes (codex → claude-code → mono → off), `a` cycles the selected
component's accent colour, **`v` built-in setup**, `Enter` saves, `q`/`Esc` cancels.

### Why the sentinel and the wait

`tmux split-window` returns as soon as the pane EXISTS, not when its command finishes, so the
picker's exit code is otherwise unobservable. The carrier is measured, not assumed — tmux 3.6,
controlled arms:

| arm | what it tests | result |
| --- | --- | --- |
| child exits before the parent waits | is a signal with no waiter lost? | code arrives — **not lost** |
| parent already waiting | ordinary case | code arrives |
| no `wait-for` at all | known-bad control | sentinel MISSING |
| nothing ever signals | the deadlock | `timeout` returns 124 |

The consequences, each load-bearing:

- **The `timeout` is not decoration.** A pane killed, closed, or lost to a tmux server restart
  signals nothing, and a bare `tmux wait-for` then blocks forever.
- **The `timeout` bounds the wait, not the pane.** When it fires, the picker is usually still
  running — a human who walked away mid-edit. The pane is not killed, because killing it would
  destroy toggles they have not saved. But that makes the cleanup conditional: `rm -f` must not
  run while the pane is alive. Measured on the pre-fix text, with the bound shortened so the path
  was observable: the parent reported `missing` and deleted the files, the pane went on to exit
  `3` and recreate `$SENT` as litter, and `$ERR` was unlinked while the pane still held fd 9 open,
  so the picker's diagnostic was written into an unreachable inode and lost. The pane id from
  `split-window -P` is what separates the two cases, and a live pane reports `still-open` rather
  than `missing` — the difference between "I do not know" and "the pane is still there".

  What `still-open` does NOT tell you is whether anything was saved, and the temptation to read it
  that way is why this is written down. A live pane proves the pane exists, nothing more — not even
  that the picker is still running. The picker's exit and the sentinel's publication are two
  separate steps, and the pane outlives the first to perform the second, so a picker that has
  already written the config and is one `mv` away from reporting looks, from out here, exactly
  like a human who walked away mid-edit. Measured, with the window widened so it was observable
  rather than raced for: `picker-exit=still-open`, the config already on disk, and the picker
  process already gone, in one run — against a control arm reporting the same `still-open` over
  the same live pane with the picker still running. Two different states, one indistinguishable
  report. Report the pane, not the outcome, and read the live state with `--show` if it matters.

  The liveness check has to be `list-panes` with a filter, and that is measured too, because the
  obvious check is wrong. `tmux display-message -p -t "$PANE"` returns 0 for a pane that has
  already died AND for an empty `$PANE` — it silently falls back to the current pane — so it
  would report `still-open` every time, including after a failed split. A bare membership test
  against `list-panes` output has the same flaw for the empty case. The filter form returns
  nothing for a dead id, an empty id, and a bogus id alike.
- **The `timeout` is also not guaranteed to exist.** Base macOS ships no `timeout`; Homebrew's
  coreutils installs it as `gtimeout`, and someone who got tmux from Homebrew need not have
  coreutils at all. `command -v` picks whichever is present, and the `[ -n "$TMO" ]` branch drops
  the wrapper entirely when neither is — degrading to the unbounded wait rather than dying on
  `command not found`. That degradation is deliberate but it is a real loss: on such a box the
  deadlock arm above has nothing bounding it.

  That has to be a branch and not a `${TMO:+$TMO 900}` expansion, because the expansion
  field-splits. A `timeout` living under a path with a space in it — a Homebrew prefix someone
  relocated, say — splits into two words, the first of which is not a command. Measured: `rc=127`
  and the wait returns in `0.00s` instead of blocking. That is worse than having no timeout at
  all. The parent then reads a sentinel the pane has not written yet, reports `missing`, and
  `rm -f`s the files while the pane is still running — so the code is not merely lost, the pane
  is left writing into a path the parent has already torn down. Quoting `"$TMO"` in an explicit
  branch keeps it one command; both controls — a plain path, and no `timeout` at all — still
  wait the full duration.
- **The `&&` before the wait is not decoration.** A failed split returns 1, and waiting on a
  channel that no pane will ever signal is the same deadlock.
- **The picker must stay a child process.** Anything that exits the pane's own shell rather
  than returning to it skips both the `echo` and the signal. This was measured by getting it
  wrong: an early probe inlined `exit N`, wrote no sentinel, sent no signal, and hung.
- **stderr needs its own file.** `tmux wait-for` carries synchronization, never pane output,
  and the pane closes when its command ends — so an error the picker prints is gone before
  anyone reads it. Redirecting to `$ERR` is the only way the message survives the pane. The
  picker's UI goes to stdout, so this captures diagnostics and nothing else.
- **That redirect has to be pre-flighted, and opened only once.** If the pane shell cannot open
  `$ERR`, bash does not run the command at all and the status is `1` — which is exactly the code
  the table below reads as a picker selftest failure. The picker would never have started, and the
  report would name it as defective. So the pane probes `$ERR` with a no-op, then opens it once on
  fd 9 and hands the picker that descriptor with `2>&9`. Two separate opens would leave a window
  in which the probe succeeds and the launch's own open fails, putting `1` back on the wire;
  reusing the descriptor closes it. Anything that fails before the launch publishes the token
  `setup`, which is outside `0`–`4` and so surfaces as `unclassified:[setup]` — UNKNOWN, and
  pointing at the launcher rather than at the picker.

  The `:` probe stays in front of the `exec` rather than being replaced by it, because the two
  fail differently. Measured: a failed `exec 9>>` leaves bash running and reaches the `else`, but
  under dash it **exits the shell** — which would kill the pane before it writes anything. Short-
  circuiting on the probe means the ordinary unopenable-stderr case reaches `setup` on either
  shell, and only the race window can reach the `exec` at all, where the worst case degrades to a
  missing sentinel rather than to a confident `1`. Also measured: `2>&9` carries stderr to the
  file and preserves the exit status; under a forced race the one-open shape published the
  picker's real `0` where the two-open shape published `1`; and with a writable `$ERR` none of
  this changes the normal path — a save still publishes `0` and a genuine picker failure still
  publishes its own `2`.

The sentinel is read as untrusted input, not interpolated. A `cat` of it exits 0 whatever it
holds, and plenty of things that are not picker exit codes can end up there — a pane shell that
cannot find `python3` writes `127`, and anything that fails before the picker runs writes its
own code. Anything outside `0`–`4` is reported as `unclassified` rather than passed off as a
result.

The classifier alone cannot catch every partial write, which is why the sentinel is *published*
rather than written in place. A truncation to a single digit that happens to be `0`–`4` — a pane
killed after the `1` of `127` — would be indistinguishable from a complete code, and the
classifier would pass it through as a selftest failure. So the pane writes `$SENT.part` and
renames it onto `$SENT` only once the write returned. `mv` within one filesystem is `rename(2)`,
which is atomic: a reader sees the old name or the new one, never a half-built file. Every
truncation therefore leaves the fragment under `.part`, `$SENT` never appears, and the outcome
reports as `missing` — UNKNOWN, which is what it is — instead of as a confident wrong code.

The `&&` between the write and the rename is what makes that hold. If the `echo` fails — a full
`/tmp` being the realistic case — the rename does not run, and a partial `.part` is never
promoted.

## Act on the exit code

| code | meaning | do |
| --- | --- | --- |
| `0` | saved | confirm with `python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py" --show` and report the new bar |
| `4` | cancelled | say so in one line; nothing was written |
| `3` | the human pressed `v` | hand off — see below |
| `2` | environment or usage error | report the captured `picker-stderr:` block verbatim; do not paper over it |
| `1` | selftest failures | a real defect in the picker; report it, do not retry |
| `still-open` | the bounded wait expired and the pane is still alive | not a result, so do not report one. The pane's existence is all that is known — usually the human is still editing, but a save that has finished while the sentinel is still being published is indistinguishable from here. Say the pane below their session is still open and that no outcome has arrived; do not say the picker is still open, and do not say whether anything was written. Nothing was cleaned up and it finishes on its own; use `--show` if the live state matters |
| `missing` | no sentinel file and no pane — it died before it could report, or died mid-write and its fragment was never promoted off `.part` | outcome UNKNOWN — say that, and read the live state with `--show` rather than assuming either way |
| `unclassified:[…]` | sentinel exists and is complete, but holds something else | also UNKNOWN. Two values are known: `setup` means the pane could not open `$ERR` and the picker never started; `127` means the pane shell had no `python3`. Anything else is genuinely unattributed — the picker may never have run, or may have been killed part-way, which is where a shell's signal statuses like `130` (SIGINT) or `143` (SIGTERM) come from. Do not say which. Report the raw value and read the live state with `--show` |

Each block below re-states the path rather than reusing `$PICKER`. That is not redundancy —
every block runs in its own shell, so a variable set in one is unset in the next, and
`python3 $PICKER --show` would silently become `python3 --show`.

Nothing was written for any **picker exit code** other than `0`. `3` in particular leaves the
config untouched: it is a cancel that carries a reason. `2` covers a save that failed, and that
also leaves the old file intact — the picker writes through a unique temp name in the target
directory and renames over the config, so a failed save unlinks its temp and the config never
changes. Measured, with a read-only config directory: `rc=2`, the config byte-identical
afterwards, and no temp file stranded beside it.

The three carrier values are not picker exit codes and carry no such guarantee. `still-open`,
`missing` and `unclassified:[…]` all mean the same thing — the picker's exit code never reached
you — and a picker can save and then die, or save and not yet have published. Never read them as
"nothing was written". Read the live state with `--show`.

Cancel has its own code because the pane's stdout never reaches you — the exit code is the only
channel out, and if cancel shared `0` with a save you would report a save that never happened.
Do not collapse them back.

## Exit 3 — hand off to the built-in workflow

The picker is a standalone Python TUI. It cannot spawn a Claude Code agent, so it cannot launch
the built-in flow itself; it exits 3 to say the human asked for it, and acting on that is this
command's job.

Before spawning anything, capture what the built-in flow is about to overwrite:

```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('statusLine',{}).get('command','unset'))"
```

Tell the human that value, plainly: the built-in `statusline-setup` agent rewrites
`settings.json.statusLine`, and pointing it elsewhere retires this repo's `statusline.js` — the
picker, the config file, and the component registry all stop being reachable. They pressed `v`,
so proceed; just make sure they are choosing that knowingly, and keep the captured value so it
can be put back.

Then spawn the agent with `subagent_type: "statusline-setup"`, passing along whatever the human
said they wanted. This is the one place in this command where that agent is correct.

## If `$TMUX` is unset (pane path only)

Do not attempt the split — it opens somewhere the human cannot see. Offer the AskUserQuestion
path above first; if the human specifically wants the full TUI, show them the current bar so
the turn is not empty, then print the line for them to run in their own terminal, and stop:

```bash
python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py" --show
```

```bash
python3 "$HOME/ClaudeCodeStatusline/statusline_picker.py"
```

## Notes

- The component registry has a single carrier: it lives only in `statusline.js` and is fetched
  with `node statusline.js --segments`. The colour-scheme list is the same way — fetched with
  `node statusline.js --schemes`. Never hardcode either list here.
- Every save goes through `--apply` (or the pane's interactive save) — both validate against
  the registry and write atomically. Hand-writing `statusline-config.json` bypasses both and
  is never the answer to an `--apply` refusal.
- Delete `~/.claude/statusline-config.json` to restore all components in default order.
- An empty selection is a valid explicit choice — an empty bar, not an error.
