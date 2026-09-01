#!/usr/bin/env python3
"""Interactive segment picker for the Claude Code statusline.

Local mirror of the Codex ``/statusline`` picker (semantics read from
codex-rs/tui/src/bottom_pane/status_line_setup.rs on 2026-08-25): a checkbox
list over the renderer's segment registry with a live preview line; membership
is the selection, order is the render order, confirm persists, cancel leaves
the config untouched. Claude Code has no in-TUI modal hook, so this runs in a
separate terminal (e.g. a tmux pane); the statusline reads its config on every
render, so a save takes effect on the next refresh without touching the
running session -- the same non-interrupting property the Codex picker has.

Single-carrier contract: the segment registry lives ONLY in statusline.js and
is fetched via ``node statusline.js --segments``; this tool never carries a
copy. The preview shells out to the SAME renderer with STATUSLINE_CONFIG
pointing at a candidate config, so preview and live bar cannot drift.

Config file (``~/.claude/statusline-config.json``)::

    {"items": ["model", "context", ...], "colors": true}

Absent file = all segments in default order. Unknown ids are skipped,
duplicates dropped, a broken file falls back to the default -- the renderer
and this tool implement the same forgiving parse. Delete the file to restore
defaults.

Keys: up/down move the cursor, space toggles, left/right reorder within the
enabled block, c toggles colors, v hands off to Claude Code's own statusline
setup, Enter saves, q/Q/Esc/ctrl-C cancel.
Modified arrows (e.g. ctrl-right) act as their plain arrow.

``v`` is a HANDOFF, not a feature: this is a standalone TUI and cannot spawn a
Claude Code agent, so it leaves the config untouched and exits 3 to say "the
human asked for the built-in workflow instead". Acting on that belongs to
whoever launched the picker -- see the ``/statusline`` command shipped in this
repo, which spawns the built-in ``statusline-setup`` agent when it sees a 3.
Nothing here knows what a slash command is, and it should stay that way.

Exit codes: 0 SAVED, 1 selftest failures, 2 environment or usage errors,
3 the human chose the built-in setup and the caller should hand off to it,
4 cancelled with nothing written.

Cancel gets its own code rather than sharing 0 with a save. A caller typically
runs this in a pane it cannot read -- the outcome line goes to that pane's
screen, not back to whoever opened it -- so the exit code is the ONLY channel
out, and a shared 0 would leave "saved" and "cancelled" indistinguishable. A
caller that guessed would report a save that never happened.

CLI::

    statusline_picker.py              # interactive picker (needs a TTY)
    statusline_picker.py --show       # print registry, config and preview
    statusline_picker.py --apply "model,context" [--colors on|off]
                                      # save a selection; no TTY needed
    statusline_picker.py --colors off # keep items, set colors only
    statusline_picker.py --selftest

``--apply`` exists so a caller with no terminal of its own -- a Claude Code
conversation building the selection through AskUserQuestion popups, any
platform without termios -- can still save through the same validated,
atomic path the TUI uses. It is strict where the file parse is forgiving:
the config on disk has no one to ask, so unknown ids there are skipped,
but --apply is an explicit instruction, and silently repairing it would
write a bar the caller did not ask for. Same reasoning as --payload:
explicit input fails loudly, defaults degrade gracefully.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")

# The renderer defaults to the one shipped beside this file, so a fresh clone is
# self-contained and the picker can never silently preview through a DIFFERENT
# renderer than the one it is configuring. Override with --js, or with
# STATUSLINE_JS when the renderer is installed elsewhere.
DEFAULT_JS = os.environ.get("STATUSLINE_JS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "statusline.js")

# The config and the payload probe are read by the RENDERER at Claude Code's
# chosen location, not by this repo, so these stay anchored to ~/.claude.
# The probe is opt-in and usually absent; the picker falls back to a synthetic
# fixture payload when it is, so previews work on a clean machine.
DEFAULT_CONFIG = os.environ.get("STATUSLINE_CONFIG") or os.path.join(
    HOME, ".claude", "statusline-config.json")
DEFAULT_PROBE = os.path.join(HOME, ".claude", "statusline-payload-last.json")
PREVIEW_TIMEOUT = 10

# Escape sequences arrive from the terminal as one burst; a human pressing
# bare ESC and then another key is orders of magnitude slower. This settle
# window separates the two cases and bounds every mid-sequence read.
ESC_SETTLE = 0.05

# Minimal payload for preview when no live probe exists (fresh box). Field
# shapes follow a probe captured from Claude Code 2.1.245.
FIXTURE_PAYLOAD = {
    "session_id": "00000000-fixture",
    "session_name": "preview fixture",
    "cwd": HOME,
    "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
    "effort": {"level": "max"},
    "fast_mode": False,
    "version": "0.0.0",
    "cost": {"total_cost_usd": 1.23},
    "context_window": {
        "total_input_tokens": 83000,
        "context_window_size": 1000000,
        "used_percentage": 8,
        "remaining_percentage": 92,
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 25, "resets_at": 0},
        "seven_day": {"used_percentage": 5, "resets_at": 0},
    },
    "workspace": {"current_dir": HOME, "project_dir": HOME, "added_dirs": []},
}


def fetch_registry(node, js):
    """The renderer is the only carrier of the segment registry. Every failure
    mode raises RuntimeError; callers print it as a clean error, never as a
    traceback."""
    try:
        out = subprocess.run(
            [node, js, "--segments"], capture_output=True, timeout=PREVIEW_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "%s --segments timed out after %ds" % (js, PREVIEW_TIMEOUT)
        ) from None
    except OSError as exc:
        # node can vanish or lose execute permission between shutil.which and
        # here; that is environmental, so it must surface as this function's
        # RuntimeError contract, not escape as a traceback
        raise RuntimeError("could not launch %s: %s" % (node, exc)) from exc
    if out.returncode != 0:
        raise RuntimeError(
            "%s --segments exited %d: %s"
            % (js, out.returncode, out.stderr.decode("utf-8", "replace").strip())
        )
    try:
        entries = json.loads(out.stdout.decode("utf-8", "replace"))
        registry = [(str(e["id"]), str(e["label"])) for e in entries]
    except (ValueError, TypeError, KeyError):
        raise RuntimeError(
            "%s --segments printed something other than the registry: %r"
            % (js, out.stdout[:120])
        ) from None
    if not registry:
        raise RuntimeError("empty segment registry from %s" % js)
    return registry


def normalize(items, known_ids):
    """Same forgiving semantics as statusline.js loadConfig: skip unknown ids,
    drop duplicates, preserve first-occurrence order."""
    seen = set()
    result = []
    for item in items:
        if item in known_ids and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_apply_items(raw, known_ids):
    """Strict parse of an --apply selection over the live registry.

    Contract: comma-separated ids, order = render order; empty tokens are
    dropped, so "" is the documented empty bar and a trailing comma is not an
    error. Unknown and duplicate ids RAISE (message naming them) instead of
    normalizing away -- an explicit instruction silently repaired would save a
    bar the caller never asked for. The forgiving parse stays file-side only."""
    items = [t for t in (t.strip() for t in raw.split(",")) if t]
    unknown = [t for t in items if t not in known_ids]
    if unknown:
        raise ValueError(
            "unknown segment id(s): %s (registry: %s)"
            % (", ".join(unknown), ", ".join(known_ids)))
    seen, dupes = set(), []
    for t in items:
        if t in seen and t not in dupes:
            dupes.append(t)
        seen.add(t)
    if dupes:
        raise ValueError("duplicate segment id(s): %s" % ", ".join(dupes))
    return items


def load_config(path, known_ids):
    """(items, colors) with the renderer's fallback: absent/broken file or a
    non-list items key = all known ids; colors defaults True."""
    default = (list(known_ids), True)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return default
    if not isinstance(cfg, dict):
        return default
    raw_items = cfg.get("items")
    items = (
        normalize(raw_items, known_ids)
        if isinstance(raw_items, list)
        else list(known_ids)
    )
    return items, cfg.get("colors") is not False


def save_config(path, items, colors):
    """Atomic write via a UNIQUE temp name in the target directory: readers
    (the statusline may render at any moment) see old or new bytes, never
    partial, and two concurrent savers cannot share a temp file -- the later
    rename wins whole."""
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp",
        dir=os.path.dirname(path) or ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"items": list(items), "colors": bool(colors)}, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_preview(node, js, items, colors, payload_bytes, sandbox_home=None):
    """Render a candidate selection through the REAL renderer. A renderer that
    fails or hangs degrades to a bracketed notice; it never raises.

    A PREVIEW NEVER WRITES A PAYLOAD PROBE. The renderer dumps whatever bytes it
    is fed when STATUSLINE_PAYLOAD_DUMP is set, and the picker feeds it bytes read
    earlier -- so an inherited dump setting turns every repaint into a write of
    possibly stale data over a live producer's probe, which the picker would then
    read back and honestly report as current, because it would be. Redirecting
    HOME does not cover this: the dump setting doubles as an explicit path, and an
    explicit path ignores HOME. Unsetting the variable holds for both forms.

    sandbox_home still redirects everything else the renderer reads out of HOME,
    and is where the temporary config is written."""
    env = dict(os.environ)
    env.pop("STATUSLINE_PAYLOAD_DUMP", None)
    tmpdir = sandbox_home or tempfile.gettempdir()
    fd, cfg_path = tempfile.mkstemp(suffix=".json", dir=tmpdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"items": list(items), "colors": bool(colors)}, fh)
        env["STATUSLINE_CONFIG"] = cfg_path
        if sandbox_home:
            env["HOME"] = sandbox_home
            os.makedirs(os.path.join(sandbox_home, ".claude"), exist_ok=True)
        try:
            out = subprocess.run(
                [node, js],
                input=payload_bytes,
                capture_output=True,
                env=env,
                timeout=PREVIEW_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return "[preview timed out after %ds]" % PREVIEW_TIMEOUT
        except OSError as exc:
            return "[preview failed: could not launch %s: %s]" % (node, exc)
        if out.returncode != 0:
            return "[preview failed: exit %d]" % out.returncode
        return out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


def read_probe(path):
    """(bytes, the mtime of the inode they came from), from ONE open handle.

    An instant, not an age. An age is only true at the moment it is taken, so a
    caller holding bytes across repaints cannot reuse one: it would have to add a
    second duration measured from some other origin, and the sum is then wrong by
    whatever separates the two origins. An mtime has no origin to get wrong, and
    every age in this file is derived from one at the moment it is reported.

    One handle, because the renderer publishes the probe with an atomic rename:
    statting the pathname after the read can sample a different inode, and the
    time would then belong to a generation the caller never saw. Taking it off
    the open descriptor closes that window rather than narrowing it.

    Raises OSError exactly as open() does. What an unreadable probe means is the
    caller's decision, not this function's."""
    with open(path, "rb") as fh:
        return fh.read(), os.fstat(fh.fileno()).st_mtime


def format_age(seconds):
    """Coarse duration, one unit, largest that fits. Deliberately imprecise --
    the mtime answers 'how stale', not 'how long exactly', and a padded string
    would imply a resolution the question does not have."""
    whole = max(0, int(seconds))
    if whole < 60:
        return "%ds" % whole
    if whole < 3600:
        return "%dm" % (whole // 60)
    if whole < 86400:
        return "%dh" % (whole // 3600)
    return "%dd" % (whole // 86400)


def payload_label(live_path, age, elapsed):
    """What the preview was rendered FROM, in one phrase. Pure: no clock, no I/O.

    Contract, from owner decision fe3c4f4d57ce -- report the age, set no
    threshold. Nothing here rejects a probe, falls back off one, or warns about
    one. A stale probe still previews; it is only named.

    'live' is not a freshness guess and rests on no constant. It means the probe
    was rewritten after this picker started, which is why ELAPSED is the picker's
    own age rather than a tolerance: a Claude Code session refreshing its status
    line rewrites the probe, so a probe that moves under a running picker has a
    producer behind it at that moment.

    ELAPSED is None when there was no window for a rewrite to fall into -- the
    one-shot dump asserts that -- and the wall-clock length of the window
    otherwise. A sentinel rather than a measured "greater than zero", because a
    duration cannot answer whether a window EXISTS once the clock is allowed to
    step: a backward step makes a real window measure negative, and a probe
    written into it is then reported old. The one-shot needs an explicit answer
    for a second reason as well -- a probe whose mtime sits at or ahead of the
    clock yields an age of zero or less, which compares as live against a window
    of any length. Clocks do run ahead: a synced home directory or a producer on
    another host is enough.

    AGE and ELAPSED must be derived from the SAME instant. Each is that instant
    minus a fixed point, so it cancels out of the comparison, which is therefore
    exactly "was the probe rewritten after this picker started" -- a question
    about two fixed points, whose answer no later clock adjustment can change.
    Taking the two from different clocks destroys the cancellation and that
    immunity with it. An ELAPSED from CLOCK_MONOTONIC in particular, which does
    not advance while the machine is suspended, reports a probe written seconds
    after startup as hours old across a suspend: its AGE counts the suspended
    time and its ELAPSED does not.

    AGE None means the caller could not determine one. No caller produces that
    today -- read_probe raises instead -- and the branch is kept so that one which
    cannot read an mtime has a way to say so rather than passing 0, which would
    read as maximally fresh."""
    if not live_path:
        return "fixture"
    if age is None:
        return "unknown age"
    if elapsed is not None and age <= elapsed:
        return "live"
    return "%s old" % format_age(age)


def pick_payload(probe_path, explicit=False):
    """(payload_bytes, sandbox_home, live_path, probe_mtime). A readable,
    valid-JSON probe previews through the real HOME, and its path is returned so
    every preview re-reads it fresh: a picker left open while a session works
    should show what that session is sending now, not what it sent when the
    picker started. (Re-reading was also the old defence against the renderer
    dumping stale bytes back over a live probe. render_preview now unsets the
    dump outright, so freshness is the only reason left.) A missing or invalid
    DEFAULT probe falls back to fixture data under a sandbox HOME; for a
    user-named --payload the same silent fallback would hide a typo, so
    explicit=True raises instead.

    probe_mtime exists because valid JSON is not evidence of currency. The dump
    that writes this probe is opt-in, so it stops when a human turns it off while
    the file stays behind: a week-old probe is readable, parses, and is returned
    by the same branch a live one is. Nothing in the bytes distinguishes them,
    which is why the distinction has to be carried out of here separately.

    Per fe3c4f4d57ce the age is REPORTED and never acted on -- no threshold
    rejects a probe, no age triggers the fixture fallback, and the missing and
    invalid branches above are unchanged. The mtime is None for fixture data,
    which has no probe behind it and therefore no age; that is not an age of 0,
    and callers must keep the two apart rather than defaulting one to the other."""
    try:
        raw, mtime = read_probe(probe_path)
        json.loads(raw.decode("utf-8", "replace"))
        return raw, None, probe_path, mtime
    except OSError:
        if explicit:
            raise
    except ValueError as exc:
        if explicit:
            raise ValueError(
                "payload %s is not JSON: %s" % (probe_path, exc)
            ) from exc
    sandbox = tempfile.mkdtemp(prefix="statusline-preview-")
    return json.dumps(FIXTURE_PAYLOAD).encode("utf-8"), sandbox, None, None


def preview_state(live_path, startup_payload, startup_mtime, started_at, now):
    """(bytes to render, the label describing them) for one repaint at NOW.

    NOW is the instant the repaint describes; every age in the returned label is
    measured against it, so the label is internally consistent even though the
    probe read below happens fractionally after it.

    The two instants are not interchangeable and neither substitutes for the
    other. STARTED_AT is the origin of the rewrite window -- how long a producer
    has had to touch the probe while this picker has been open -- and it says
    nothing about when any bytes were written. STARTUP_MTIME is when the retained
    bytes were written, and their age is measured from that and nothing else.

    Split out of the previewer closure so the retained-bytes branch is reachable
    from the selftest. That branch is the only place the two can be confused, and
    while it lived in a closure no check could reach it.

    An unreadable probe keeps the startup bytes rather than blanking the preview:
    a probe caught mid-rename is ordinary, and one failed read is not evidence the
    producer is gone. The label then describes those bytes, not the missing ones."""
    data = startup_payload
    age = None if startup_mtime is None else now - startup_mtime
    if live_path:
        try:
            fresh, fresh_mtime = read_probe(live_path)
            json.loads(fresh.decode("utf-8", "replace"))
            data, age = fresh, now - fresh_mtime
        except (OSError, ValueError):
            pass  # probe mid-write or gone: fall back to the startup bytes
    return data, payload_label(live_path, age, now - started_at)


class PickerState:
    """Enabled block (render order) + disabled block (canonical order), the
    same two-block model the Codex picker builds in its constructor."""

    def __init__(self, registry, enabled_ids, colors):
        self.labels = dict(registry)
        self.canonical = [rid for rid, _ in registry]
        self.enabled = [rid for rid in enabled_ids if rid in self.labels]
        self.disabled = [rid for rid in self.canonical if rid not in self.enabled]
        self.colors = bool(colors)
        self.cursor = 0

    def rows(self):
        return self.enabled + self.disabled

    def clamp(self):
        top = max(0, len(self.rows()) - 1)
        self.cursor = min(max(self.cursor, 0), top)

    def move_cursor(self, delta):
        self.cursor += delta
        self.clamp()

    def toggle(self):
        rows = self.rows()
        if not rows:
            return
        rid = rows[self.cursor]
        if rid in self.enabled:
            # off: back into the disabled block at its canonical slot
            self.enabled.remove(rid)
            self.disabled = [
                c for c in self.canonical if c in self.disabled or c == rid
            ]
            self.cursor = len(self.enabled) + self.disabled.index(rid)
        else:
            # on: append to the enabled block -- selection order = render order
            self.disabled.remove(rid)
            self.enabled.append(rid)
            self.cursor = len(self.enabled) - 1

    def reorder(self, delta):
        """left/right: shift the item under the cursor within the enabled
        block; refuses to cross into the disabled block."""
        i, j = self.cursor, self.cursor + delta
        if 0 <= i < len(self.enabled) and 0 <= j < len(self.enabled):
            self.enabled[i], self.enabled[j] = self.enabled[j], self.enabled[i]
            self.cursor = j


def draw(state, preview, source, write):
    write("\x1b[2J\x1b[H")
    write("statusline picker -- space toggle, left/right reorder, c colors, "
          "v built-in setup, Enter save, q/Esc cancel\r\n")
    write("\r\npreview: " + preview.replace("\n", "") + "\x1b[0m\r\n")
    write("payload: " + source + "\r\n\r\n")
    for idx, rid in enumerate(state.rows()):
        cursor = ">" if idx == state.cursor else " "
        mark = "x" if rid in state.enabled else " "
        style = "" if rid in state.enabled else "\x1b[2m"
        write(
            "%s [%s] %s%-16s %s\x1b[0m\r\n"
            % (cursor, mark, style, rid, state.labels[rid])
        )
    write("\r\ncolors: %s\r\n" % ("on" if state.colors else "off"))


def run_picker(state, keys, previewer, write):
    """Drive the picker with a key-token stream. Returns 'saved', 'cancelled'
    or 'customize'; the caller persists. Injectable for the selftest.

    'customize' is a cancel that carries a reason: the config is left untouched
    exactly as on 'cancelled', and the only difference is what the caller is
    told to do next.

    previewer returns (bar, payload_label) as one value. The pair is deliberate:
    the label describes the bytes that produced THAT bar, so binding them at the
    source makes it impossible for a frame to show one render's preview beside
    another render's provenance."""
    keys = iter(keys)
    dirty = True
    preview, source = "", ""
    while True:
        if dirty:
            preview, source = previewer(state)
            dirty = False
        # frame before pull: the key reader blocks until a key arrives, and
        # the frame must already be on screen while it waits
        draw(state, preview, source, write)
        key = next(keys, None)
        if key is None:
            return "cancelled"
        if key == "up":
            state.move_cursor(-1)
        elif key == "down":
            state.move_cursor(1)
        elif key == "left":
            state.reorder(-1)
            dirty = True
        elif key == "right":
            state.reorder(1)
            dirty = True
        elif key == "space":
            state.toggle()
            dirty = True
        elif key == "colors":
            state.colors = not state.colors
            dirty = True
        elif key == "customize":
            return "customize"
        elif key == "enter":
            return "saved"
        elif key == "quit":
            return "cancelled"


