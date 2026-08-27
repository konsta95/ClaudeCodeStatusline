# /statusline

**Input:** $ARGUMENTS

Open the picker in a tmux pane. Always — with or without an argument, whatever the argument
says. There is no preset path and no question-first path: the picker itself is the interface,
and inside it `v` hands off to Claude Code's own built-in statusline setup.

Copy this file to `~/.claude/commands/statusline.md`. A user command shadows the built-in of
the same name, so `/statusline` reaches this instead. If you cloned the repo somewhere other
than `~/claude-code-statusline-picker`, change the path in **every** block below — each one
runs in its own shell, so none of them can inherit the path from another.

**Do not spawn the `statusline-setup` agent up front, and do not edit `settings.json`.** That
key already points at this repo's `statusline.js` and does not need to change. What changes is
the *component selection* in `~/.claude/statusline-config.json`, which the renderer re-reads on
every refresh — so a save lands on the next repaint, with no restart and no interruption to the
running session. The one path that legitimately reaches `statusline-setup` is exit code 3.

## Open it

Run this with a Bash `timeout` of `600000`. A human reads the bar, thinks, and toggles; the
default 120s tool timeout would otherwise background the call mid-edit. If it does get
backgrounded that is not a failure — the pane keeps running and the exit code still arrives in
the output file when it finishes.

```bash
PICKER="$HOME/claude-code-statusline-picker/statusline_picker.py"
CHAN="statusline-picker-$$"
SENT="/tmp/statusline-picker-$$.rc"; ERR="/tmp/statusline-picker-$$.err"
rm -f "$SENT" "$SENT.part" "$ERR"
TMO=$(command -v timeout || command -v gtimeout || true)   # macOS has neither by default
tmux split-window -v -b -l 16 \
  "if : 2>>\"$ERR\" && exec 9>>\"$ERR\"; then python3 \"$PICKER\" 2>&9; RC=\$?; else RC=setup; fi
   echo \$RC > \"$SENT.part\" && mv \"$SENT.part\" \"$SENT\"; tmux wait-for -S $CHAN" \
  && ${TMO:+$TMO 900} tmux wait-for "$CHAN"

if [ ! -f "$SENT" ]; then
  RC=missing
else
  RC=$(cat "$SENT")
  case "$RC" in 0|1|2|3|4) ;; *) RC="unclassified:[$RC]" ;; esac
fi
echo "picker-exit=$RC"
[ -s "$ERR" ] && { echo "picker-stderr:"; cat "$ERR"; }
rm -f "$SENT" "$SENT.part" "$ERR"
```

`-v -b` stacks the pane ABOVE the session and `-l 16` gives it the ~14 lines it needs; a
side-by-side `-h` split is too narrow and the preview line clips mid-word. Focus moves to the
pane, which is the point — the human types there.

Keys once it opens: `↑`/`↓` move, `space` toggles, `←`/`→` reorder within the enabled block,
`c` colours, **`v` built-in setup**, `Enter` saves, `q`/`Esc` cancels.

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

Five consequences, each load-bearing:

- **The `timeout` is not decoration.** A pane killed, closed, or lost to a tmux server restart
  signals nothing, and a bare `tmux wait-for` then blocks forever.
- **The `timeout` is also not guaranteed to exist.** Base macOS ships no `timeout`; Homebrew's
  coreutils installs it as `gtimeout`, and someone who got tmux from Homebrew need not have
  coreutils at all. `command -v` picks whichever is present, and `${TMO:+…}` drops the wrapper
  entirely when neither is — degrading to the unbounded wait rather than dying on
  `command not found`. That degradation is deliberate but it is a real loss: on such a box the
  deadlock arm above has nothing bounding it.
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
| `0` | saved | confirm with `python3 "$HOME/claude-code-statusline-picker/statusline_picker.py" --show` and report the new bar |
| `4` | cancelled | say so in one line; nothing was written |
| `3` | the human pressed `v` | hand off — see below |
| `2` | environment or usage error | report the captured `picker-stderr:` block verbatim; do not paper over it |
| `1` | selftest failures | a real defect in the picker; report it, do not retry |
| `missing` | no sentinel file — the pane died before it could report, or died mid-write and its fragment was never promoted off `.part` | outcome UNKNOWN — say that, and read the live state with `--show` rather than assuming either way |
| `unclassified:[…]` | sentinel exists and is complete, but holds something else | also UNKNOWN. Two values are known: `setup` means the pane could not open `$ERR` and the picker never started; `127` means the pane shell had no `python3`. Anything else is genuinely unattributed — the picker may never have run, or may have been killed part-way, which is where a shell's signal statuses like `130` (SIGINT) or `143` (SIGTERM) come from. Do not say which. Report the raw value and read the live state with `--show` |

Each block below re-states the path rather than reusing `$PICKER`. That is not redundancy —
every block runs in its own shell, so a variable set in one is unset in the next, and
`python3 $PICKER --show` would silently become `python3 --show`.

Nothing was written for any code other than `0`. `3` in particular leaves the config untouched:
it is a cancel that carries a reason.

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

## If `$TMUX` is unset

Do not attempt the split — it opens somewhere the human cannot see. Show them the current bar
so the turn is not empty, then print the line for them to run in their own terminal, and stop:

```bash
python3 "$HOME/claude-code-statusline-picker/statusline_picker.py" --show
```

```
python3 "$HOME/claude-code-statusline-picker/statusline_picker.py"
```

## Notes

- The component registry has a single carrier: it lives only in `statusline.js` and is fetched
  with `node statusline.js --segments`. Never hardcode a component list here.
- Delete `~/.claude/statusline-config.json` to restore all components in default order.
- An empty selection is a valid explicit choice — an empty bar, not an error.
