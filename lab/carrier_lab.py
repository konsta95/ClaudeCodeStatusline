"""Live tmux arms for the carrier documented in commands/statusline.md.

`lab/exit_contract_lab.py` covers what the PICKER does. This covers what the
CARRIER does with it: the tmux pane, the sentinel file, the bounded wait, and
the classification of what comes back. Run it from anywhere; it needs tmux.

    python3 lab/carrier_lab.py

Exit codes match the picker's own contract: 0 all arms as expected, 1 an arm
disagreed (a real defect -- read the arm, do not retry), 2 the environment
cannot run the lab.

The carrier under test is EXTRACTED from the markdown rather than retyped, so an
arm that passes says something about the shipped text and not about a copy of it
that drifted. Both pre-fix controls are built by MUTATING that same extracted
text -- reverting one region each -- rather than by reading an old commit out of
git. A history baseline stops being a known-bad the moment the fix merges; a
mutation of current source does not.

Two independent mutants, because the fixes live in two regions:
  PRE_PANE  reverts the pane command  -> the setup guard and the quoting
  PRE_TAIL  reverts the parent's tail -> pane liveness before cleanup
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOC = os.path.join(REPO, "commands", "statusline.md")
REAL = os.path.join(REPO, "statusline_picker.py")
WORK = tempfile.mkdtemp(prefix="carrier-lab.")

md = open(DOC).read()
m = re.search(r"```bash\n(PICKER=.*?)```", md, re.S)
if not m:
    print("lab error: no carrier block in %s -- the lab reads the shipped text\n"
          "rather than a copy, so there is nothing to fall back to." % DOC,
          file=sys.stderr)
    sys.exit(2)
FIXED = m.group(1)


def mutate(text, anchor, replacement, what):
    """Revert one region of the extracted carrier, or refuse.

    A missing anchor means the doc moved and this control no longer reverts
    what it names. That is a stale FIXTURE, not a failing arm, so it exits 2
    rather than 1: relaxing the match here would leave a control that always
    passes and proves nothing.
    """
    if anchor not in text:
        print("lab error: mutation anchor missing (%s) -- the carrier changed "
              "shape.\nUpdate the anchor; do NOT relax the match, or the control\n"
              "silently stops being a control.\n\n%s" % (what, text),
              file=sys.stderr)
        sys.exit(2)
    return text.replace(anchor, replacement)


PANE_NEW = ('  "if : 2>>\\"$ERR\\" && exec 9>>\\"$ERR\\"; then python3 \\"$PICKER\\" 2>&9; RC=\\$?; else RC=setup; fi\n'
            '   echo \\$RC > \\"$SENT.part\\" && mv \\"$SENT.part\\" \\"$SENT\\"; tmux wait-for -S $CHAN") \\\n')
PANE_OLD = ('  "python3 $PICKER 2>$ERR; echo \\$? > $SENT.part && mv $SENT.part $SENT;'
            ' tmux wait-for -S $CHAN") \\\n')

TAIL_NEW = ('if [ -f "$SENT" ]; then\n'
            '  RC=$(cat "$SENT")\n'
            '  case "$RC" in 0|1|2|3|4) ;; *) RC="unclassified:[$RC]" ;; esac\n'
            'elif [ -n "$(tmux list-panes -a -f "#{==:#{pane_id},$PANE}" -F \'#{pane_id}\' 2>/dev/null)" ]; then\n'
            '  RC=still-open\n'
            'else\n'
            '  RC=missing\n'
            'fi\n')
TAIL_OLD = ('if [ ! -f "$SENT" ]; then\n'
            '  RC=missing\n'
            'else\n'
            '  RC=$(cat "$SENT")\n'
            '  case "$RC" in 0|1|2|3|4) ;; *) RC="unclassified:[$RC]" ;; esac\n'
            'fi\n')
CLEAN_NEW = '[ "$RC" = still-open ] || rm -f "$SENT" "$SENT.part" "$ERR"'
CLEAN_OLD = 'rm -f "$SENT" "$SENT.part" "$ERR"'

PRE_PANE = mutate(FIXED, PANE_NEW, PANE_OLD, "pane command")
PRE_TAIL = mutate(mutate(FIXED, TAIL_NEW, TAIL_OLD, "parent tail"),
                  CLEAN_NEW, CLEAN_OLD, "conditional cleanup")


def sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


def stub(name, code, msg=None, sleep=0, pidfile=None):
    p = os.path.join(WORK, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = "import os, sys, time\n"
    if pidfile:
        # Written FIRST, so the probe can distinguish "still running" from
        # "never started".
        body += "open(%r, 'w').write(str(os.getpid()))\n" % pidfile
    if sleep:
        body += "time.sleep(%d)\n" % sleep
    if msg:
        body += "sys.stderr.write(%r)\nsys.stderr.flush()\n" % msg
    body += "sys.exit(%d)\n" % code
    open(p, "w").write(body)
    return p


def stub_saving(name, marker, pidfile=None):
    """A picker that SAVES and exits 0 -- the marker file stands in for
    ~/.claude/statusline-config.json."""
    p = os.path.join(WORK, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = "import os, sys\n"
    if pidfile:
        body += "open(%r, 'w').write(str(os.getpid()))\n" % pidfile
    body += "open(%r, 'w').write('SAVED')\nsys.exit(0)\n" % marker
    open(p, "w").write(body)
    return p


def enotdir_err():
    f = os.path.join(WORK, "blocker")
    open(f, "w").close()
    return os.path.join(f, "x.err")


def arm(label, carrier, picker, err=None, sendkeys=None, env=None,
        tmo=None, report_files=False, widen=None, marker=None, pidfile=None):
    d = tempfile.mkdtemp(dir=WORK)
    body = re.sub(r'^PICKER=.*$', lambda _: 'PICKER=%s' % sh(picker),
                  carrier, count=1, flags=re.M)
    if err is not None:
        body = body.replace('ERR="/tmp/statusline-picker-$$.err"', 'ERR=%s' % sh(err))
    if tmo is not None:
        body = mutate(body, '"$TMO" 900 tmux', '"$TMO" %d tmux' % tmo, "timeout bound")
    if widen is not None:
        # INSTRUMENTATION, not a behaviour change. The picker's exit and the
        # sentinel's publication are already two separate steps, so a window
        # between them exists by construction; this only makes it long enough
        # to observe. No state is created that the shipped text does not have.
        body = mutate(body, '   echo \\$RC > \\"$SENT.part\\"',
                      '   sleep %d; echo \\$RC > \\"$SENT.part\\"' % widen,
                      "sentinel publish step")
    pre = "set +e\n"
    for k, v in (env or {}).items():
        pre += "export %s=%s\n" % (k, sh(v))
    if sendkeys:
        pre += "( sleep 5; tmux send-keys %s ) &\n" % sendkeys
    post = ""
    if report_files:
        # The observable for N6: did the parent delete the files out from under
        # a pane that is still running?
        post += ('\nif [ -f "$ERR" ]; then echo ERR_KEPT=yes; else echo ERR_KEPT=no; fi\n'
                 'if tmux list-panes -a -f "#{==:#{pane_id},$PANE}" -F "#{pane_id}" '
                 '| read -r _; then echo PANE_ALIVE=yes; else echo PANE_ALIVE=no; fi\n')
    if marker:
        post += ('if [ -f %s ]; then echo CONFIG_WRITTEN=yes; else echo CONFIG_WRITTEN=no; fi\n'
                 % sh(marker))
    if pidfile:
        # Is the PICKER still running at the moment the carrier reported? A live
        # pane is not the same fact. Matched on argv rather than on the pid
        # alone, so a reused pid cannot read as "still running", and on the
        # exact picker path rather than a pattern, because the PANE's own shell
        # carries that path in its command line too and would match a looser
        # test long after the picker is gone.
        post += ('PP=$(cat %s 2>/dev/null)\n'
                 'if [ -n "$PP" ] && [ -r "/proc/$PP/cmdline" ] && '
                 'tr "\\0" "\\n" < "/proc/$PP/cmdline" | command grep -qxF "$PICKER"; then\n'
                 '  echo PICKER_RUNNING=yes\nelse\n  echo PICKER_RUNNING=no\nfi\n'
                 % sh(pidfile))
    if report_files:
        post += 'rm -f "$SENT" "$SENT.part" "$ERR"\n'
    script = os.path.join(d, "arm.sh")
    open(script, "w").write(pre + body + post + '\ntouch "%s/done"\n' % d)
    # The pid is in the name because the next line KILLS the session before
    # creating it. A fixed name would have one concurrent run tear down
    # another's pane mid-arm and report the failure against the wrong lab.
    sess = "carrier-lab-%d-%s" % (os.getpid(), label)
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-x", "200", "-y", "50",
                    "bash %s > %s/out 2>&1" % (sh(script), d)], check=True)
    for _ in range(600):
        if os.path.exists(os.path.join(d, "done")):
            break
        time.sleep(0.2)
    else:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        return "(TIMED OUT)"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    return open(os.path.join(d, "out")).read().strip()


def show(label, want, got, negate=False):
    first = got.splitlines()[0] if got else "(no output)"
    ok = all((w not in got) if negate else (w in got)
             for w in (want if isinstance(want, list) else [want]))
    print("  %-46s %s" % (label, "OK" if ok else "*** MISMATCH ***"))
    print("      want %-9s: %r" % ("ABSENT" if negate else "contains", want))
    print("      got           : %s" % first)
    for line in got.splitlines()[1:]:
        print("                      %s" % line)
    return ok


def main():
    if not shutil.which("tmux"):
        print("lab error: tmux is required -- every arm opens a real pane, and a "
              "simulated one\nwould test the simulation.", file=sys.stderr)
        return 2

    results = []
    print("FIXED carrier")
    results.append(show("A  stub exit 0",
                        "picker-exit=0",
                        arm("a", FIXED, stub("plain/p0.py", 0))))
    results.append(show("B  stub exit 2, writes stderr",
                        "picker-exit=2",
                        arm("b", FIXED, stub("plain/p2.py", 2, "error: renderer not found\n"))))
    results.append(show("C  stub exit 3, path with a SPACE",
                        "picker-exit=3",
                        arm("c", FIXED, stub("has space/p3.py", 3))))
    results.append(show("D  stderr target returns ENOTDIR",
                        "picker-exit=unclassified:[setup]",
                        arm("d", FIXED, stub("plain/p0b.py", 0), err=enotdir_err())))
    results.append(show("E  REAL picker, q pressed -> cancel",
                        "picker-exit=4",
                        arm("e", FIXED, REAL, sendkeys="q",
                            env={"STATUSLINE_CONFIG": os.path.join(WORK, "throwaway.json")})))
    slow_pid = os.path.join(WORK, "slow.pid")
    results.append(show("F  bound fires while the pane still lives",
                        ["picker-exit=still-open", "ERR_KEPT=yes", "PANE_ALIVE=yes",
                         "PICKER_RUNNING=yes"],
                        arm("f", FIXED, stub("plain/slow.py", 3, "DIAGNOSTIC\n", sleep=8,
                                             pidfile=slow_pid),
                            tmo=2, report_files=True, pidfile=slow_pid)))

    saved = os.path.join(WORK, "saved-config.json")
    saver_pid = os.path.join(WORK, "saver.pid")
    results.append(show("G  saved+exited, sentinel not yet published",
                        ["picker-exit=still-open", "PANE_ALIVE=yes", "CONFIG_WRITTEN=yes",
                         "PICKER_RUNNING=no"],
                        arm("g", FIXED, stub_saving("plain/saver.py", saved, pidfile=saver_pid),
                            tmo=2, widen=4, marker=saved, report_files=True,
                            pidfile=saver_pid)))
    print("      ^ G is a PROSE known-bad, not a code one. The carrier behaves")
    print("        correctly here; what it refutes is the claim that still-open")
    print("        means the human is still editing, that the picker is still open,")
    print("        and that nothing has been written. F and G are the two polarities")
    print("        of the same probe: same still-open report, same live pane, and the")
    print("        picker running in one and already gone in the other -- so a live")
    print("        pane does not carry the picker's state.")

    print("\nPRE-FIX controls -- each reverts ONE region of the text above")
    results.append(show("C' spaced path: the code must NOT survive",
                        "picker-exit=3",
                        arm("cp", PRE_PANE, stub("has space/p3b.py", 3)), negate=True))
    results.append(show("D' ENOTDIR: publishes a confident, wrong 1",
                        "picker-exit=1",
                        arm("dp", PRE_PANE, stub("plain/p0c.py", 0), err=enotdir_err())))
    results.append(show("F' old tail: reports missing, deletes $ERR",
                        ["picker-exit=missing", "ERR_KEPT=no", "PANE_ALIVE=yes"],
                        arm("fp", PRE_TAIL, stub("plain/slow2.py", 3, "DIAGNOSTIC\n", sleep=8),
                            tmo=2, report_files=True)))

    print("\nA-G prove the fixed carrier does the right thing. C', D' and F' prove the")
    print("arms discriminate: pre-fix, C loses the exit code, D publishes 1, and F")
    print("reports missing while deleting the live pane's stderr file.")
    bad = len([r for r in results if not r])
    print("---")
    print("CARRIER LAB %s (%d arms disagreed)"
          % ("PASSED" if not bad else "FAILED", bad))
    if bad:
        # Left in place on purpose: the arm's script, its captured output and
        # the sentinel files are the evidence for what disagreed.
        print("working tree kept for inspection: %s" % WORK)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