def read_keys_tty(stdin):
    """Raw-mode key reader translating bytes to tokens. A lone ESC (no
    follow-up byte within ESC_SETTLE) cancels; CSI and SS3 sequences are
    consumed through their final byte, so modified arrows act as their plain
    arrow and no sequence tail can fall through into the key bindings. Raw
    mode is entered with TCSADRAIN: a key typed while the first preview
    renders is kept, not flushed."""
    import select
    import termios
    import tty

    fd = stdin.fileno()
    old = termios.tcgetattr(fd)
    arrows = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}

    def read1(timeout=None):
        if timeout is not None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return b""
        return os.read(fd, 1)

    try:
        tty.setraw(fd, termios.TCSADRAIN)
        while True:
            ch = read1()
            if not ch:
                return
            if ch == b"\x1b":
                nxt = read1(ESC_SETTLE)
                if not nxt:
                    yield "quit"  # bare ESC
                elif nxt == b"[":
                    # CSI: parameter/intermediate bytes 0x20-0x3f, then one
                    # final byte 0x40-0x7e closes the sequence
                    fin = b""
                    while True:
                        part = read1(ESC_SETTLE)
                        if not part or 0x40 <= part[0] <= 0x7E:
                            fin = part
                            break
                    yield arrows.get(fin, "other")
                elif nxt == b"O":
                    # SS3: application-mode arrows send ESC O A..D
                    yield arrows.get(read1(ESC_SETTLE), "other")
                else:
                    yield "other"  # alt-chord: consumed whole, never rebound
            elif ch in (b"\r", b"\n"):
                yield "enter"
            elif ch == b" ":
                yield "space"
            elif ch in (b"c", b"C"):
                yield "colors"
            elif ch in (b"v", b"V"):
                yield "customize"
            elif ch in (b"q", b"Q", b"\x03"):
                yield "quit"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def show(node, js, cfg_path, probe_path, write, explicit_payload=False):
    """One-shot dump: registry, config, preview. Raises RuntimeError /
    OSError / ValueError upward; main prints them as clean exit-2 errors."""
    registry = fetch_registry(node, js)
    known = [rid for rid, _ in registry]
    items, colors = load_config(cfg_path, known)
    write("config: %s%s\n" % (cfg_path, "" if os.path.exists(cfg_path) else " (absent -> defaults)"))
    for rid, label in registry:
        mark = "x" if rid in items else " "
        pos = str(items.index(rid) + 1) if rid in items else "-"
        write(" [%s] %-2s %-16s %s\n" % (mark, pos, rid, label))
    write("colors: %s\n" % ("on" if colors else "off"))
    payload, sandbox, live, mtime = pick_payload(probe_path, explicit=explicit_payload)
    try:
        preview = render_preview(node, js, items, colors, payload, sandbox)
    finally:
        if sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
    # the path goes out with the label because --show is what a human runs when
    # the bar looks wrong, and "fixture" without a path leaves them guessing
    # which file was missing
    if live:
        # derived at the point of report, not carried from the read. --show is one
        # shot so the two instants are microseconds apart here; it follows the rule
        # anyway because the interactive path depends on it. A live path with no
        # mtime cannot happen today -- they are set together -- and it degrades to
        # "unknown age" rather than a crash if that ever stops being true.
        age = None if mtime is None else time.time() - mtime
        # no window: this process opened no picker for a rewrite to land under
        write("payload: %s (%s)\n" % (payload_label(live, age, None), live))
    else:
        write("payload: fixture (no usable probe at %s)\n" % probe_path)
    # reset only when the preview carries color: colors:false output is ANSI-free
    write("preview: " + preview + ("\x1b[0m" if colors else "") + "\n")


