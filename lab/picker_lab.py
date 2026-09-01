#!/usr/bin/env python3
"""Adversarial lab for statusline_picker.py.

Runs the REAL picker binary — not an import, not a copy — under sandboxed
HOME directories, driving the interactive mode through a pty pair created with
os.openpty so the parent keeps the slave fd and can read the terminal's termios
state before, during, and after the child runs. Every case prints one
EVIDENCE line; the findings report is compiled from these lines. A case that
cannot run prints LAB-ERROR and the run exits nonzero — a broken instrument
must not read as a clean sweep.

Layout under SCRATCH/picker-lab/: one home-<case>/ per case, tmp/ as TMPDIR
for every child so mkdtemp leaks land where they can be counted.

Role: REGRESSION harness. A check earns the right to be called evidence only
once it has been observed FAILING against a known-bad input, so the pre-fix run
is preserved beside this file at lab/picker-lab-prefix-known-bad.log. That log
is the observed-failing record for every comparator whose verdict flips to ok
in lab/picker-lab-postfix.log. Cases N12 / P5 / P6 were reworked to assert the
fixed contract — unique-temp saves through the real save_config, the timed-out
preview notice, autowrap-off framing — so their original known-bad observations
live only in the frozen pre-fix log.

One exception to "not an import": case N12 imports the picker module directly to
hammer save_config from two threads. The concurrent-save collision window is
microseconds wide and cannot be steered from outside the process, so that case
buys determinism by giving up black-box isolation. Every other case runs the
binary.

Requires a POSIX pty. Run it with `python3 lab/picker_lab.py` from the
repository root; it needs no arguments and writes only under SCRATCH.
"""
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

# Paths resolve relative to this file's repository by default, so the lab runs from a
# fresh clone with no configuration. Each is overridable by environment variable for
# running the lab against an installed picker rather than the one in the tree.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRATCH = os.environ.get("PICKER_LAB_SCRATCH") or os.path.join(_REPO, ".lab-scratch")
LAB = os.path.join(SCRATCH, "picker-lab")
LABTMP = os.path.join(LAB, "tmp")
PICKER = os.environ.get("PICKER_LAB_PICKER") or os.path.join(_REPO, "statusline_picker.py")
REAL_JS = os.environ.get("PICKER_LAB_RENDERER") or os.path.join(_REPO, "statusline.js")
PY = sys.executable or "python3"

lab_errors = []


def evidence(case, verdict, detail):
    print("EVIDENCE %-28s %-8s %s" % (case, verdict, detail))


def lab_error(case, detail):
    lab_errors.append(case)
    print("LAB-ERROR %-27s %s" % (case, detail))


def make_home(name):
    """Fresh sandbox HOME with the real renderer copied in (copied, not
    symlinked: some cases replace the file mid-run)."""
    home = os.path.join(LAB, "home-" + name)
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(os.path.join(home, ".claude", "tools"))
    shutil.copy2(REAL_JS, os.path.join(home, ".claude", "statusline.js"))
    return home


def child_env(home):
    env = dict(os.environ)
    env["HOME"] = home
    env["TMPDIR"] = LABTMP
    env["TERM"] = "xterm-256color"
    env.pop("STATUSLINE_CONFIG", None)
    # The renderer's payload probe is opt-in. "1" selects the default location
    # under $HOME rather than a fixed path, so the sandbox HOME redirects the
    # probe with it -- which is what N1-leak, N2 and P8 actually measure. A
    # literal path here would point every case at one file and make those three
    # cases report on the lab's own env instead of the picker's behaviour.
    env["STATUSLINE_PAYLOAD_DUMP"] = "1"
    # Point the picker at the renderer copy INSIDE the sandbox home. The picker
    # defaults to the renderer beside its own file so a fresh clone is self-
    # contained -- but the sabotage cases (N6, N13, P5) work by overwriting the
    # copy in the sandbox, and against the default they would exercise the intact
    # repo renderer and report ok while testing nothing.
    env["STATUSLINE_JS"] = os.path.join(home, ".claude", "statusline.js")
    return env


