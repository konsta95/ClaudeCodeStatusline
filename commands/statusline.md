# /statusline

**Input:** $ARGUMENTS

Open the picker in a tmux pane. Always — with or without an argument, whatever the argument
says. There is no preset path and no question-first path: the picker itself is the interface,
and inside it `v` hands off to Claude Code's own built-in statusline setup.

Copy this file to `~/.claude/commands/statusline.md`. A user command shadows the built-in of
the same name, so `/statusline` reaches this instead. Set `PICKER` below to wherever you
cloned the repo.

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
CHAN="statusline-picker-$$"; SENT="/tmp/statusline-picker-$$.rc"; rm -f "$SENT"
tmux split-window -v -b -l 16 \
  "python3 $PICKER; echo \$? > $SENT; tmux wait-for -S $CHAN" \
  && timeout 900 tmux wait-for "$CHAN"
echo "picker-exit=$(cat "$SENT" 2>/dev/null || echo missing)"; rm -f "$SENT"
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

Three consequences, each load-bearing:

- **The `timeout` is not decoration.** A pane killed, closed, or lost to a tmux server restart
  signals nothing, and a bare `tmux wait-for` then blocks forever.
- **The `&&` before the wait is not decoration.** A failed split returns 1, and waiting on a
  channel that no pane will ever signal is the same deadlock.
- **The picker must stay a child process.** Anything that exits the pane's own shell rather
  than returning to it skips both the `echo` and the signal. This was measured by getting it
  wrong: an early probe inlined `exit N`, wrote no sentinel, sent no signal, and hung.

## Act on the exit code

| code | meaning | do |
| --- | --- | --- |
| `0` | saved | confirm with `python3 $PICKER --show` and report the new bar |
| `4` | cancelled | say so in one line; nothing was written |
| `3` | the human pressed `v` | hand off — see below |
| `2` | environment or usage error | report the pane's own message verbatim; do not paper over it |
| `1` | selftest failures | a real defect in the picker; report it, do not retry |
| `missing` | pane died before it could report | outcome UNKNOWN — say that, and read the live state with `--show` rather than assuming either way |

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
python3 $PICKER --show
```

```
python3 $PICKER
```

## Notes

- The component registry has a single carrier: it lives only in `statusline.js` and is fetched
  with `node statusline.js --segments`. Never hardcode a component list here.
- Delete `~/.claude/statusline-config.json` to restore all components in default order.
- An empty selection is a valid explicit choice — an empty bar, not an error.