def selftest():
    failures = []

    def check(name, cond):
        print("%s: %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    known = ["a", "b", "c"]
    registry = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]

    # normalize: unknown skipped, dupes dropped, order preserved
    check("normalize skips unknown + dupes", normalize(["c", "zz", "a", "c"], known) == ["c", "a"])
    check("normalize empty stays empty", normalize([], known) == [])

    # --apply parse: strict where the file parse above is forgiving. The two
    # refusal checks run against known-bad input by construction -- they are the
    # observation that the guard fires, not an assumption that it would.
    check("apply parse preserves order", parse_apply_items("c,a", known) == ["c", "a"])
    check("apply parse tolerates whitespace + trailing comma",
          parse_apply_items(" a , b ,", known) == ["a", "b"])
    check("apply parse: empty string is the empty bar", parse_apply_items("", known) == [])
    try:
        parse_apply_items("a,zz,yy", known)
        check("apply parse refuses unknown ids, naming them", False)
    except ValueError as exc:
        check("apply parse refuses unknown ids, naming them",
              "zz" in str(exc) and "yy" in str(exc))
    try:
        parse_apply_items("a,b,a", known)
        check("apply parse refuses duplicate ids, naming them", False)
    except ValueError as exc:
        check("apply parse refuses duplicate ids, naming them",
              str(exc).endswith(": a"))

    # fetch_registry's contract is "every failure mode raises RuntimeError";
    # an OSError escaping raw would exit 1 -- the "defect, do not retry" code --
    # for a node that vanished between shutil.which and exec, which is
    # environmental and exactly retryable
    try:
        fetch_registry("/nonexistent/statusline-node", "statusline.js")
        check("fetch_registry: unlaunchable node is a RuntimeError", False)
    except RuntimeError:
        check("fetch_registry: unlaunchable node is a RuntimeError", True)
    except OSError:
        check("fetch_registry: unlaunchable node is a RuntimeError", False)
    # render_preview's contract is "never raises": same launch failure must
    # degrade to a bracketed notice like every other renderer failure
    try:
        got_bad_node = render_preview("/nonexistent/statusline-node", "x.js",
                                      ["a"], False, b"{}")
        check("preview degrades when node cannot launch",
              got_bad_node.startswith("[preview failed:"))
    except OSError:
        check("preview degrades when node cannot launch", False)

    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "cfg.json")

        # load: absent -> all + colors on
        check("load absent -> defaults", load_config(cfg, known) == (known, True))
        # load: broken -> defaults
        with open(cfg, "w") as fh:
            fh.write("not json")
        check("load broken -> defaults", load_config(cfg, known) == (known, True))
        # load: empty items honored, colors false parsed
        with open(cfg, "w") as fh:
            json.dump({"items": [], "colors": False}, fh)
        check("load honors empty + colors:false", load_config(cfg, known) == ([], False))
        # save/load round-trip, atomic (no *.tmp residue anywhere in the dir --
        # the temp name is unique per writer, so a fixed-suffix probe is blind)
        save_config(cfg, ["b", "a"], True)
        check("save/load round-trip", load_config(cfg, known) == (["b", "a"], True))
        check("save leaves no tmp residue", not [f for f in os.listdir(td) if ".tmp" in f])

        # state: toggle off -> canonical slot in disabled block, cursor follows
        st = PickerState(registry, ["a", "b", "c"], True)
        st.cursor = 1
        st.toggle()
        check("toggle off -> disabled canonical slot",
              st.enabled == ["a", "c"] and st.disabled == ["b"] and st.cursor == 2)
        # toggle on -> appended to enabled (selection order)
        st.toggle()
        check("toggle on -> enabled tail",
              st.enabled == ["a", "c", "b"] and st.disabled == [] and st.cursor == 2)
        # reorder within enabled; refuses to cross blocks
        st.reorder(-1)
        check("reorder swaps within enabled", st.enabled == ["a", "b", "c"] and st.cursor == 1)
        st2 = PickerState(registry, ["a"], True)
        st2.cursor = 1  # first disabled row
        st2.reorder(1)
        check("reorder refused outside enabled block", st2.enabled == ["a"] and st2.cursor == 1)

        # scripted picker run: toggle b off, move c ahead of a, colors off, save
        st3 = PickerState(registry, ["a", "b", "c"], True)
        sink = []
        outcome = run_picker(
            st3,
            iter(["down", "space", "up", "left", "colors", "enter"]),
            lambda _s: ("p", "fixture"),
            sink.append,
        )
        check("scripted run saves [c,a] colors off",
              outcome == "saved" and st3.enabled == ["c", "a"] and st3.colors is False)
        check("scripted run drew frames", any("picker" in s for s in sink))
        # cancel path
        st4 = PickerState(registry, ["a"], True)
        outcome4 = run_picker(st4, iter(["space", "quit"]), lambda _s: ("p", "fixture"), lambda _s: None)
        check("quit cancels", outcome4 == "cancelled")
        # customize is a cancel that carries a reason: the caller must be able
        # to tell it apart from a plain cancel, and edits made before pressing
        # it must NOT be treated as a save
        st4b = PickerState(registry, ["a"], True)
        outcome4b = run_picker(st4b, iter(["space", "customize"]), lambda _s: ("p", "fixture"),
                               lambda _s: None)
        check("customize returns its own outcome", outcome4b == "customize")
        check("customize is distinguishable from cancel", outcome4b != "cancelled")
        # exhausted key stream (no Enter) must not save either
        st5 = PickerState(registry, ["a"], True)
        outcome5 = run_picker(st5, iter(["down"]), lambda _s: ("p", "fixture"), lambda _s: None)
        check("key stream end cancels", outcome5 == "cancelled")
        # the first frame must be visible before the first key is pulled --
        # the reader blocks until a key arrives
        order = []

        def keys_recording():
            order.append("pull")
            yield "quit"

        st6 = PickerState(registry, ["a"], True)
        run_picker(st6, keys_recording(), lambda _s: ("p", "fixture"),
                   lambda _s: order.append("draw") if "draw" not in order else None)
        check("first frame precedes first key pull", order[:1] == ["draw"])

        # Staleness REPORTING. Every check below asserts that a stale probe is
        # named; none asserts that one is refused, and the two that pin the
        # no-threshold contract are the ones to keep if these are ever trimmed:
        # a stale probe must still preview, and it must still preview from its
        # own bytes rather than the fixture.
        check("format_age steps one unit at each boundary",
              (format_age(0), format_age(59), format_age(60), format_age(3599),
               format_age(3600), format_age(86400), format_age(-5))
              == ("0s", "59s", "1m", "59m", "1h", "1d", "0s"))
        check("label: no probe is fixture, whatever the age argument says",
              payload_label(None, 10 ** 9, None) == "fixture")
        check("label: unreadable mtime is unknown, never an age of zero",
              payload_label("/p", None, None) == "unknown age")
        check("label: rewritten under a running picker is live",
              payload_label("/p", 5.0, 30.0) == "live")
        check("label: older than the picker reports its age",
              payload_label("/p", 7200.0, 30.0) == "2h old")
        check("label: a one-shot dump cannot reach live",
              payload_label("/p", 0.5, None) == "0s old")
        # the boundary the comparison alone gets wrong: age <= 0 against no
        # window. A probe written in the same clock tick, or by a producer whose
        # clock runs ahead, lands exactly here.
        check("label: a one-shot dump cannot reach live at age zero",
              payload_label("/p", 0.0, None) == "0s old")
        check("label: nor when the probe's mtime runs ahead of the clock",
              payload_label("/p", -30.0, None) == "0s old")
        check("label: a running picker still reaches live at the same ages",
              (payload_label("/p", 0.0, 5.0), payload_label("/p", -30.0, 5.0))
              == ("live", "live"))

        # A wall-clock step under a running picker. Same probe and same picker as
        # the live check above -- written 4s after startup -- seen through a clock
        # that jumped an hour. Both arguments move with the step, so it cancels
        # out of the comparison and the verdict does not move. Backward is the
        # direction the shipped code got wrong: it read a negative window as no
        # window at all and called a live probe dead.
        check("label: a backward clock step does not kill a live probe",
              payload_label("/p", -3504.0, -3500.0) == "live")
        check("label: nor does a forward clock step",
              payload_label("/p", 3696.0, 3700.0) == "live")
        # Why the window is measured on the same clock as the age and not on a
        # monotonic one: an hour of suspend, which CLOCK_MONOTONIC does not count.
        # It reaches the age either way -- an mtime has only the wall clock behind
        # it -- so a monotonic window would be short by the suspend and call this
        # 1h old.
        check("label: suspended time counts on both sides or on neither",
              payload_label("/p", 3606.0, 3610.0) == "live")

        # the shipped defect, as a fixture: readable, parses, two hours dead
        stale = os.path.join(td, "stale-probe.json")
        with open(stale, "w") as fh:
            json.dump({"session_id": "stale"}, fh)
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        raw_s, sbox_s, live_s, mtime_s = pick_payload(stale)
        # derived here, exactly as the callers derive it -- a missing mtime must
        # reach payload_label as "no age", never as an exception or a zero
        age_s = None if mtime_s is None else time.time() - mtime_s
        check("stale probe still previews from its own bytes",
              live_s == stale and sbox_s is None
              and json.loads(raw_s.decode())["session_id"] == "stale")
        check("stale probe carries the write instant out",
              age_s is not None and 7100 < age_s < 7300)
        raw_r, mtime_r = read_probe(stale)
        check("read_probe returns the bytes and their mtime from one handle",
              json.loads(raw_r.decode())["session_id"] == "stale"
              and 7100 < time.time() - mtime_r < 7300)
        check("stale probe is reported stale, not silently shown as current",
              payload_label(live_s, age_s, 1.0) == "2h old")
        raw_m, sbox_m, live_m, mtime_m = pick_payload(os.path.join(td, "absent.json"))
        try:
            check("missing probe: fixture, no path, and no instant to report",
                  live_m is None and mtime_m is None and sbox_m is not None
                  and json.loads(raw_m.decode())["session_id"] == "00000000-fixture")
        finally:
            if sbox_m:
                shutil.rmtree(sbox_m, ignore_errors=True)

        # The retained-bytes branch: a probe that read at startup and is gone by
        # this repaint. Its age comes off the bytes' own mtime, and the picker's
        # start is only the rewrite window. Both directions are pinned, because
        # confusing the two origins is wrong in one direction and reporting an age
        # captured once is wrong in the other.
        gone = os.path.join(td, "vanished.json")
        kept = b'{"session_id":"kept"}'
        opened = time.time() - 100.0
        d_old, l_old = preview_state(gone, kept, opened - 50.0, opened, opened + 100.0)
        check("retained bytes older than the picker report their own age",
              json.loads(d_old.decode())["session_id"] == "kept" and l_old == "2m old")
        d_new, l_new = preview_state(gone, kept, opened + 4.0, opened, opened + 100.0)
        check("retained bytes written under the running picker are still live",
              json.loads(d_new.decode())["session_id"] == "kept" and l_new == "live")
        d_fix, l_fix = preview_state(None, kept, None, opened, opened + 100.0)
        check("no probe: retained bytes are labelled fixture, not aged",
              d_fix == kept and l_fix == "fixture")
        d_re, l_re = preview_state(stale, kept, opened + 4.0, opened, time.time())
        check("a readable probe replaces the retained bytes and brings its own age",
              json.loads(d_re.decode())["session_id"] == "stale" and l_re == "2h old")

        # preview plumbing through a fake renderer: env config + stdin arrive
        node = shutil.which("node")
        if node:
            fake = os.path.join(td, "fake.js")
            with open(fake, "w") as fh:
                fh.write(
                    "const fs=require('fs');"
                    "const cfg=fs.readFileSync(process.env.STATUSLINE_CONFIG,'utf8');"
                    "let n=0;process.stdin.on('data',c=>n+=c.length);"
                    "process.stdin.on('end',()=>process.stdout.write(cfg+'|'+n));"
                )
            got = render_preview(node, fake, ["b"], False, b"12345", sandbox_home=td)
            check("preview passes config env + stdin",
                  '"items": ["b"]' in got and got.endswith("|5"))

            # A preview must not write a payload probe. This renderer writes one
            # if the environment tells it to, and the dump variable is set to an
            # EXPLICIT path -- the form a sandbox HOME cannot redirect -- so the
            # only thing that can keep the file from appearing is render_preview
            # dropping the variable.
            dumper = os.path.join(td, "dumper.js")
            probe_out = os.path.join(td, "must-not-appear.json")
            with open(dumper, "w") as fh:
                fh.write(
                    "const fs=require('fs');"
                    "const d=process.env.STATUSLINE_PAYLOAD_DUMP;"
                    "if(d)fs.writeFileSync(d,'written');"
                    "process.stdout.write(d?'DUMPED':'clean');"
                )
            prev_dump = os.environ.get("STATUSLINE_PAYLOAD_DUMP")
            os.environ["STATUSLINE_PAYLOAD_DUMP"] = probe_out
            try:
                out = render_preview(node, dumper, ["b"], False, b"{}", sandbox_home=td)
            finally:
                if prev_dump is None:
                    os.environ.pop("STATUSLINE_PAYLOAD_DUMP", None)
                else:
                    os.environ["STATUSLINE_PAYLOAD_DUMP"] = prev_dump
            check("preview never writes a payload probe, explicit path included",
                  out == "clean" and not os.path.exists(probe_out))
            # registry fetch contract against the real renderer, when present
            if os.path.exists(DEFAULT_JS):
                reg = fetch_registry(node, DEFAULT_JS)
                check("live registry: >=8 id+label pairs",
                      len(reg) >= 8 and all(r and l for r, l in reg))

                # --apply end to end, both polarities. The known-bad arm is the
                # whole point: a refusal must leave NOTHING on disk, because a
                # config written on the way to exit 2 would be a save the caller
                # was told did not happen.
                e2e_cfg = os.path.join(td, "apply-cfg.json")
                bad = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--apply", "model,bogus-id", "--config", e2e_cfg,
                     "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("apply e2e: unknown id exits 2 naming it, writes nothing",
                      bad.returncode == 2 and "bogus-id" in bad.stderr
                      and not os.path.exists(e2e_cfg))
                first_two = [rid for rid, _ in reg][:2]
                good = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--apply", ",".join(first_two), "--colors", "off",
                     "--config", e2e_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("apply e2e: saves exactly the selection, colors off",
                      good.returncode == 0
                      and load_config(e2e_cfg, [r for r, _ in reg])
                      == (first_two, False))
                # --colors alone edits colors and keeps the saved items
                flip = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--colors", "on", "--config", e2e_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("apply e2e: --colors alone keeps items, flips colors",
                      flip.returncode == 0
                      and load_config(e2e_cfg, [r for r, _ in reg])
                      == (first_two, True))
            else:
                print("SKIP: live registry (no %s)" % DEFAULT_JS)
        else:
            print("SKIP: preview plumbing (node not on PATH)")

    # raw-mode reader on a real pty: the CSI / SS3 / lone-ESC contract. A
    # watchdog alarm turns any regression back to unbounded blocking reads
    # into a failure instead of a hung selftest.
    if hasattr(os, "openpty"):
        import signal
        import termios as _termios

        class _FdStdin:
            def __init__(self, fd):
                self._fd = fd

            def fileno(self):
                return self._fd

        def _alarm(_sig, _frame):
            raise TimeoutError("pty reader blocked")

        master, slave = os.openpty()
        gen = None
        before = None
        old_handler = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(10)
        try:
            before = _termios.tcgetattr(slave)
            gen = read_keys_tty(_FdStdin(slave))
            os.write(master, b"\x1b[A")
            check("pty reader: plain arrow", next(gen) == "up")
            os.write(master, b"\x1b[1;5C")
            check("pty reader: modified arrow acts as its arrow", next(gen) == "right")
            os.write(master, b"c")
            check("pty reader: binding intact after CSI tail", next(gen) == "colors")
            os.write(master, b"v")
            check("pty reader: v yields customize", next(gen) == "customize")
            os.write(master, b"V")
            check("pty reader: V yields customize", next(gen) == "customize")
            os.write(master, b"\x1bq")
            check("pty reader: alt-chord discarded whole", next(gen) == "other")
            os.write(master, b"q")
            check("pty reader: q still quits after chord", next(gen) == "quit")
            os.write(master, b"\x1b")
            check("pty reader: lone ESC cancels", next(gen) == "quit")
            os.write(master, b"\x1bOB")
            check("pty reader: SS3 arrow", next(gen) == "down")
        except TimeoutError:
            check("pty reader: never blocks past the settle window", False)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            if gen is not None:
                gen.close()
            restored = before is not None and _termios.tcgetattr(slave) == before
            os.close(master)
            os.close(slave)
        check("pty reader: termios restored on close", restored)
    else:
        print("SKIP: pty reader checks (no os.openpty)")

    print("---")
    print("SELFTEST %s (%d failures)" % ("PASSED" if not failures else "FAILED", len(failures)))
    return 0 if not failures else 1


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--show", action="store_true",
                    help="print registry, current config and preview; no TTY needed")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks")
    ap.add_argument("--apply", metavar="IDS",
                    help="save a selection without the TUI: comma-separated segment "
                         "ids in render order ('' saves an empty bar); unknown or "
                         "duplicate ids are refused with exit 2; no TTY needed")
    ap.add_argument("--colors", choices=("on", "off"),
                    help="set colors when saving via --apply, or alone to change "
                         "colors while keeping the current items; absent = keep "
                         "the current value")
    ap.add_argument("--js", default=DEFAULT_JS, help="statusline renderer path")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config file to edit")
    ap.add_argument("--payload", default=DEFAULT_PROBE,
                    help="payload JSON for the preview (default: live probe; an "
                         "unreadable or non-JSON explicit path is an error)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    node = shutil.which("node")
    if not node:
        print("error: node not on PATH (the renderer is a Node script)", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(args.js):
        print("error: renderer not found: %s" % args.js, file=sys.stderr)
        sys.exit(2)

    if args.show and (args.apply is not None or args.colors):
        print("error: --show does not combine with --apply/--colors "
              "(one reads, the other writes)", file=sys.stderr)
        sys.exit(2)

    if args.show:
        try:
            show(node, args.js, args.config, args.payload, sys.stdout.write,
                 explicit_payload=(args.payload != DEFAULT_PROBE))
        except (RuntimeError, OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    if args.apply is not None or args.colors:
        # The non-interactive save path. Reaches neither the TTY probe nor the
        # termios import below on purpose: this is the path a caller with no
        # terminal -- an AskUserQuestion-built selection, native Windows -- saves
        # through, and it shares load/validate/save with the TUI rather than
        # reimplementing them.
        try:
            registry = fetch_registry(node, args.js)
        except RuntimeError as exc:
            print("error: %s" % exc, file=sys.stderr)
            sys.exit(2)
        known = [rid for rid, _ in registry]
        items, colors = load_config(args.config, known)
        if args.apply is not None:
            try:
                items = parse_apply_items(args.apply, known)
            except ValueError as exc:
                print("error: %s" % exc, file=sys.stderr)
                sys.exit(2)
        if args.colors:
            colors = args.colors == "on"
        try:
            save_config(args.config, items, colors)
        except OSError as exc:
            print("error: could not save %s: %s" % (args.config, exc), file=sys.stderr)
            sys.exit(2)
        print("saved %s" % args.config)
        print("items:  %s" % (", ".join(items) or "(none -- empty line)"))
        print("colors: %s" % ("on" if colors else "off"))
        print("takes effect on the next statusline refresh; delete the file to restore defaults")
        sys.exit(0)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: interactive picker needs a TTY (use --show without one)",
              file=sys.stderr)
        sys.exit(2)

    # Probed here rather than left to the lazy import in read_keys_tty: an
    # ImportError there escapes main() and Python exits 1, which this tool
    # documents as "selftest failures -- a real defect, do not retry". A
    # platform without termios is neither a defect nor un-retryable, so it
    # has to reach the environment code instead.
    try:
        import termios  # noqa: F401
    except ImportError:
        print("error: the interactive picker needs a POSIX terminal (termios); "
              "this platform has none. --show and --selftest work anywhere; "
              "on Windows, run it under WSL.", file=sys.stderr)
        sys.exit(2)

    try:
        registry = fetch_registry(node, args.js)
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
    known = [rid for rid, _ in registry]
    items, colors = load_config(args.config, known)
    state = PickerState(registry, items, colors)
    started_at = time.time()
    try:
        payload, sandbox, live_path, startup_mtime = pick_payload(
            args.payload, explicit=(args.payload != DEFAULT_PROBE))
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)

    def previewer(st):
        # only the rendering stays here; which bytes and which label is
        # preview_state, where a check can reach it
        data, label = preview_state(live_path, payload, startup_mtime,
                                    started_at, time.time())
        return render_preview(node, args.js, st.enabled, st.colors,
                              data, sandbox), label

    def write_flush(s):
        sys.stdout.write(s)
        sys.stdout.flush()

    # autowrap off while frames are on screen: a frame wider than the pane
    # clips at the right edge instead of wrapping the layout into a mangle
    write_flush("\x1b[?7l")
    try:
        outcome = run_picker(state, read_keys_tty(sys.stdin), previewer, write_flush)
    finally:
        write_flush("\x1b[?7h")
        if sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
    sys.stdout.write("\x1b[2J\x1b[H")
    if outcome == "saved":
        # save_config re-raises after cleaning up its temp file, and an
        # uncaught OSError here would exit 1 -- the code reserved for a picker
        # defect that must not be retried. A full disk or a read-only config
        # directory is the opposite: environmental, and retrying after fixing
        # it is exactly right. So it has to land on 2.
        try:
            save_config(args.config, state.enabled, state.colors)
        except OSError as exc:
            print("error: could not save %s: %s" % (args.config, exc),
                  file=sys.stderr)
            sys.exit(2)
        print("saved %s" % args.config)
        print("items:  %s" % (", ".join(state.enabled) or "(none -- empty line)"))
        print("colors: %s" % ("on" if state.colors else "off"))
        print("takes effect on the next statusline refresh; delete the file to restore defaults")
    elif outcome == "customize":
        # Deliberately says what the human asked for, not what should happen
        # next: this tool has no idea who launched it or what "hand off" means
        # in that context. The exit code is the contract; the line is for a
        # human reading the pane before it closes.
        print("customize -- %s untouched" % args.config)
        print("handing off to Claude Code's built-in statusline setup")
        sys.exit(3)
    else:
        print("cancelled -- %s untouched" % args.config)
        sys.exit(4)


if __name__ == "__main__":
    main()