def run_show(home, extra_args=(), timeout=30):
    """Non-interactive picker invocation through pipes."""
    return subprocess.run(
        [PY, PICKER] + list(extra_args),
        capture_output=True,
        env=child_env(home),
        timeout=timeout,
    )


class PtyPicker:
    """Picker under a real pty. Parent keeps BOTH fds: master to talk,
    slave to measure termios (ECHO/ICANON are cleared by tty.setraw and
    must be back after exit)."""

    def __init__(self, home, args=(), cols=120, rows=40):
        self.master, self.slave = os.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        self.buf = b""
        self.status = None
        self.pid = os.fork()
        if self.pid == 0:
            os.setsid()
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
            os.dup2(self.slave, 0)
            os.dup2(self.slave, 1)
            os.dup2(self.slave, 2)
            os.execve(PY, [PY, PICKER] + list(args), child_env(home))
            os._exit(127)

    def read_until(self, needle, deadline_s):
        """Accumulate master output until needle appears in the NEW bytes of
        this call, or the deadline passes. Returns the accumulated bytes."""
        got = b""
        end = time.monotonic() + deadline_s
        while needle not in got and time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
        self.buf += got
        return got

    def drain(self, quiet_s=0.3, max_s=3.0):
        """Read until the line goes quiet for quiet_s."""
        got = b""
        end = time.monotonic() + max_s
        last = time.monotonic()
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
                last = time.monotonic()
            elif time.monotonic() - last > quiet_s:
                break
        self.buf += got
        return got

    def send(self, data):
        os.write(self.master, data)

    def _poll(self):
        """One WNOHANG reap; remembers the status so later calls stay valid."""
        if self.status is not None:
            return self.status
        try:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.status = -1
            return self.status
        if pid:
            self.status = st
        return self.status

    def alive(self):
        return self._poll() is None

    def wait(self, timeout_s):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            st = self._poll()
            if st is not None:
                return st
            time.sleep(0.05)
        return None

    def termios_restored(self):
        """ECHO and ICANON back on the slave = raw mode was undone."""
        now = termios.tcgetattr(self.slave)
        lflag = now[3]
        return bool(lflag & termios.ECHO) and bool(lflag & termios.ICANON)

    def kill_close(self):
        if self.alive():
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        os.close(self.master)
        os.close(self.slave)


def exit_code(st):
    """Decode PtyPicker.wait()'s RAW waitpid status into the child's exit
    code, or None when the wait timed out (None), the child was already
    reaped (-1), or it died to a signal. Cases assert against the picker's
    documented exit CONTRACT (save=0, handoff=3, cancel=4); comparing the
    raw status against a bare code is how P5/P9 sat FINDING from the
    initial commit -- exit(4) arrives as 1024 -- and routing every case
    through this decoder also stops a signal death from masquerading as a
    contract code."""
    if st is None or st < 0 or not os.WIFEXITED(st):
        return None
    return os.WEXITSTATUS(st)


