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

    {"items": ["model", "context", ...], "colors": true,
     "scheme": "codex", "item_colors": {"branch": "#87afff"}}

Absent file = the renderer's own default selection, which this tool reads from
the same ``--segments`` answer instead of deciding for itself. Unknown ids are
skipped, duplicates dropped, a broken file falls back to that default -- the
renderer and this tool implement the same forgiving parse. Delete the file to
restore defaults. Scheme names come from ``node statusline.js --schemes`` (the
renderer is the only carrier of those too); ``item_colors`` overrides one
segment's identity accent with a named ansi color or ``#RRGGBB`` hex --
the picker's accent key cycles the names, hex stays config-file-only, and
the renderer ignores specs it cannot parse rather than failing the bar.

Keys: up/down move the cursor, space toggles, left/right reorder within the
enabled block, c cycles colors (each scheme, then off), a cycles the selected
segment's accent override (colorable segments only), v hands off to Claude
Code's own statusline setup, Enter saves, q/Q/Esc/ctrl-C cancel.
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
    statusline_picker.py --apply "model,context" [--colors on|off] [--scheme NAME]
                                      # save a selection; no TTY needed
    statusline_picker.py --colors off # keep items, set colors only
    statusline_picker.py --scheme claude-code
                                      # keep items, switch the color scheme
    statusline_picker.py --selftest

``--apply`` exists so a caller with no terminal of its own -- a Claude Code
conversation building the selection through AskUserQuestion popups, any
platform without termios -- can still save through the same validated,
atomic path the TUI uses. It is strict where the file parse is forgiving:
the config on disk has no one to ask, so unknown ids there are skipped,
but --apply is an explicit instruction, and silently repairing it would
write a bar the caller did not ask for. Same reasoning as --payload:
explicit input fails loudly, defaults degrade gracefully.

Launch-window key guard (in ``--selftest``). WHY THIS EXISTS: the TTY reader
enters raw mode with TCSADRAIN so a key typed while the first preview renders
is kept, not flushed; ``tty.setraw``'s default ``when`` is TCSAFLUSH, which
discards it, so the regression is one dropped argument away. Writing a key to
the pty just before raw entry does not test that: a byte the line discipline
has not ingested yet survives TCSAFLUSH too (measured 2026-09-16: kept in 20
of 50 trials), so such a check reads green against the exact regression it
exists for. The guard types ``q`` -- the quit binding, the key the launch
window actually lost -- and waits for its echo on the master before raw entry:
a fresh pty is ICANON|ECHO, so the echo is proof of ingestion.

CONTRACT. Two arms on real ptys, both behind that echo barrier: the reader
itself must still yield quit for the ingested key (the guard), and TCSAFLUSH
on an identical fixture must drop it (the known-bad arm, asserting the
hazard's shape). If the byte ever survives TCSAFLUSH the arms no longer
discriminate, and that is a failure, never a vacuous pass; an echo that never
arrives fails its arm, never skips it. A green covers the kernel line
discipline of the host pty only: it says nothing about keys a terminal
emulator or tmux still holds before writing them to the pty, nor about the
Windows reader, which has no raw-mode entry. Without os.openpty the whole pty
block is skipped and reports nothing about the primitive; the closing receipt
``statusline_picker selftest: N/M passed`` counts only the checks that ran.
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

# This pair ships in two layouts and the renderer sits somewhere different in
# each: in a clone it is this file's sibling; installed, the picker goes to
# ~/.claude/tools/ while the renderer stays at ~/.claude/statusline.js, which is
# where settings.json's statusLine.command points at it. Resolution walks the
# layouts in that order and takes the first that is a FILE, so a clone stays
# self-contained and an install still finds its renderer.
JS_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline.js"),
    os.path.join(HOME, ".claude", "statusline.js"),
)


def resolve_js(env=None, candidates=JS_CANDIDATES):
    """Path to the renderer this picker previews and configures through.

    STATUSLINE_JS wins whenever it is SET, and is returned verbatim -- absent
    file, empty string, anything. Presence is the test rather than truthiness:
    falling through would preview a DIFFERENT renderer than the caller named,
    and an empty value is usually a script that expanded an unset variable,
    which deserves an error rather than a silent substitution.

    Candidates must be files. A directory named statusline.js would otherwise
    win selection and mask the real renderer waiting behind it.

    Returns the first candidate when none matches, because the caller needs a
    path to name in its message and that one describes the layout it is in.
    """
    environment = os.environ if env is None else env
    if "STATUSLINE_JS" in environment:
        return environment["STATUSLINE_JS"]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


DEFAULT_JS = resolve_js()

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

# What the picker changes about the screen while its frames are up, and the
# sequence that puts it back. Autowrap off: a frame wider than the pane clips
# at the right edge instead of wrapping the layout into a mangle. Cursor
# hidden: draw() parks it under the "colors:" line after every frame, where it
# would sit blinking as if the picker were a prompt. ONE leave sequence,
# because two paths emit it -- main()'s finally and the signal guard -- and
# they must not drift. It NORMALIZES rather than round-trips the incoming
# state: terminfo's own exit capability is a static sequence (cnorm, measured
# \x1b[?12l\x1b[?25h on xterm-256color) because prior mode state is not
# portably queryable, so an interactive shell's defaults -- wrap on, cursor
# visible -- are the exit contract, exactly as curses endwin leaves them. A
# DECRQM query round-trip would trade that for a blocking read on terminals
# that never answer it.
SCREEN_ENTER = "\x1b[?7l\x1b[?25l"
SCREEN_LEAVE = "\x1b[?7h\x1b[?25h"

