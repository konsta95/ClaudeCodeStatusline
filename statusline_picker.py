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
    statusline_picker.py --selftest
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

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
        raise RuntimeError("%s --segments timed out after %ds" % (js, PREVIEW_TIMEOUT))
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
        )
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
    """Render a candidate selection through the REAL renderer. sandbox_home
    redirects $HOME so the renderer's payload-probe dump cannot overwrite the
    live probe when previewing fixture data. A renderer that fails or hangs
    degrades to a bracketed notice; it never raises."""
    env = dict(os.environ)
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
        if out.returncode != 0:
            return "[preview failed: exit %d]" % out.returncode
        return out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


def pick_payload(probe_path, explicit=False):
    """(payload_bytes, sandbox_home, live_path). A readable, valid-JSON probe
    previews through the real HOME, and its path is returned so every preview
    re-reads it fresh -- the renderer dumps the payload it was just fed, so
    fresh bytes keep a live session's probe from being clobbered with
    startup-stale data (the overwrite window shrinks to one preview's
    read-to-dump interval). A missing or invalid DEFAULT probe falls back to
    fixture data under a sandbox HOME; for a user-named --payload the same
    silent fallback would hide a typo, so explicit=True raises instead."""
    try:
        with open(probe_path, "rb") as fh:
            raw = fh.read()
        json.loads(raw.decode("utf-8", "replace"))
        return raw, None, probe_path
    except OSError:
        if explicit:
            raise
    except ValueError as exc:
        if explicit:
            raise ValueError("payload %s is not JSON: %s" % (probe_path, exc))
    sandbox = tempfile.mkdtemp(prefix="statusline-preview-")
    return json.dumps(FIXTURE_PAYLOAD).encode("utf-8"), sandbox, None


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


def draw(state, preview, write):
    write("\x1b[2J\x1b[H")
    write("statusline picker -- space toggle, left/right reorder, c colors, "
          "v built-in setup, Enter save, q/Esc cancel\r\n")
    write("\r\npreview: " + preview.replace("\n", "") + "\x1b[0m\r\n\r\n")
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
    told to do next."""
    keys = iter(keys)
    dirty = True
    preview = ""
    while True:
        if dirty:
            preview = previewer(state)
            dirty = False
        # frame before pull: the key reader blocks until a key arrives, and
        # the frame must already be on screen while it waits
        draw(state, preview, write)
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
    payload, sandbox, _live = pick_payload(probe_path, explicit=explicit_payload)
    try:
        preview = render_preview(node, js, items, colors, payload, sandbox)
    finally:
        if sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
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
            lambda _s: "p",
            sink.append,
        )
        check("scripted run saves [c,a] colors off",
              outcome == "saved" and st3.enabled == ["c", "a"] and st3.colors is False)
        check("scripted run drew frames", any("picker" in s for s in sink))
        # cancel path
        st4 = PickerState(registry, ["a"], True)
        outcome4 = run_picker(st4, iter(["space", "quit"]), lambda _s: "p", lambda _s: None)
        check("quit cancels", outcome4 == "cancelled")
        # customize is a cancel that carries a reason: the caller must be able
        # to tell it apart from a plain cancel, and edits made before pressing
        # it must NOT be treated as a save
        st4b = PickerState(registry, ["a"], True)
        outcome4b = run_picker(st4b, iter(["space", "customize"]), lambda _s: "p",
                               lambda _s: None)
        check("customize returns its own outcome", outcome4b == "customize")
        check("customize is distinguishable from cancel", outcome4b != "cancelled")
        # exhausted key stream (no Enter) must not save either
        st5 = PickerState(registry, ["a"], True)
        outcome5 = run_picker(st5, iter(["down"]), lambda _s: "p", lambda _s: None)
        check("key stream end cancels", outcome5 == "cancelled")
        # the first frame must be visible before the first key is pulled --
        # the reader blocks until a key arrives
        order = []

        def keys_recording():
            order.append("pull")
            yield "quit"

        st6 = PickerState(registry, ["a"], True)
        run_picker(st6, keys_recording(), lambda _s: "p",
                   lambda _s: order.append("draw") if "draw" not in order else None)
        check("first frame precedes first key pull", order[:1] == ["draw"])

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
            # registry fetch contract against the real renderer, when present
            if os.path.exists(DEFAULT_JS):
                reg = fetch_registry(node, DEFAULT_JS)
                check("live registry: >=8 id+label pairs",
                      len(reg) >= 8 and all(r and l for r, l in reg))
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

    if args.show:
        try:
            show(node, args.js, args.config, args.payload, sys.stdout.write,
                 explicit_payload=(args.payload != DEFAULT_PROBE))
        except (RuntimeError, OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: interactive picker needs a TTY (use --show without one)",
              file=sys.stderr)
        sys.exit(2)

    try:
        registry = fetch_registry(node, args.js)
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
    known = [rid for rid, _ in registry]
    items, colors = load_config(args.config, known)
    state = PickerState(registry, items, colors)
    try:
        payload, sandbox, live_path = pick_payload(
            args.payload, explicit=(args.payload != DEFAULT_PROBE))
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)

    def previewer(st):
        data = payload
        if live_path:
            try:
                with open(live_path, "rb") as fh:
                    fresh = fh.read()
                json.loads(fresh.decode("utf-8", "replace"))
                data = fresh
            except (OSError, ValueError):
                pass  # probe mid-write or gone: fall back to the startup bytes
        return render_preview(node, args.js, st.enabled, st.colors, data, sandbox)

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
        save_config(args.config, state.enabled, state.colors)
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