def canonical_probe(home):
    """Have the RENDERER itself write the probe file, so its bytes are exactly
    what a live Claude Code refresh leaves behind (same dump code path)."""
    payload = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "cwd": home,
        "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
        "effort": {"level": "max"},
        "version": "9.9.9",
        "cost": {"total_cost_usd": 2.5},
        "context_window": {"total_input_tokens": 10000,
                           "context_window_size": 1000000,
                           "used_percentage": 1},
        "rate_limits": {"seven_day": {"used_percentage": 5, "resets_at": 0}},
        "workspace": {"current_dir": home},
    }
    subprocess.run(
        ["node", os.path.join(home, ".claude", "statusline.js")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=child_env(home),
        timeout=30,
        check=True,
    )
    probe = os.path.join(home, ".claude", "statusline-payload-last.json")
    if not os.path.exists(probe):
        raise RuntimeError("renderer did not write the probe")
    return probe


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def leak_count():
    return len([d for d in os.listdir(LABTMP) if d.startswith("statusline-preview-")])


HANG_RENDER_JS = """\
// --segments answers instantly; a render call never exits once FLAG exists.
// FLAG is an ABSOLUTE path: fixture-mode previews redirect $HOME to a mkdtemp
// sandbox, so a $HOME-relative flag would never be seen by the renderer.
if (process.argv.includes('--segments')) {
  process.stdout.write(JSON.stringify([{id:'model',label:'Model'},{id:'cost',label:'Cost'}]));
  process.exit(0);
}
const fs = require('fs');
if (fs.existsSync('%FLAG%')) {
  setInterval(() => {}, 1000000); // hold the process open forever
} else {
  process.stdin.resume();
  process.stdin.on('end', () => { process.stdout.write('ok'); process.exit(0); });
}
"""

GARBAGE_SEGMENTS_JS = """\
// exits 0 but prints non-JSON on --segments
process.stdout.write('hello, not json');
process.exit(0);
"""

HANG_SEGMENTS_JS = """\
// never answers anything, including --segments
setInterval(() => {}, 1000000);
"""


def main():
    shutil.rmtree(LAB, ignore_errors=True)
    os.makedirs(LABTMP)

    # ---- N1: fixture-mode --show — sandbox dir leak + fixture preview artifacts
    home = make_home("n1")
    before = leak_count()
    out = run_show(home, ["--show"])
    after = leak_count()
    txt = out.stdout.decode("utf-8", "replace")
    evidence("N1-leak", "FINDING" if after > before else "ok",
             "mkdtemp sandbox dirs in TMPDIR before=%d after=%d rc=%d" % (before, after, out.returncode))
    # The renderer locates the installed binary on PATH, and PATH -- unlike $HOME --
    # is NOT redirected by the sandbox. So the restart hint is EXPECTED here whenever
    # a claude binary is discoverable: the fixture payload claims version 0.0.0, which
    # differs from any real install. With no install on PATH there is nothing to
    # compare against and no hint. Assert that correspondence rather than the bare
    # presence of the word, which only ever measured which box the lab ran on.
    claude_on_path = shutil.which("claude") is not None
    hint = [l for l in txt.splitlines() if "restart" in l]
    evidence("N1-restart-artifact", "ok" if bool(hint) == claude_on_path else "FINDING",
             "claude-on-PATH=%s restart-hint=%s" % (claude_on_path, bool(hint)))

    # ---- N2: live-probe --show — is the probe rewrite really byte-identical?
    home = make_home("n2")
    probe = canonical_probe(home)
    h_before = sha(probe)
    out = run_show(home, ["--show"])
    h_after = sha(probe)
    evidence("N2-probe-noop", "ok" if h_before == h_after else "FINDING",
             "probe sha before==after: %s rc=%d" % (h_before == h_after, out.returncode))

    # ---- N3/N4: empty and corrupt probe files — fallback or error preview?
    for case, content in (("N3-empty-probe", b""), ("N4-corrupt-probe", b"{half a json")):
        home = make_home(case.lower())
        with open(os.path.join(home, ".claude", "statusline-payload-last.json"), "wb") as fh:
            fh.write(content)
        out = run_show(home, ["--show"])
        txt = out.stdout.decode("utf-8", "replace")
        errline = [l for l in txt.splitlines() if "statusline error" in l]
        evidence(case, "FINDING" if errline else "ok",
                 "rc=%d preview=%r" % (out.returncode, errline or txt.splitlines()[-1:]))

    # ---- N5: --js points at a non-renderer file — clean error or traceback?
    home = make_home("n5")
    garbage = os.path.join(home, "notjs.txt")
    with open(garbage, "w") as fh:
        fh.write("this is not a node script {")
    out = run_show(home, ["--show", "--js", garbage])
    tb = b"Traceback" in out.stderr
    evidence("N5-garbage-js", "FINDING" if tb else "ok",
             "rc=%d traceback=%s stderr-tail=%r" % (out.returncode, tb, out.stderr.decode()[-160:].replace("\n", " ")))

    # ---- N6: renderer hangs on --segments — what does the 10s timeout produce?
    home = make_home("n6")
    with open(os.path.join(home, ".claude", "statusline.js"), "w") as fh:
        fh.write(HANG_SEGMENTS_JS)
    t0 = time.monotonic()
    out = run_show(home, ["--show"], timeout=30)
    dt = time.monotonic() - t0
    tb = b"Traceback" in out.stderr
    evidence("N6-hung-segments", "FINDING" if tb else "ok",
             "rc=%d after %.1fs traceback=%s tail=%r" % (out.returncode, dt, tb, out.stderr.decode()[-120:].replace("\n", " ")))

    # ---- N7: exit-code contract on guarded paths
    home = make_home("n7")
    missing = run_show(home, ["--show", "--js", os.path.join(home, "absent.js")])
    evidence("N7-missing-js", "ok" if missing.returncode == 2 and b"Traceback" not in missing.stderr else "FINDING",
             "rc=%d stderr=%r" % (missing.returncode, missing.stderr.decode().strip()[:90]))
    nottty = subprocess.run([PY, PICKER], capture_output=True, env=child_env(home), timeout=30)
    evidence("N7-no-tty", "ok" if nottty.returncode == 2 and b"Traceback" not in nottty.stderr else "FINDING",
             "rc=%d stderr=%r" % (nottty.returncode, nottty.stderr.decode().strip()[:90]))
    env = child_env(home)
    env["PATH"] = "/nonexistent"
    nonode = subprocess.run([PY, PICKER, "--show"], capture_output=True, env=env, timeout=30)
    evidence("N7-no-node", "ok" if nonode.returncode == 2 and b"Traceback" not in nonode.stderr else "FINDING",
             "rc=%d stderr=%r" % (nonode.returncode, nonode.stderr.decode().strip()[:90]))

    # ---- N8: config file is a directory / unreadable — defaults, no crash
    home = make_home("n8")
    cfgdir = os.path.join(home, ".claude", "statusline-config.json")
    os.makedirs(cfgdir)
    out = run_show(home, ["--show"])
    evidence("N8-config-dir", "ok" if out.returncode == 0 and b"Traceback" not in out.stderr else "FINDING",
             "rc=%d stderr=%r" % (out.returncode, out.stderr.decode().strip()[:120]))

    # ---- N9: the shipped selftest, in a sandbox
    home = make_home("n9")
    out = run_show(home, ["--selftest"])
    passed = b"SELFTEST PASSED" in out.stdout
    evidence("N9-selftest", "ok" if passed and out.returncode == 0 else "FINDING",
             "rc=%d tail=%r" % (out.returncode, out.stdout.decode().splitlines()[-1:]))

    # ---- N10: colors:false --show — is the output actually ANSI-free?
    home = make_home("n10")
    with open(os.path.join(home, ".claude", "statusline-config.json"), "w") as fh:
        json.dump({"items": ["model", "session"], "colors": False}, fh)
    canonical_probe(home)
    out = run_show(home, ["--show"])
    esc_count = out.stdout.count(b"\x1b[")
    evidence("N10-colorsoff-ansi", "FINDING" if esc_count else "ok",
             "escape sequences in --show output with colors:false: %d" % esc_count)

    # ---- N11: EXPLICIT --payload pointing at a missing file — error or silent fixture?
    home = make_home("n11")
    out = run_show(home, ["--show", "--payload", "/nonexistent/payload.json"])
    fixture_used = b"00000000-fixture" in out.stdout
    mentioned = b"nonexistent" in out.stdout + out.stderr
    evidence("N11-payload-typo", "FINDING" if fixture_used and not mentioned else "ok",
             "rc=%d fixture-preview=%s path-mentioned-anywhere=%s" % (out.returncode, fixture_used, mentioned))

    # ---- N12: two concurrent savers + a continuous validating reader over the
    # REAL save_config, imported in-process — the one case that must exercise
    # the function body rather than the binary, because the collision window is
    # microseconds and cannot be steered from outside. The old fixed-name .tmp
    # defect was observed deterministically via the syscall replay in the
    # frozen pre-fix log (corrupt merged config + FileNotFoundError for the
    # losing writer); under that code this hammer crashes or corrupts too.
    # The unique-temp contract: every observed state parses, no writer
    # crashes, no temp residue.
    sys.path.insert(0, os.path.dirname(PICKER))
    import threading

    import statusline_picker as sp
    cfg12 = os.path.join(LAB, "n12-config.json")
    sp.save_config(cfg12, ["model"], True)
    crashes = []

    def hammer(items):
        try:
            for _ in range(150):
                sp.save_config(cfg12, items, True)
        except BaseException as exc:  # noqa: BLE001 - the crash IS the finding
            crashes.append(repr(exc))

    wa = threading.Thread(target=hammer, args=(["model", "context"],))
    wb = threading.Thread(target=hammer, args=(["cost"],))
    corrupt_reads = 0
    total_reads = 0
    wa.start()
    wb.start()
    while (wa.is_alive() or wb.is_alive()) and total_reads < 200000:
        try:
            json.load(open(cfg12))
        except (ValueError, OSError):  # partial bytes or a missing file both break readers
            corrupt_reads += 1
        total_reads += 1
    wa.join()
    wb.join()
    residue = [f for f in os.listdir(LAB)
               if f.startswith("n12-config.json.") and f.endswith(".tmp")]
    evidence("N12-tmp-collision", "FINDING" if corrupt_reads or crashes or residue else "ok",
             "reader saw %d/%d corrupt-or-missing states, writer-crashes=%s, tmp-residue=%d"
             % (corrupt_reads, total_reads, crashes or "none", len(residue)))

    # ---- N13: --segments exits 0 but prints non-JSON — clean error or traceback?
    home = make_home("n13")
    with open(os.path.join(home, ".claude", "statusline.js"), "w") as fh:
        fh.write(GARBAGE_SEGMENTS_JS)
    out = run_show(home, ["--show"])
    tb = b"Traceback" in out.stderr
    evidence("N13-garbage-registry", "FINDING" if tb else "ok",
             "rc=%d traceback=%s tail=%r" % (out.returncode, tb, out.stderr.decode()[-100:].replace("\n", " ")))

    # ---- N1b: what does the fixture preview's version segment actually show?
    # Informational companion to N1-restart-artifact: that case asserts whether the
    # skew ARROW appears, this one records the rendered segment verbatim, so a change
    # in the version segment's shape leaves a trace even when the arrow verdict holds.
    home = make_home("n1b")
    out = run_show(home, ["--show"])
    plain = re.sub(rb"\x1b\[[0-9;]*m", b"", out.stdout)
    vseg = [w for w in plain.split(b"|") if b"v0.0.0" in w]
    evidence("N1b-fixture-version", "ok",
             "fixture preview version segment renders as %r" % (vseg or plain.splitlines()[-1:]))

    # ---- P0: key pressed the instant the first frame lands — before raw-mode
    # entry. tty.setraw defaults to TCSAFLUSH, which DISCARDS pending input, so
    # a fast keypress in the launch window may be silently dropped.
    # Verdict is behavioral: the q worked iff "cancelled" printed AND the
    # picker exited within grace. alive() the instant the needle lands is NOT
    # a verdict — it races normal teardown (measured 2026-08-25, p0_artifact.py:
    # 10/10 caught keystrokes flagged dropped by the instant-alive check; the
    # revised verdict still flips FINDING on the HEAD TCSAFLUSH reader).
    home = make_home("p0")
    p = PtyPicker(home)
    first = p.read_until(b"statusline picker", 15)
    p.send(b"q")             # immediately after the frame, no settling delay
    got = p.read_until(b"cancelled", 3)
    cancelled = b"cancelled" in got
    status = p.wait(5)
    dropped = (not cancelled) or (status is None)
    evidence("P0-launch-flush", "FINDING" if dropped else "ok",
             "first-frame=%s cancelled=%s exit=%s dropped=%s"
             % (b"statusline picker" in first, cancelled, status, dropped))
    p.kill_close()

    # ---- P1: pty baseline — first frame with no keypress, q cancels, termios back
    home = make_home("p1")
    p = PtyPicker(home)
    first = p.read_until(b"statusline picker", 15)
    got_frame = b"statusline picker" in first
    p.drain()                # let the picker reach its raw-mode key read
    p.send(b"q")
    p.read_until(b"cancelled", 10)
    status = p.wait(5)
    restored = p.termios_restored()
    evidence("P1-baseline", "ok" if got_frame and status is not None and restored else "FINDING",
             "first-frame=%s exit=%s termios-restored=%s" % (got_frame, status, restored))
    p.kill_close()

    # ---- P2: bare ESC — does the reader block, and does it swallow the next key?
    home = make_home("p2")
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 15)
    p.drain()
    p.send(b"\x1b")          # bare ESC: reader now waits for a second byte
    time.sleep(0.6)
    alive_after_esc = p.alive()
    p.send(b"q")             # arrives as the SECOND byte of the escape read
    time.sleep(0.6)
    swallowed = p.alive()    # a swallowed q leaves the picker running
    p.send(b"q")             # a real quit afterwards
    p.read_until(b"cancelled", 10)
    status = p.wait(5)
    evidence("P2-esc-swallow", "FINDING" if swallowed else "ok",
             "alive-after-ESC=%s alive-after-ESC+q=%s (q swallowed) final-exit=%s"
             % (alive_after_esc, swallowed, status))
    p.kill_close()

    # ---- P3: ctrl-right (ESC [ 1 ; 5 C) — trailing C must NOT toggle colors
    home = make_home("p3")
    p = PtyPicker(home)
    p.read_until(b"colors: on", 15)
    p.drain()
    p.send(b"\x1b[1;5C")
    # run_picker redraws on EVERY token, including the ignored "other" from
    # ESC-[-1 — so drain to quiet and inspect ALL frames, not just the first.
    frame = p.drain(quiet_s=0.6, max_s=6.0)
    toggled = b"colors: off" in frame
    p.send(b"q")
    p.wait(5)
    evidence("P3-ctrl-arrow", "FINDING" if toggled else "ok",
             "ctrl-right flipped the colors line in some frame: %s" % toggled)
    p.kill_close()

    # ---- P4: Home key (ESC [ H) — control: must be inert
    home = make_home("p4")
    p = PtyPicker(home)
    p.read_until(b"colors: on", 15)
    p.drain()
    p.send(b"\x1b[H")
    time.sleep(0.5)
    frame = p.drain()
    changed = b"colors: off" in frame
    still = p.alive()
    p.send(b"q")
    p.wait(5)
    evidence("P4-home-key", "ok" if still and not changed else "FINDING",
             "alive=%s colors-flipped=%s" % (still, changed))
    p.kill_close()

    # ---- P5: renderer hangs mid-session — the sweep contract: a bracketed
    # timeout notice in the redraw ~PREVIEW_TIMEOUT later, picker survives,
    # no traceback, clean quit afterwards. (Pre-fix: TimeoutExpired staircased
    # in raw mode, exit 256 — frozen log.)
    home = make_home("p5")
    with open(os.path.join(home, ".claude", "statusline.js"), "w") as fh:
        fh.write(HANG_RENDER_JS.replace("%FLAG%", os.path.join(home, "flag")))
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 20)
    p.drain()
    with open(os.path.join(home, "flag"), "w") as fh:
        fh.write("1")
    t0 = time.monotonic()
    p.send(b" ")             # toggle -> dirty -> previewer -> hung renderer
    got = p.read_until(b"[preview timed out", 15)
    dt = time.monotonic() - t0
    noticed = b"[preview timed out" in got
    survived = p.alive()
    tb = b"Traceback" in p.buf
    p.send(b"q")
    p.read_until(b"cancelled", 10)
    # quit path: the exit CONTRACT is 4 (cancelled). status==0 here was
    # decode-blind against the raw waitpid encoding and sat FINDING from the
    # initial commit while every sub-fact printed healthy.
    code = exit_code(p.wait(5))
    restored = p.termios_restored()
    clean = noticed and survived and not tb and code == 4 and restored
    evidence("P5-hung-preview", "ok" if clean else "FINDING",
             "timeout-notice=%s after %.1fs survived=%s traceback=%s exit=%s termios-restored=%s"
             % (noticed, dt, survived, tb, code, restored))
    p.kill_close()

    # ---- P6: 40-column pty — the sweep contract: autowrap is disabled before
    # frames (\\x1b[?7l) and restored on exit (\\x1b[?7h), so an over-wide line
    # clips at the terminal's right edge instead of wrapping the layout into a
    # mangle. Line LENGTH stays informational: the bytes are still wide; the
    # terminal now clips them. (Pre-fix: no mode switch, 86-col lines wrapped
    # a 40-col pane — frozen log.)
    home = make_home("p6")
    p = PtyPicker(home, cols=40)
    p.read_until(b"colors: on", 15)
    nowrap_on = b"\x1b[?7l" in p.buf
    plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", p.buf)
    longest = max((len(l) for l in plain.split(b"\r\n")), default=0)
    p.send(b"q")
    p.read_until(b"cancelled", 10)
    p.wait(5)
    p.drain()
    nowrap_off = b"\x1b[?7h" in p.buf
    evidence("P6-narrow-pty", "ok" if nowrap_on and nowrap_off else "FINDING",
             "autowrap-off-before-frames=%s restored-on-exit=%s longest-line=%d cols on a 40-col pty"
             % (nowrap_on, nowrap_off, longest))
    p.kill_close()

    # ---- P7: save path end-to-end on a pty — config written, exact content
    home = make_home("p7")
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 15)
    p.drain()
    p.send(b" ")             # toggle top row (git-branch) off
    p.drain()
    p.send(b"\r")            # save
    p.read_until(b"saved", 10)
    p.wait(5)
    cfg_path = os.path.join(home, ".claude", "statusline-config.json")
    try:
        cfg = json.load(open(cfg_path))
        ok = cfg.get("items", [None])[0] != "git-branch" and "git-branch" not in cfg["items"]
    except Exception as e:  # noqa: BLE001 - any parse failure is the finding
        cfg, ok = repr(e), False
    evidence("P7-save", "ok" if ok else "FINDING", "config=%s" % json.dumps(cfg))
    p.kill_close()

    # ---- P8: probe rewrite during a live session — the picker holds payload
    # bytes read ONCE at startup; a newer probe landing mid-session gets
    # overwritten with the stale bytes on the next preview.
    home = make_home("p8")
    probe = canonical_probe(home)
    stale = open(probe, "rb").read()
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 15)
    p.drain()
    with open(probe, "wb") as fh:              # a live refresh lands a NEWER probe
        fh.write(b'{"marker": "NEWER-PROBE"}')
    p.send(b" ")                                # toggle -> preview -> renderer dump
    p.drain()
    now = open(probe, "rb").read()
    reverted = now == stale
    p.send(b"q")
    p.wait(5)
    evidence("P8-stale-probe-pin", "FINDING" if reverted else "ok",
             "newer probe overwritten back to startup bytes: %s (now=%r...)" % (reverted, now[:30]))
    p.kill_close()

    # ---- P9: ctrl-C in raw mode — mapped to quit, or KeyboardInterrupt mess?
    home = make_home("p9")
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 15)
    p.drain()
    p.send(b"\x03")
    got = p.read_until(b"cancelled", 10)
    # ctrl-C folds to quit, so the exit CONTRACT is 4 (cancelled), not 0 --
    # same decode-blind comparison as P5, red since the initial commit.
    code = exit_code(p.wait(5))
    restored = p.termios_restored()
    clean = b"cancelled" in got and code == 4 and restored and b"KeyboardInterrupt" not in p.buf
    evidence("P9-ctrl-c", "ok" if clean else "FINDING",
             "cancelled=%s exit=%s termios-restored=%s kbdint=%s"
             % (b"cancelled" in got, code, restored, b"KeyboardInterrupt" in p.buf))
    p.kill_close()

    # ---- P10: the terminal cursor — hidden before the FIRST frame
    # (\x1b[?25l) and restored AFTER quit (\x1b[?25h), so the hardware cursor
    # does not sit blinking under the "colors:" line while the picker owns
    # the screen, and the pane's shell gets its cursor back afterwards.
    # Order is asserted by byte position and the restore is searched only in
    # the bytes that arrived after q — a membership scan over the whole
    # accumulated buffer proved nothing about sequence — and the cancel exit
    # contract (4) is required. (Pre-fix: no cursor switch in either
    # direction — observed hidden=False restored=False.)
    home = make_home("p10")
    p = PtyPicker(home)
    p.read_until(b"colors: on", 15)
    hide_idx = p.buf.find(b"\x1b[?25l")
    frame_idx = p.buf.find(b"colors: on")
    hid_before_frame = 0 <= hide_idx < frame_idx
    mark = len(p.buf)
    p.send(b"q")
    p.read_until(b"cancelled", 10)
    code = exit_code(p.wait(5))
    p.drain()
    shown_after_quit = b"\x1b[?25h" in p.buf[mark:]
    clean = hid_before_frame and shown_after_quit and code == 4
    evidence("P10-cursor", "ok" if clean else "FINDING",
             "hidden-before-first-frame=%s (hide@%d frame@%d) restored-after-quit=%s exit=%s"
             % (hid_before_frame, hide_idx, frame_idx, shown_after_quit, code))
    p.kill_close()

    # ---- F-cases: every comparator that can report "ok" above is observed
    # firing against a doctored known-bad first (house rule). Expected verdict
    # for each F-case is FLIPPED — the detector fires.

    # F1 flips N2's sha comparator: a doctored probe must yield unequal hashes.
    home = make_home("f1")
    probe = canonical_probe(home)
    h1 = sha(probe)
    with open(probe, "ab") as fh:
        fh.write(b" ")
    h2 = sha(probe)
    evidence("F1-sha-flip", "FLIPPED" if h1 != h2 else "LAB-DEAD",
             "doctored probe changes sha: %s" % (h1 != h2))
    if h1 == h2:
        lab_error("F1", "sha comparator blind")

    # F2 flips P3/P4's colors-flip detector: a REAL c press must show colors: off.
    home = make_home("f2")
    p = PtyPicker(home)
    p.read_until(b"colors: on", 15)
    p.drain()
    p.send(b"c")
    frame = p.read_until(b"colors:", 5)
    fired = b"colors: off" in frame
    evidence("F2-colors-flip", "FLIPPED" if fired else "LAB-DEAD",
             "plain c toggles the colors line: %s" % fired)
    if not fired:
        lab_error("F2", "colors detector blind")
    p.send(b"q")
    p.wait(5)
    p.kill_close()

    # F3 flips P2's aliveness detector AND the termios probe: mid-run the pty
    # must read RAW (ECHO off), and a real q must be seen as death.
    home = make_home("f3")
    p = PtyPicker(home)
    p.read_until(b"statusline picker", 15)
    p.drain()
    raw_mid = not p.termios_restored()
    p.send(b"q")
    p.read_until(b"cancelled", 10)
    p.wait(5)
    dead = not p.alive()
    evidence("F3-alive-termios", "FLIPPED" if raw_mid and dead else "LAB-DEAD",
             "raw-mode-detected-mid-run=%s death-detected-after-q=%s" % (raw_mid, dead))
    if not (raw_mid and dead):
        lab_error("F3", "aliveness or termios probe blind")
    p.kill_close()

    # F4 flips P7's config comparator: garbage bytes at the config path must
    # take the exception branch.
    bad = os.path.join(LAB, "f4-bad-config.json")
    with open(bad, "w") as fh:
        fh.write("{broken")
    try:
        json.load(open(bad))
        f4 = False
    except ValueError:
        f4 = True
    evidence("F4-config-flip", "FLIPPED" if f4 else "LAB-DEAD",
             "garbage config takes the exception branch: %s" % f4)
    if not f4:
        lab_error("F4", "config comparator blind")

    print("---")
    if lab_errors:
        print("LAB BROKEN: %s" % ", ".join(lab_errors))
        return 1
    print("LAB COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