# The accent ring 'a' cycles a segment through: None = the scheme's own slot,
# then the renderer's named override colors (its NAMED table). The renderer
# validates specs, so a name here that it does not know would be silently
# dropped from the bar. The selftest pins that: it renders every name on this
# ring through the real renderer under the mono scheme, where an escape on the
# line can only come from an honoured override. Hex specs are
# config-file-only.
ACCENT_RING = (None, "red", "green", "yellow", "blue",
               "magenta", "cyan", "white", "dim")

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
        # colorable is absent from older renderers; default False so the
        # accent submode stays off rather than painting segments the
        # renderer would ignore. default is absent from renderers that do not
        # publish it; those read as True, the rule that held before any
        # segment was opt-in.
        registry = [
            (str(e["id"]), str(e["label"]), bool(e.get("colorable")),
             bool(e.get("default", True)))
            for e in entries
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError(
            "%s --segments printed something other than the registry: %r"
            % (js, out.stdout[:120])
        ) from None
    if not registry:
        raise RuntimeError("empty segment registry from %s" % js)
    return registry


def fetch_schemes(node, js):
    """The renderer is the only carrier of the scheme list too (--schemes).
    Same failure contract as fetch_registry: every failure mode raises
    RuntimeError. A renderer too old to know --schemes ignores the flag,
    reads the empty stdin this call supplies, prints a bar instead of JSON,
    and lands in the parse error -- a clean mismatch report, not a hang."""
    try:
        out = subprocess.run(
            [node, js, "--schemes"], input=b"", capture_output=True,
            timeout=PREVIEW_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "%s --schemes timed out after %ds" % (js, PREVIEW_TIMEOUT)
        ) from None
    except OSError as exc:
        raise RuntimeError("could not launch %s: %s" % (node, exc)) from exc
    if out.returncode != 0:
        raise RuntimeError(
            "%s --schemes exited %d: %s"
            % (js, out.returncode, out.stderr.decode("utf-8", "replace").strip())
        )
    try:
        raw = json.loads(out.stdout.decode("utf-8", "replace"))
        # a JSON string or object also parses and would iterate per character
        # or per key -- only a list of non-empty names is the scheme list
        if (not isinstance(raw, list)
                or not all(isinstance(s, str) and s for s in raw)):
            raise TypeError
        schemes = raw
    except (ValueError, TypeError):
        raise RuntimeError(
            "%s --schemes printed something other than the scheme list: %r"
            % (js, out.stdout[:120])
        ) from None
    if not schemes:
        raise RuntimeError("empty scheme list from %s" % js)
    return schemes


def fetch_schemes_or_none(node, js):
    """Degrading fetch: the scheme list, or None when the renderer cannot
    answer --schemes. A renderer that answers --segments but not --schemes is
    version skew (older statusline.js beside a newer picker), and skew must
    not brick the picker: every path that can proceed without the offer --
    the TUI, --show, an --apply that names no scheme -- proceeds with
    known_schemes=None (load_config then preserves the stored scheme
    verbatim). Only an EXPLICIT --scheme, which cannot be validated blind,
    stays fatal; that path fetches unwrapped so the refusal can name the
    reason. Observed before the degrade existed: lab case P5's hung-renderer
    fixture killed the picker at startup with exit 2, one --schemes call
    before the first frame."""
    try:
        return fetch_schemes(node, js)
    except RuntimeError:
        return None


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


def default_ids(registry):
    """The selection the RENDERER falls back to with no usable config, in
    registry order. The picker must fall back to exactly this: a save that
    names no items (--colors, --scheme, Enter on an untouched list) writes the
    fallback to disk, so a second opinion here would change a bar the user
    never touched. Rows shorter than four fields -- fixtures, renderers that
    do not publish the flag -- count as default."""
    return [e[0] for e in registry if len(e) < 4 or e[3]]


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


def load_config(path, known_ids, known_schemes=("codex",), fallback_ids=None):
    """(items, colors, scheme, item_colors) with the renderer's fallback:
    absent/broken file or a non-list items key = fallback_ids, which callers
    take from default_ids(registry) so the fallback stays the renderer's
    decision (None = every known id, for a caller with no registry to ask);
    colors defaults True; an unknown scheme falls back to the first known one;
    item_colors keeps only str->str entries but preserves their VALUES
    opaquely (a hex spec the picker cannot cycle must still round-trip a
    save untouched -- the renderer is the one that judges specs).

    known_schemes=None means the offer could not be learned (a renderer too
    old for --schemes): the stored scheme string is then preserved VERBATIM
    rather than judged -- a caller that cannot see the offer must not rewrite
    the user's stored choice, and the renderer already treats an unknown
    scheme as codex at render time, so the value heals when the pair stops
    skewing."""
    fallback_scheme = known_schemes[0] if known_schemes else "codex"
    fallback_items = list(known_ids if fallback_ids is None else fallback_ids)
    default = (list(fallback_items), True, fallback_scheme, {})
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
        else list(fallback_items)
    )
    scheme = cfg.get("scheme")
    if not isinstance(scheme, str):
        scheme = fallback_scheme
    elif known_schemes is not None and scheme not in known_schemes:
        scheme = fallback_scheme
    raw_colors = cfg.get("item_colors")
    item_colors = {}
    if isinstance(raw_colors, dict):
        item_colors = {
            k: v for k, v in raw_colors.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    return items, cfg.get("colors") is not False, scheme, item_colors


def save_config(path, items, colors, scheme="codex", item_colors=None):
    """Atomic write via a UNIQUE temp name in the target directory: readers
    (the statusline may render at any moment) see old or new bytes, never
    partial, and two concurrent savers cannot share a temp file -- the later
    rename wins whole. A config directory that does not exist yet is created:
    the alternative was an error naming the TEMP file, which tells the user
    nothing about what to fix. Every failure is still an OSError, which the
    callers report as exit 2."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "items": list(items), "colors": bool(colors),
                "scheme": str(scheme),
                "item_colors": dict(item_colors or {}),
            }, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def colors_report(colors, scheme):
    """The value of the 'colors:' line in a save report. It names the scheme
    even when colors are off: --scheme alone is a save of exactly that value,
    and a report that left it out would not say what was saved."""
    if colors:
        return "on (%s)" % scheme
    return "off (scheme %s is saved and applies once colors are on)" % scheme


def home_env(env, home):
    # Node's os.homedir() and Python's expanduser read HOME on POSIX and
    # USERPROFILE on Windows. Setting one of them redirects nothing on the other
    # system, and there the child reads and writes the real profile.
    env["HOME"] = home
    env["USERPROFILE"] = home
    return env


def render_preview(node, js, items, colors, payload_bytes, sandbox_home=None,
                   scheme="codex", item_colors=None):
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
            json.dump({
                "items": list(items), "colors": bool(colors),
                "scheme": str(scheme),
                "item_colors": dict(item_colors or {}),
            }, fh)
        env["STATUSLINE_CONFIG"] = cfg_path
        if sandbox_home:
            home_env(env, sandbox_home)
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

    Contract, by design -- report the age, set no
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

    By that same contract the age is REPORTED and never acted on -- no threshold
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
    same two-block model the Codex picker builds in its constructor. Also
    carries the color state: colors on/off, the active scheme, and the
    per-item accent overrides ('a' cycles the selected row through the named
    accents; only rows the registry marks colorable)."""

    def __init__(self, registry, enabled_ids, colors,
                 scheme="codex", item_colors=None, schemes=("codex",)):
        # registry rows may be (id, label) -- older fixtures/renderers -- or
        # carry colorable and default after it; colorable defaults False, and
        # default is load_config's business, not this state's.
        self.labels = {e[0]: e[1] for e in registry}
        self.canonical = [e[0] for e in registry]
        self.colorable = {e[0] for e in registry if len(e) > 2 and e[2]}
        self.enabled = [rid for rid in enabled_ids if rid in self.labels]
        self.disabled = [rid for rid in self.canonical if rid not in self.enabled]
        self.colors = bool(colors)
        self.schemes = list(schemes) or ["codex"]
        self.scheme = scheme if scheme in self.schemes else self.schemes[0]
        self.item_colors = dict(item_colors or {})
        self.cursor = 0

    def cycle_colors(self):
        """One ring: on+scheme1 -> on+scheme2 -> ... -> off -> on+scheme1.
        Off keeps no scheme memory on purpose -- re-entering the ring at the
        first scheme is predictable; resuming a remembered one is not."""
        if not self.colors:
            self.colors, self.scheme = True, self.schemes[0]
            return
        i = self.schemes.index(self.scheme) if self.scheme in self.schemes else 0
        if i + 1 < len(self.schemes):
            self.scheme = self.schemes[i + 1]
        else:
            self.colors = False

    def cycle_accent(self):
        """Advance the selected row's accent override: default -> each named
        accent -> default. Rows the registry does not mark colorable are
        refused silently -- the renderer would drop the override anyway, and
        a picker that appears to paint what the bar will not honour lies. A
        non-cycle value from the config file (a hex spec) re-enters the ring
        at its start rather than being preserved: pressing 'a' IS the edit."""
        rows = self.rows()
        if not rows:
            return False
        rid = rows[self.cursor]
        if rid not in self.colorable:
            return False
        cur = self.item_colors.get(rid)
        ring = ACCENT_RING
        i = ring.index(cur) if cur in ring else 0
        nxt = ring[(i + 1) % len(ring)]
        if nxt is None:
            self.item_colors.pop(rid, None)
        else:
            self.item_colors[rid] = nxt
        return True

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
          "a accent, v built-in setup, Enter save, q/Esc cancel\r\n")
    write("\r\npreview: " + preview.replace("\n", "") + "\x1b[0m\r\n")
    write("payload: " + source + "\r\n\r\n")
    for idx, rid in enumerate(state.rows()):
        cursor = ">" if idx == state.cursor else " "
        mark = "x" if rid in state.enabled else " "
        style = "" if rid in state.enabled else "\x1b[2m"
        accent = state.item_colors.get(rid)
        tag = (" [%s]" % accent) if accent else ""
        write(
            "%s [%s] %s%-16s %s%s\x1b[0m\r\n"
            % (cursor, mark, style, rid, state.labels[rid], tag)
        )
    # "colors: on"/"colors: off" stays a stable prefix -- lab needles and the
    # humans reading the pane both key off it; the scheme rides behind it.
    write("\r\ncolors: %s\r\n"
          % (("on (%s)" % state.scheme) if state.colors else "off"))


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
            state.cycle_colors()
            dirty = True
        elif key == "accent":
            if state.cycle_accent():
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
            elif ch in (b"a", b"A"):
                yield "accent"
            elif ch in (b"v", b"V"):
                yield "customize"
            elif ch in (b"q", b"Q", b"\x03"):
                yield "quit"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_keys_windows(getwch):
    """Key reader for the native Windows console, yielding the same tokens as
    read_keys_tty so run_picker cannot tell the platforms apart. No raw-mode
    dance: console reads are already unbuffered and unechoed. Special keys
    arrive as a two-read pair -- a '\\x00' or '\\xe0' prefix, then a scan code
    -- and the pair is consumed whole so an unknown special key can never fall
    through into the character bindings (the CSI parser's no-fallthrough rule,
    ported). Ctrl-C is normalized to cancel: under the default console mode it
    surfaces as KeyboardInterrupt out of getwch, and letting that escape would
    exit 1 -- the "defect, do not retry" code -- for a keypress this tool
    documents as cancel. getwch is injected (the caller passes msvcrt.getwch)
    so these semantics stay checkable off-Windows through a scripted console;
    a getwch that raises EOFError ends the stream -- the scripted console
    does, the real one never."""
    specials = {"H": "up", "P": "down", "K": "left", "M": "right"}
    while True:
        try:
            ch = getwch()
        except EOFError:
            return
        except KeyboardInterrupt:
            ch = "\x03"
        if ch in ("\x00", "\xe0"):
            try:
                scan = getwch()
            except (EOFError, KeyboardInterrupt):
                return
            yield specials.get(scan, "other")
        elif ch in ("\r", "\n"):
            yield "enter"
        elif ch == " ":
            yield "space"
        elif ch in ("c", "C"):
            yield "colors"
        elif ch in ("a", "A"):
            yield "accent"
        elif ch in ("v", "V"):
            yield "customize"
        elif ch in ("q", "Q", "\x1b", "\x03"):
            yield "quit"


def vt_flags_needed(mode_value):
    """(already_ok, mode_to_request) for a console output mode. 0x0004 is
    ENABLE_VIRTUAL_TERMINAL_PROCESSING; 0x0001, ENABLE_PROCESSED_OUTPUT, must
    accompany it -- without it the console writes VT sequences into the buffer
    literally instead of parsing them, and draw()'s \\r\\n discipline depends
    on it in its own right. So a mode with VT set but processed output clear
    is NOT accepted as already-ok. Pure on purpose: the ctypes path below can
    only execute on Windows, and this is the part of it whose wrong answer is
    a corrupted screen, so it has to be checkable everywhere."""
    want = 0x0001 | 0x0004
    return (mode_value & want == want, mode_value | want)


def enable_vt_output():
    """Best-effort switch of the Windows console to ANSI (VT) processing,
    which draw() and the renderer's colored output require. True when VT
    sequences will be honoured, False when this console cannot render the TUI
    (a pre-VT conhost). Never raises: anywhere without the Win32 console API
    -- every POSIX platform -- the honest answer is simply False, and no
    caller there needs it. -11 is STD_OUTPUT_HANDLE, named here because
    ctypes carries no symbolic constants for it; the mode flags live in
    vt_flags_needed."""
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)  # exists only on Windows
        if windll is None:
            return False
        kernel32 = windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ok, wanted = vt_flags_needed(mode.value)
        if ok:
            return True
        return bool(kernel32.SetConsoleMode(handle, wanted))
    except (AttributeError, OSError, ValueError):
        return False


def guard_terminal_against_signals(fd, config_path):
    """Make SIGINT, SIGQUIT, SIGTERM and SIGHUP put the pane back before they
    take effect. POSIX only; call once, before the picker changes anything about
    the terminal. Returns a list the caller may append zero-argument cleanups
    to; they run, best effort, on the way out.

    A finally is the wrong tool for these three. SIGTERM and SIGHUP kill a
    Python process without unwinding it, so no finally runs and the pane's
    shell inherits raw mode, no autowrap and a hidden cursor. A handler that
    RAISES so the finally blocks do run is not enough either: the exception
    lands wherever the main thread happens to be, and when that is inside a
    restoring finally the rest of that block is abandoned. So the handler
    does the whole restore itself and depends on nothing after it.

    SIGINT is the documented cancel key arriving as a signal, which is how a
    terminal delivers Ctrl-C until the reader reaches raw mode. It ends the
    run as a cancel -- exit 4, nothing written -- instead of a traceback and a
    signal death. SIGQUIT, SIGTERM and SIGHUP are re-delivered under the
    default disposition once the pane is back, so a caller's wait status is
    exactly what it was before this guard existed; only the pane differs.
    SIGQUIT is on the list because Ctrl-\\ is as reachable as Ctrl-C in the
    launch window, where the terminal still turns both into signals.

    The restore writes to the descriptor, never to sys.stdout: a handler can
    interrupt a buffered write, and re-entering that object raises. It uses
    TCSANOW because TCSADRAIN waits for pending output, and on SIGHUP the
    terminal may be gone, where that wait never ends. Every step tolerates
    failure for the same reason. These four are the ones a terminal or a
    caller actually sends. Any other signal can still strand a pane, SIGKILL
    above all because it cannot be caught; nothing here claims otherwise."""
    import signal
    import termios

    saved = termios.tcgetattr(fd)
    armed = (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP)
    cleanups = []

    def handler(signum, _frame):
        # one restore per run: a second signal must not interrupt this one
        for sig in armed:
            signal.signal(sig, signal.SIG_IGN)
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except (termios.error, OSError):
            pass
        leave = SCREEN_LEAVE
        if signum == signal.SIGINT:
            leave += "\x1b[2J\x1b[Hcancelled -- %s untouched\n" % config_path
        try:
            os.write(1, leave.encode("utf-8", "replace"))
        except OSError:
            pass
        for cleanup in cleanups:
            try:
                cleanup()
            except Exception:  # a cleanup must never keep the process alive
                pass
        if signum == signal.SIGINT:
            # SystemExit, not os._exit: unwinding is what kills a renderer
            # child still in flight (subprocess.run does it on any exception).
            # Nothing the unwinding skips matters now -- the pane is back.
            sys.exit(4)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in armed:
        signal.signal(sig, handler)
    return cleanups


def show(node, js, cfg_path, probe_path, write, explicit_payload=False):
    """One-shot dump: registry, config, preview. Raises RuntimeError /
    OSError / ValueError upward; main prints them as clean exit-2 errors."""
    registry = fetch_registry(node, js)
    known = [e[0] for e in registry]
    schemes = fetch_schemes_or_none(node, js)
    items, colors, scheme, item_colors = load_config(
        cfg_path, known, schemes, fallback_ids=default_ids(registry))
    write("config: %s%s\n" % (cfg_path, "" if os.path.exists(cfg_path) else " (absent -> defaults)"))
    for entry in registry:
        rid, label = entry[0], entry[1]
        mark = "x" if rid in items else " "
        pos = str(items.index(rid) + 1) if rid in items else "-"
        accent = item_colors.get(rid)
        tag = (" [%s]" % accent) if accent else ""
        write(" [%s] %-2s %-16s %s%s\n" % (mark, pos, rid, label, tag))
    write("colors: %s\n" % colors_report(colors, scheme))
    payload, sandbox, live, mtime = pick_payload(probe_path, explicit=explicit_payload)
    try:
        preview = render_preview(node, js, items, colors, payload, sandbox,
                                 scheme=scheme, item_colors=item_colors)
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
    ran = []

    def check(name, cond):
        ran.append(name)
        print("%s: %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    known = ["a", "b", "c"]
    registry = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]

    # normalize: unknown skipped, dupes dropped, order preserved
    check("normalize skips unknown + dupes", normalize(["c", "zz", "a", "c"], known) == ["c", "a"])
    check("normalize empty stays empty", normalize([], known) == [])

    # a sandboxed child: both names, because which one a platform reads differs.
    # Only the Windows runs can see the behaviour; this sees the helper anywhere.
    check("sandbox home: HOME and USERPROFILE are both redirected",
          home_env({"HOME": "/real", "KEEP": "1"}, "/sandbox")
          == {"HOME": "/sandbox", "USERPROFILE": "/sandbox", "KEEP": "1"})

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

        # load: absent -> all + colors on + first scheme + no overrides
        check("load absent -> defaults",
              load_config(cfg, known) == (known, True, "codex", {}))
        # the fallback belongs to the caller, who takes it from the renderer:
        # an absent file lands on it rather than on every known id
        check("load absent -> the handed-over fallback, not every known id",
              load_config(cfg, known, fallback_ids=["a", "c"])
              == (["a", "c"], True, "codex", {}))
        check("default ids: the flag decides, short rows count as default",
              default_ids([("a", "A", True, True), ("b", "B", True, False),
                           ("c", "C")]) == ["a", "c"])
        # load: broken -> defaults
        with open(cfg, "w") as fh:
            fh.write("not json")
        check("load broken -> defaults",
              load_config(cfg, known) == (known, True, "codex", {}))
        # load: a config whose items key is not a list keeps its other keys
        # and takes the same fallback for items
        with open(cfg, "w") as fh:
            json.dump({"items": "model", "colors": False}, fh)
        check("load non-list items -> the handed-over fallback, colors kept",
              load_config(cfg, known, fallback_ids=["a", "c"])
              == (["a", "c"], False, "codex", {}))
        # load: empty items honored, colors false parsed
        with open(cfg, "w") as fh:
            json.dump({"items": [], "colors": False}, fh)
        check("load honors empty + colors:false",
              load_config(cfg, known) == ([], False, "codex", {}))
        # save/load round-trip, atomic (no *.tmp residue anywhere in the dir --
        # the temp name is unique per writer, so a fixed-suffix probe is blind)
        save_config(cfg, ["b", "a"], True)
        check("save/load round-trip",
              load_config(cfg, known) == (["b", "a"], True, "codex", {}))
        check("save leaves no tmp residue", not [f for f in os.listdir(td) if ".tmp" in f])
        # scheme + item_colors round-trip; a hex spec the picker cannot cycle
        # must survive opaquely, and shape-invalid entries must drop one by
        # one, never the map
        save_config(cfg, ["a"], True, scheme="mono",
                    item_colors={"a": "#87afff", "b": "red"})
        check("scheme+item_colors round-trip",
              load_config(cfg, known, ("codex", "mono"))
              == (["a"], True, "mono", {"a": "#87afff", "b": "red"}))
        check("unknown scheme falls back to first known",
              load_config(cfg, known, ("codex",))[2] == "codex")
        # (an int KEY is untestable here: json serializes every object key to
        # a string, so a non-str key cannot reach load_config through a file)
        with open(cfg, "w") as fh:
            json.dump({"items": ["a"], "scheme": 7,
                       "item_colors": {"a": 3, "b": "blue"}}, fh)
        check("shape-invalid scheme and override entries drop cleanly",
              load_config(cfg, known, ("codex", "mono"))
              == (["a"], True, "codex", {"b": "blue"}))

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

        # colors ring: with several schemes, c walks every scheme then off
        # then re-enters at the first; with the single default scheme it
        # degrades to the old on/off toggle (pinned above by st3).
        st5 = PickerState(registry, ["a"], True,
                          schemes=("codex", "claude-code", "mono"))
        seen = [(st5.colors, st5.scheme)]
        for _ in range(4):
            st5.cycle_colors()
            seen.append((st5.colors, st5.scheme))
        check("colors ring walks schemes, off, then round",
              seen == [(True, "codex"), (True, "claude-code"), (True, "mono"),
                       (False, "mono"), (True, "codex")])

        # accent ring on a colorable row: default -> red, and a full lap
        # lands back on default (override removed, not set to None)
        reg3 = [("a", "A", True), ("b", "B", False)]
        st6 = PickerState(reg3, ["a", "b"], True)
        check("accent cycles the selected colorable row",
              st6.cycle_accent() and st6.item_colors == {"a": "red"})
        for _ in range(len(ACCENT_RING) - 1):
            st6.cycle_accent()
        check("a full accent lap clears the override",
              st6.item_colors == {})
        # non-colorable row: refused, no override, no dirty signal
        st6.cursor = 1
        check("accent refused on a non-colorable row",
              st6.cycle_accent() is False and st6.item_colors == {})
        # a hex spec from the config file re-enters the ring at its start
        st7 = PickerState(reg3, ["a"], True, item_colors={"a": "#87afff"})
        st7.cycle_accent()
        check("hex override re-enters the ring at red",
              st7.item_colors == {"a": "red"})

        # the accent key through the real state machine: paint row a, save
        st8 = PickerState(reg3, ["a", "b"], True)
        outcome8 = run_picker(st8, iter(["accent", "accent", "enter"]),
                              lambda _s: ("p", "fixture"), lambda _s: None)
        check("scripted run saves an accented row",
              outcome8 == "saved" and st8.item_colors == {"a": "green"})
        # draw shows the override tag beside the row and the scheme in the
        # colors line
        frames = []
        draw(st8, "p", "fixture", frames.append)
        joined = "".join(frames)
        check("draw tags the accented row and names the scheme",
              "[green]" in joined and "colors: on (codex)" in joined)
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

        # Renderer resolution across both shipping layouts, on fixtures rather
        # than on whatever this machine happens to have installed. Fixtures are
        # the point: the regression these guard shipped green because every
        # renderer-dependent case below SKIPPED when resolution missed, so a
        # check that resolution is RIGHT has to be one that cannot skip.
        clone_js = os.path.join(td, "clone", "statusline.js")
        installed_js = os.path.join(td, "installed", ".claude", "statusline.js")
        absent_sibling = os.path.join(td, "installed", ".claude", "tools",
                                      "statusline.js")
        for made in (clone_js, installed_js):
            os.makedirs(os.path.dirname(made), exist_ok=True)
            open(made, "w").close()
        check("resolve_js: a clone takes the sibling renderer",
              resolve_js({}, (clone_js, installed_js)) == clone_js)
        check("resolve_js: an install with no sibling falls through to ~/.claude",
              resolve_js({}, (absent_sibling, installed_js)) == installed_js)
        check("resolve_js: STATUSLINE_JS outranks both layouts",
              resolve_js({"STATUSLINE_JS": clone_js},
                         (absent_sibling, installed_js)) == clone_js)
        check("resolve_js: an absent STATUSLINE_JS is honoured verbatim, never "
              "swapped for a renderer the caller did not name",
              resolve_js({"STATUSLINE_JS": absent_sibling},
                         (clone_js, installed_js)) == absent_sibling)
        check("resolve_js: an EMPTY STATUSLINE_JS is set, so it wins too -- a "
              "script that expanded an unset variable must not silently get "
              "the default renderer",
              resolve_js({"STATUSLINE_JS": ""}, (clone_js, installed_js)) == "")
        check("resolve_js: with nothing on disk it names the first candidate",
              resolve_js({}, (absent_sibling, absent_sibling + ".x"))
              == absent_sibling)
        # A directory is not a renderer. os.path.exists would take this one and
        # mask the real file behind it, which is the whole reason for isfile.
        dir_js = os.path.join(td, "dirshaped", "statusline.js")
        os.makedirs(dir_js, exist_ok=True)
        check("resolve_js: a DIRECTORY named statusline.js loses to the real "
              "renderer behind it",
              resolve_js({}, (dir_js, installed_js)) == installed_js)

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
                check("live registry: >=8 id+label+colorable rows",
                      len(reg) >= 8 and all(e[0] and e[1] for e in reg)
                      and any(e[2] for e in reg)
                      and not all(e[2] for e in reg))
                check("live schemes: codex first, claude-code and mono known",
                      fetch_schemes(node, DEFAULT_JS)[:1] == ["codex"]
                      and {"claude-code", "mono"}
                      <= set(fetch_schemes(node, DEFAULT_JS)))

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
                first_two = [e[0] for e in reg][:2]
                good = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--apply", ",".join(first_two), "--colors", "off",
                     "--config", e2e_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("apply e2e: saves exactly the selection, colors off",
                      good.returncode == 0
                      and load_config(e2e_cfg, [e[0] for e in reg])
                      == (first_two, False, "codex", {}))
                # --colors alone edits colors and keeps the saved items
                flip = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--colors", "on", "--config", e2e_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("apply e2e: --colors alone keeps items, flips colors",
                      flip.returncode == 0
                      and load_config(e2e_cfg, [e[0] for e in reg])
                      == (first_two, True, "codex", {}))

                # Fresh install: no config exists yet, and the first save may
                # name no items at all (--colors, --scheme, Enter in the TUI
                # on an untouched list). That save must write the bar the
                # renderer was already drawing: what no-config means is ONE
                # decision and the renderer owns it. Known-bad by
                # construction: a picker that falls back to every known id
                # saves the opt-in segments too, and the two bars differ.
                fresh_cfg = os.path.join(td, "fresh-cfg.json")
                fresh = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--colors", "on", "--config", fresh_cfg,
                     "--js", DEFAULT_JS],
                    capture_output=True, text=True)

                def _bar(config_path):
                    bar_env = dict(os.environ)
                    bar_env.pop("STATUSLINE_PAYLOAD_DUMP", None)
                    home_env(bar_env, td)
                    bar_env["STATUSLINE_CONFIG"] = config_path
                    return subprocess.run(
                        [node, DEFAULT_JS],
                        input=json.dumps(FIXTURE_PAYLOAD).encode("utf-8"),
                        capture_output=True, env=bar_env,
                        timeout=PREVIEW_TIMEOUT).stdout

                bare = _bar(os.path.join(td, "no-such-config.json"))
                check("fresh install: a save naming no items keeps the bar "
                      "the renderer already draws",
                      fresh.returncode == 0 and bool(bare)
                      and _bar(fresh_cfg) == bare)

                # ── the renderer's own contract, through the real file ──
                # Each check below was observed failing on the renderer as it
                # stood before the behaviour it names existed.
                def _render(payload, config=None, extra_env=None, cwd=None):
                    r_env = dict(os.environ)
                    for name in ("STATUSLINE_PAYLOAD_DUMP", "NO_COLOR"):
                        r_env.pop(name, None)
                    home_env(r_env, td)
                    cfg_file = os.path.join(td, "render-cfg.json")
                    if config is None:
                        cfg_file = os.path.join(td, "no-such-config.json")
                    else:
                        with open(cfg_file, "w") as cfh:
                            json.dump(config, cfh)
                    r_env["STATUSLINE_CONFIG"] = cfg_file
                    r_env.update(extra_env or {})
                    return subprocess.run(
                        [node, DEFAULT_JS],
                        input=json.dumps(payload).encode("utf-8"),
                        capture_output=True, env=r_env, cwd=cwd,
                        timeout=PREVIEW_TIMEOUT)

                def _payload(**changes):
                    fresh_payload = json.loads(json.dumps(FIXTURE_PAYLOAD))
                    fresh_payload.pop("workspace", None)
                    fresh_payload.update(changes)
                    return fresh_payload

                plain = {"items": ["directory", "model"], "colors": False}

                # payload probe: the off spellings are OFF. "0" used to be a
                # truthy string, which turned the probe on and wrote the
                # payload into a file named 0 in the working directory. The
                # two positive arms prove the variable reaches the renderer,
                # without which the negative arm would pass for free.
                dump_cwd = os.path.join(td, "dump-cwd")
                os.makedirs(dump_cwd)
                default_probe = os.path.join(
                    td, ".claude", "statusline-payload-last.json")
                os.makedirs(os.path.dirname(default_probe), exist_ok=True)
                for off in ("0", "false", "No", "off", "relative-name.json"):
                    _render(_payload(), plain,
                            {"STATUSLINE_PAYLOAD_DUMP": off}, cwd=dump_cwd)
                check("payload probe: off spellings and relative names write nothing",
                      os.listdir(dump_cwd) == []
                      and not os.path.exists(default_probe))
                _render(_payload(), plain,
                        {"STATUSLINE_PAYLOAD_DUMP": "1"}, cwd=dump_cwd)
                abs_probe = os.path.join(td, "abs-probe.json")
                _render(_payload(), plain,
                        {"STATUSLINE_PAYLOAD_DUMP": abs_probe}, cwd=dump_cwd)
                check("payload probe: 1 writes the default location, an "
                      "absolute path writes there",
                      os.path.exists(default_probe) and os.path.exists(abs_probe)
                      and os.listdir(dump_cwd) == [])

                # linked worktree whose .git file holds a RELATIVE gitdir: it
                # is relative to the worktree, not to the renderer's cwd
                wt = os.path.join(td, "wt-fixture", "tree")
                wt_git = os.path.join(td, "wt-fixture", "real.git", "worktrees", "tree")
                os.makedirs(wt)
                os.makedirs(wt_git)
                with open(os.path.join(wt, ".git"), "w") as gfh:
                    gfh.write("gitdir: ../real.git/worktrees/tree\n")
                with open(os.path.join(wt_git, "HEAD"), "w") as gfh:
                    gfh.write("ref: refs/heads/feature-x\n")
                wt_out = _render(_payload(cwd=wt),
                                 {"items": ["git-branch"], "colors": False},
                                 cwd=dump_cwd).stdout.decode("utf-8", "replace")
                check("worktree: a relative gitdir resolves against the worktree",
                      wt_out == "tree(feature-x)")

                # a reader that goes away first must not earn a stack trace
                gone = subprocess.Popen(
                    [node, DEFAULT_JS], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                gone.stdout.close()
                gone.stdin.write(json.dumps(_payload()).encode("utf-8"))
                gone.stdin.close()
                gone_err = gone.stderr.read()
                gone.stderr.close()
                check("closed stdout: exits 0 with nothing on stderr",
                      gone.wait(timeout=PREVIEW_TIMEOUT) == 0 and gone_err == b"")

                # one field of the wrong type costs one segment, not the bar
                bad_field = _render(
                    _payload(cwd=123),
                    {"items": ["git-branch", "model", "context"], "colors": False},
                ).stdout.decode("utf-8", "replace")
                check("a builder that throws: that segment is marked, the rest renders",
                      "statusline error" not in bad_field
                      and bad_field.startswith("git-branch!|")
                      and "Fable5" in bad_field and "83K/1M" in bad_field)

                # the error line is part of the bar, so NO_COLOR and
                # colors:false reach it too
                unparseable = subprocess.run(
                    [node, DEFAULT_JS], input=b"not json", capture_output=True,
                    env=home_env(
                        dict(os.environ, NO_COLOR="1",
                             STATUSLINE_CONFIG=os.path.join(td, "no-such.json")),
                        td),
                    timeout=PREVIEW_TIMEOUT).stdout
                check("error line: NO_COLOR strips it like the rest of the bar",
                      unparseable.startswith(b"statusline error:")
                      and b"\x1b" not in unparseable)

                # --show is the confirm step of the slash command, so it has
                # to name a saved scheme even while colors are off
                show_cfg = os.path.join(td, "show-cfg.json")
                save_config(show_cfg, ["model"], False, "mono")
                shown = subprocess.run(
                    [sys.executable, os.path.abspath(__file__), "--show",
                     "--config", show_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("show: names the saved scheme even with colors off",
                      shown.returncode == 0 and "colors: off" in shown.stdout
                      and "mono" in shown.stdout)

                # control bytes in a name never reach the terminal
                hostile = _render(
                    _payload(cwd="/srv/ev\x1b]0;owned\x07il\nna\x9bme"),
                    {"items": ["directory"], "colors": False},
                ).stdout.decode("utf-8", "replace")
                check("control bytes: stripped from rendered text",
                      hostile == "ev]0;ownedilname")

                # the filesystem root has a name too
                root_out = _render(_payload(cwd="/"), plain
                                   ).stdout.decode("utf-8", "replace")
                check("root directory: rendered as /, not as an empty segment",
                      root_out.startswith("/|"))

                # NO_COLOR, per no-color.org: present and non-empty turns
                # colour off; empty is the same as unset
                coloured = {"items": ["directory", "model"], "colors": True}
                nc_on = _render(_payload(), coloured, {"NO_COLOR": "1"}).stdout
                nc_empty = _render(_payload(), coloured, {"NO_COLOR": ""}).stdout
                check("NO_COLOR: non-empty strips colour, empty does not",
                      b"\x1b" not in nc_on and bool(nc_on)
                      and b"\x1b[" in nc_empty)

                # a save into a directory that does not exist yet creates it:
                # the alternative was exit 2 naming a TEMP file the user never
                # asked for, which says nothing about what to fix
                deep_cfg = os.path.join(td, "not-yet", "deeper", "cfg.json")
                try:
                    save_config(deep_cfg, ["model"], True)
                    deep_ok = json.load(open(deep_cfg)).get("items") == ["model"]
                except OSError:
                    deep_ok = False
                check("save: a missing config directory is created", deep_ok)

                # --scheme alone is a save of exactly that value, so the report
                # has to name it even while colors are off
                quiet_cfg = os.path.join(td, "quiet-cfg.json")
                quiet = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--scheme", "mono", "--colors", "off",
                     "--config", quiet_cfg, "--js", DEFAULT_JS],
                    capture_output=True, text=True)
                check("save report: names the scheme even with colors off",
                      quiet.returncode == 0 and "colors: off" in quiet.stdout
                      and "mono" in quiet.stdout)

                # no renderer in EITHER layout: the refusal names every place
                # it looked, not just the first
                bare_home = os.path.join(td, "bare-home")
                lone_dir = os.path.join(td, "lone-tools")
                os.makedirs(bare_home)
                os.makedirs(lone_dir)
                lone = os.path.join(lone_dir, "statusline_picker.py")
                shutil.copy2(os.path.abspath(__file__), lone)
                lone_env = dict(os.environ)
                lone_env.pop("STATUSLINE_JS", None)
                home_env(lone_env, bare_home)
                missing = subprocess.run(
                    [sys.executable, lone, "--show"],
                    capture_output=True, text=True, env=lone_env)
                check("missing renderer: the refusal names both layouts it tried",
                      missing.returncode == 2
                      and os.path.join(lone_dir, "statusline.js") in missing.stderr
                      and os.path.join(bare_home, ".claude", "statusline.js")
                      in missing.stderr)

                # the accent ring against the renderer that judges it. Under
                # mono the scheme emits no escape bytes at all, so an escape
                # on the line means the override was honoured and its absence
                # means the renderer dropped the name. The bogus name is the
                # known-bad arm: without it a ring of anything would pass.
                def _ring_honoured(names):
                    for name in names:
                        bar = _render(
                            _payload(),
                            {"items": ["directory"], "colors": True,
                             "scheme": "mono",
                             "item_colors": {"directory": name}}).stdout
                        if b"\x1b[" not in bar:
                            return False
                    return True

                check("accent ring: the renderer honours every name on it",
                      _ring_honoured(ACCENT_RING[1:]))
                check("accent ring: a name the renderer does not know is caught",
                      not _ring_honoured(("orange",)))

                # Renderer skew: a renderer that answers --segments but not
                # --schemes (an older statusline.js beside a newer picker)
                # must degrade, not brick. Contract: implicit paths proceed
                # and PRESERVE the stored scheme string verbatim; only an
                # EXPLICIT --scheme, which cannot be validated, is fatal.
                skew_js = os.path.join(td, "skew.js")
                with open(skew_js, "w") as fh:
                    fh.write(
                        "if (process.argv.includes('--segments')) {"
                        "process.stdout.write(JSON.stringify("
                        "[{id:'alpha',label:'Alpha',colorable:true},"
                        "{id:'beta',label:'Beta'}])); process.exit(0); }\n"
                        "if (process.argv.includes('--schemes')) {"
                        "process.stderr.write('unknown flag'); process.exit(1); }\n"
                        "process.stdin.resume();"
                        "process.stdin.on('end', () => process.stdout.write('bar'));\n")
                skew_cfg = os.path.join(td, "skew-cfg.json")
                with open(skew_cfg, "w") as fh:
                    json.dump({"items": ["alpha"], "scheme": "zebra"}, fh)
                try:
                    skew_load = load_config(skew_cfg, ["alpha", "beta"], None)
                except TypeError:  # observed red 2026-09-01: `in None` raised
                    skew_load = None
                check("skew: unknowable scheme offer preserves the stored string",
                      skew_load is not None and skew_load[2] == "zebra")
                skew_apply = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--apply", "beta,alpha", "--config", skew_cfg,
                     "--js", skew_js],
                    capture_output=True, text=True)
                check("skew: plain --apply saves and keeps the stored scheme",
                      skew_apply.returncode == 0
                      and json.load(open(skew_cfg)).get("scheme") == "zebra"
                      and json.load(open(skew_cfg)).get("items")
                      == ["beta", "alpha"])
                skew_strict = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     "--scheme", "mono", "--config", skew_cfg,
                     "--js", skew_js],
                    capture_output=True, text=True)
                check("skew: explicit --scheme is fatal when unvalidatable",
                      skew_strict.returncode == 2
                      and "cannot validate --scheme" in skew_strict.stderr)
                # --schemes must answer with a LIST of scheme names. A string
                # is also valid JSON and iterates per character, a dict per
                # key -- either would let --scheme validate against garbage
                # the renderer never offered.
                shape_js = os.path.join(td, "shape.js")
                with open(shape_js, "w") as fh:
                    fh.write(
                        "if (process.argv.includes('--schemes')) {"
                        "process.stdout.write(JSON.stringify('abc'));"
                        "process.exit(0); }\n")
                try:
                    shape_got = fetch_schemes(node, shape_js)
                except RuntimeError:
                    shape_got = None
                check("schemes fetch rejects a non-list JSON answer",
                      shape_got is None)
                # the degrade ring built from the preserved scheme: c must
                # cycle scheme -> off -> scheme without ever renaming it
                st_deg = PickerState([("a", "A", True)], ["a"], True,
                                     scheme="zebra", schemes=("zebra",))
                deg_walk = [(st_deg.colors, st_deg.scheme)]
                for _ in range(2):
                    st_deg.cycle_colors()
                    deg_walk.append((st_deg.colors, st_deg.scheme))
                check("degrade ring: preserved scheme cycles to off and back",
                      deg_walk == [(True, "zebra"), (False, "zebra"),
                                   (True, "zebra")])
            else:
                # Not a SKIP. The renderer ships WITH this picker, so node being
                # present while the renderer is not means the install is broken,
                # and the ten cases below are exactly the ones that would say so.
                # Skipping them shrinks the run and still prints PASSED -- which
                # is how a renderer this picker could not reach shipped green.
                print("tried: %s" % ", ".join(JS_CANDIDATES))
                check("renderer reachable at %s" % DEFAULT_JS, False)
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

        def _watchdog(seconds):
            # Fires after `seconds` and then every second until it is switched
            # off. The exception it raises unwinds through the reader's own
            # restore, which is a second blocking call: a one-shot alarm was
            # spent by then, and the selftest hung instead of failing (observed
            # 2026-09-17 on macos-26-arm64, Python 3.8 and 3.14).
            signal.setitimer(signal.ITIMER_REAL, seconds, 1)

        def _watchdog_off():
            signal.setitimer(signal.ITIMER_REAL, 0)

        close_watchdog = 5
        master, slave = os.openpty()
        gen = None
        before = None
        old_handler = signal.signal(signal.SIGALRM, _alarm)
        _watchdog(10)
        try:
            # No echo on this fixture. The first key is written before the
            # reader has entered raw mode, so a cooked slave would echo it, and
            # nothing here reads the master. macOS holds raw entry with
            # TCSADRAIN until that echo is read, which is never; Linux does not
            # wait. A real terminal reads its side all the time.
            quiet = _termios.tcgetattr(slave)
            quiet[3] &= ~_termios.ECHO
            _termios.tcsetattr(slave, _termios.TCSANOW, quiet)
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
            os.write(master, b"a")
            check("pty reader: a yields accent", next(gen) == "accent")
            os.write(master, b"A")
            check("pty reader: A yields accent", next(gen) == "accent")
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
            # close() is where the reader restores the terminal on every run that
            # did not time out, so the watchdog has to cover it as well. Switched
            # off first, a restore that blocks hung the selftest with all eleven
            # key checks green.
            close_returned = True
            try:
                _watchdog(close_watchdog)
                if gen is not None:
                    gen.close()
            except TimeoutError:
                close_returned = False
            finally:
                _watchdog_off()
            signal.signal(signal.SIGALRM, old_handler)
            after = _termios.tcgetattr(slave) if before is not None else None
            os.close(master)
            os.close(slave)
        check("pty reader: the restore on close returns", close_returned)

        def _settings(attrs):
            # PENDIN is the kernel's own note that typed-ahead input waits to be
            # re-read. On macOS it comes back raised after every raw round trip,
            # typed input or not (measured on hosted macos-26-arm64, 2026-09-17;
            # Linux returns the flags identical), so it is state the reader
            # cannot put back, not a setting it left behind. Everything else has
            # to come back exactly.
            settings = list(attrs)
            settings[3] &= ~getattr(_termios, "PENDIN", 0)
            # tcgetattr hands VMIN and VTIME back as ints while ICANON is clear
            # and as one-byte strings while it is set: same value, other type
            settings[6] = [bytes([c]) if isinstance(c, int) else c
                           for c in settings[6]]
            return settings

        restored = before is not None and _settings(after) == _settings(before)

        def _difference(field, was, now):
            if field == "cc":
                return "cc " + ", ".join(
                    "[%d] %r -> %r" % (slot, w, n)
                    for slot, (w, n) in enumerate(zip(was, now)) if w != n)
            return "%s %#x -> %#x" % (field, was, now)

        differs = ""
        if before is not None and not restored:
            fields = ("iflag", "oflag", "cflag", "lflag", "ispeed", "ospeed", "cc")
            differs = " -- differs in " + "; ".join(
                _difference(field, was, now)
                for field, was, now in zip(fields, _settings(before), _settings(after))
                if was != now)
        check("pty reader: termios restored on close, PENDIN aside" + differs,
              restored)

        # Launch-window guard. The byte must be INGESTED before raw entry, and
        # its echo on the master is the proof: a byte still in flight to the
        # line discipline survives TCSAFLUSH as well, and the guard would then
        # read green against the regression it exists for. Each arm gets its
        # own watchdog so a blocked arm is attributed to itself and cannot cut
        # the other short; every path through an arm checks its name once.
        import select as _select
        import tty as _tty

        launch_key = b"q"  # the quit binding: the key the launch window lost
        barrier_timeout = 2.0
        drop_window = 0.5
        arm_watchdog = 5

        def _ingested(master_fd):
            os.write(master_fd, launch_key)
            ready, _, _ = _select.select([master_fd], [], [], barrier_timeout)
            return bool(ready) and os.read(master_fd, 1) == launch_key

        def _reader_keeps(slave_fd):
            keys = read_keys_tty(_FdStdin(slave_fd))
            try:
                return next(keys, None) == "quit"
            finally:
                keys.close()

        def _flush_drops(slave_fd):
            _tty.setraw(slave_fd, _termios.TCSAFLUSH)
            ready, _, _ = _select.select([slave_fd], [], [], drop_window)
            return not ready

        def _launch_window_arm(name, observe):
            fds = []
            detail = ""
            verdict = False
            old = signal.signal(signal.SIGALRM, _alarm)
            _watchdog(arm_watchdog)
            try:
                fds.extend(os.openpty())
                if _ingested(fds[0]):
                    verdict = observe(fds[1])
                else:
                    detail = " -- echo barrier timed out"
            except TimeoutError:
                verdict, detail = False, " -- blocked past the watchdog"
            except OSError as exc:
                verdict, detail = False, " -- pty fixture failed: %s" % exc
            finally:
                _watchdog_off()
                signal.signal(signal.SIGALRM, old)
                for fd in fds:
                    os.close(fd)
            check(name + ("" if verdict else detail), verdict)

        _launch_window_arm("pty reader: launch-window key survives raw entry",
                           _reader_keeps)
        _launch_window_arm("pty fixture: TCSAFLUSH drops the pre-typed byte "
                           "(arms discriminate)", _flush_drops)
    else:
        print("SKIP: pty reader checks (no os.openpty)")

    # windows console driver: same token language as the tty reader, pinned
    # through a scripted console so the mapping is checked on EVERY platform
    # -- including the POSIX boxes where msvcrt itself cannot exist. "KBI"
    # scripts a Ctrl-C: the real console surfaces it as KeyboardInterrupt out
    # of getwch, and the reader must fold it into cancel rather than let it
    # escape as exit 1.
    def scripted_console(script):
        it = iter(script)

        def getwch():
            item = next(it, None)
            if item is None:
                raise EOFError
            if item == "KBI":
                raise KeyboardInterrupt
            return item

        return getwch

    win_script = ["\xe0", "H", "\x00", "P", "\xe0", "K", "\xe0", "M",
                  "\xe0", "G", " ", "\r", "c", "C", "a", "A", "v", "x", "q",
                  "\x1b", "KBI", "\x03"]
    check("windows reader: full token mapping",
          list(read_keys_windows(scripted_console(win_script)))
          == ["up", "down", "left", "right", "other", "space", "enter",
              "colors", "colors", "accent", "accent", "customize", "quit",
              "quit", "quit", "quit"])
    check("windows reader: stream ends cleanly at console EOF",
          list(read_keys_windows(scripted_console([]))) == [])
    check("windows reader: EOF inside a special-key pair ends, no fallthrough",
          list(read_keys_windows(scripted_console(["\xe0"]))) == [])

    # the driver end-to-end: a scripted console drives the real state machine
    # to a save -- toggle a off, cursor down to c, toggle c on, save
    def _stub_previewer(_st):
        return "bar", "label"

    def _sink(_s):
        return None

    st_win = PickerState(registry, ["a", "b"], True)
    outcome_win = run_picker(
        st_win,
        read_keys_windows(scripted_console([" ", "\xe0", "P", " ", "\r"])),
        _stub_previewer, _sink)
    check("windows reader drives run_picker to a save",
          outcome_win == "saved" and st_win.enabled == ["b", "c"])

    vt = enable_vt_output()
    check("enable_vt_output returns a bool, never raises", vt in (True, False))
    if not sys.platform.startswith("win"):
        check("enable_vt_output is False off-Windows", vt is False)
    # the flag decision is pure so the corrupted-screen case is pinned here,
    # off-Windows included: VT (0x0004) WITHOUT processed output (0x0001) is
    # an invalid configuration the console writes sequences into literally,
    # so it must not be accepted as already-ok
    check("vt flags: default console mode (0x0003) needs the request",
          vt_flags_needed(0x0003) == (False, 0x0007))
    check("vt flags: VT without processed output is not accepted",
          vt_flags_needed(0x0004) == (False, 0x0005))
    check("vt flags: both set is already ok, request unchanged",
          vt_flags_needed(0x0007) == (True, 0x0007))

    print("---")
    print("SELFTEST %s (%d failures)" % ("PASSED" if not failures else "FAILED", len(failures)))
    print("statusline_picker selftest: %d/%d passed" % (len(ran) - len(failures), len(ran)))
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
    ap.add_argument("--scheme", metavar="NAME",
                    help="set the color scheme when saving (alone or with "
                         "--apply/--colors); names come from the renderer's "
                         "--schemes list, unknown ones are refused with exit 2")
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
    # isfile, not exists, for the same reason resolve_js uses it: a directory
    # here would pass the guard and hand node something it cannot run. An empty
    # path gets a readable stand-in so a script that expanded an unset variable
    # does not produce a message that trails off into nothing.
    if not os.path.isfile(args.js):
        # When the path came from resolution rather than from the caller,
        # resolve_js hands back the first candidate only so there is a path to
        # name -- and naming it alone sends an installed user to a clone they
        # do not have. Say every place that was tried.
        tried = ""
        if args.js == DEFAULT_JS and "STATUSLINE_JS" not in os.environ:
            tried = " (looked in: %s)" % ", ".join(JS_CANDIDATES)
        print("error: renderer not found: %s%s"
              % (args.js or "(empty path)", tried), file=sys.stderr)
        sys.exit(2)

    if args.show and (args.apply is not None or args.colors or args.scheme is not None):
        print("error: --show does not combine with --apply/--colors/--scheme "
              "(one reads, the others write)", file=sys.stderr)
        sys.exit(2)

    if args.show:
        try:
            show(node, args.js, args.config, args.payload, sys.stdout.write,
                 explicit_payload=(args.payload != DEFAULT_PROBE))
        except (RuntimeError, OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    if args.apply is not None or args.colors or args.scheme is not None:
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
        known = [e[0] for e in registry]
        try:
            schemes = fetch_schemes(node, args.js)
            schemes_err = None
        except RuntimeError as exc:
            # degrade, except under an explicit --scheme (checked below):
            # a plain --apply/--colors must survive renderer skew, and
            # load_config(None) preserves the stored scheme verbatim
            schemes, schemes_err = None, exc
        items, colors, scheme, item_colors = load_config(
            args.config, known, schemes, fallback_ids=default_ids(registry))
        if args.apply is not None:
            try:
                items = parse_apply_items(args.apply, known)
            except ValueError as exc:
                print("error: %s" % exc, file=sys.stderr)
                sys.exit(2)
        if args.colors:
            colors = args.colors == "on"
        if args.scheme is not None:
            # strict like --apply: an explicit instruction silently repaired
            # would save a look the caller never asked for
            if schemes is None:
                print("error: cannot validate --scheme %r: %s"
                      % (args.scheme, schemes_err), file=sys.stderr)
                sys.exit(2)
            if args.scheme not in schemes:
                print("error: unknown scheme %r (renderer offers: %s)"
                      % (args.scheme, ", ".join(schemes)), file=sys.stderr)
                sys.exit(2)
            scheme = args.scheme
        try:
            save_config(args.config, items, colors, scheme, item_colors)
        except OSError as exc:
            print("error: could not save %s: %s" % (args.config, exc), file=sys.stderr)
            sys.exit(2)
        print("saved %s" % args.config)
        print("items:  %s" % (", ".join(items) or "(none -- empty line)"))
        print("colors: %s" % colors_report(colors, scheme))
        print("takes effect on the next statusline refresh; delete the file to restore defaults")
        sys.exit(0)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: interactive picker needs a TTY (use --show without one)",
              file=sys.stderr)
        sys.exit(2)

    # cleanups for the signal guard to run; stays empty where none is armed
    on_signal_exit = []

    # Probed here rather than left to the lazy import in read_keys_tty: an
    # ImportError there escapes main() and Python exits 1, which this tool
    # documents as "selftest failures -- a real defect, do not retry". A
    # platform without a usable console API is neither a defect nor
    # un-retryable, so it has to reach the environment code instead.
    try:
        import termios  # noqa: F401
    except ImportError:
        # Native Windows lands here: no termios, but the console API does the
        # same job through its own reader. Only a platform with NEITHER
        # module is out of options -- still environmental, never a defect.
        try:
            import msvcrt
        except ImportError:
            print("error: the interactive picker needs a POSIX terminal "
                  "(termios) or a Windows console (msvcrt); this platform "
                  "has neither. --show, --apply and --selftest work "
                  "anywhere.", file=sys.stderr)
            sys.exit(2)
        if not enable_vt_output():
            print("error: this console does not support ANSI (VT) output, "
                  "which the picker's screen needs; Windows 10+ conhost or "
                  "Windows Terminal required. --show and --apply work "
                  "anywhere.", file=sys.stderr)
            sys.exit(2)

        def make_keys():
            return read_keys_windows(msvcrt.getwch)
    else:
        def make_keys():
            return read_keys_tty(sys.stdin)

        # Armed here, before the first renderer call, because the launch
        # window is where Ctrl-C still arrives as a signal. This branch only:
        # the Windows console path changes no input mode, its reader already
        # folds Ctrl-C into the cancel token, and none of this could be
        # driven there.
        on_signal_exit = guard_terminal_against_signals(
            sys.stdin.fileno(), args.config)

    try:
        registry = fetch_registry(node, args.js)
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
    known = [e[0] for e in registry]
    schemes = fetch_schemes_or_none(node, args.js)
    items, colors, scheme, item_colors = load_config(
        args.config, known, schemes, fallback_ids=default_ids(registry))
    # Degrade ring is (scheme,), not ("codex",): PickerState normalizes a
    # scheme outside its ring to the ring's first entry, so a codex ring would
    # rewrite a preserved stored scheme on the next save -- measured
    # 2026-09-01: an untouched interactive save under skew turned a stored
    # "zebra" into "codex". The one-entry ring keeps c usable (scheme -> off
    # -> scheme) while never claiming schemes the offer could not confirm.
    state = PickerState(registry, items, colors, scheme=scheme,
                        item_colors=item_colors,
                        schemes=schemes or (scheme,))
    started_at = time.time()
    try:
        payload, sandbox, live_path, startup_mtime = pick_payload(
            args.payload, explicit=(args.payload != DEFAULT_PROBE))
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
    if sandbox:
        on_signal_exit.append(
            lambda: shutil.rmtree(sandbox, ignore_errors=True))

    def previewer(st):
        # only the rendering stays here; which bytes and which label is
        # preview_state, where a check can reach it
        data, label = preview_state(live_path, payload, startup_mtime,
                                    started_at, time.time())
        return render_preview(node, args.js, st.enabled, st.colors,
                              data, sandbox, scheme=st.scheme,
                              item_colors=st.item_colors), label

    def write_flush(s):
        sys.stdout.write(s)
        sys.stdout.flush()

    # SCREEN_ENTER / SCREEN_LEAVE say what changes and why. The finally covers
    # every exit that UNWINDS -- save, cancel, handoff, a raise; the exits
    # that do not unwind are guard_terminal_against_signals' business.
    # close() runs the reader's own finally -- the termios restore -- here and
    # now. Left to collection it ran when the generator's last reference
    # died, which is prompt under CPython's refcounting and unspecified
    # anywhere else.
    keys = make_keys()
    write_flush(SCREEN_ENTER)
    try:
        outcome = run_picker(state, keys, previewer, write_flush)
    finally:
        keys.close()
        write_flush(SCREEN_LEAVE)
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
            save_config(args.config, state.enabled, state.colors,
                        state.scheme, state.item_colors)
        except OSError as exc:
            print("error: could not save %s: %s" % (args.config, exc),
                  file=sys.stderr)
            sys.exit(2)
        print("saved %s" % args.config)
        print("items:  %s" % (", ".join(state.enabled) or "(none -- empty line)"))
        print("colors: %s" % colors_report(state.colors, state.scheme))
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
