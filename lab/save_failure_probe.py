"""Does a FAILED save ever leave the config changed?

`commands/statusline.md` tells the caller that nothing was written for any
picker exit code other than `0`. Three of the four non-zero codes never reach
`save_config` at all -- `1` comes only from the selftest, `3` is the customize
handoff, `4` is cancel -- so the whole weight of that sentence rests on `2`,
which is the code a failed save exits with.

`save_config` writes through a unique temp name in the TARGET directory and
renames over the config, so the intent is that a failure unlinks its temp and
leaves the old bytes untouched. This measures that end to end rather than
reading it out of the source: a config with known bytes, a save forced to fail
by a mode-555 directory, Enter pressed to trigger it, and a byte-for-byte
comparison afterwards.

    python3 lab/save_failure_probe.py

Exit codes: 0 the guarantee held, 1 it did not (a real defect -- read the
output, do not retry), 2 the environment cannot answer the question. That last
one covers more than it looks: the picker has EIGHT routes to exit 2 and only
one of them is the failed save -- the other seven fire before `save_config` is
reached -- so a run that died on a missing node or an absent tty also arrives
here with the config untouched and no temp stranded, which is
indistinguishable from a clean pass on every other check this probe makes. It is
the save-specific diagnostic that tells the two apart.

Reuses `lab/exit_contract_lab.py`'s run_picker rather than reimplementing a pty
driver, so the probe and the lab agree on how the picker is driven.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load_lab():
    path = os.path.join(HERE, "exit_contract_lab.py")
    spec = importlib.util.spec_from_file_location("exit_contract_lab", path)
    if spec is None or spec.loader is None:
        print("probe error: could not load the lab as a module from %s -- the probe "
              "reuses\nits run_picker rather than reimplementing a pty driver, so "
              "there is nothing\nto fall back to." % path, file=sys.stderr)
        sys.exit(2)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except OSError as exc:
        # The None check above does NOT cover a missing file: for a path that
        # does not exist, spec_from_file_location still hands back a spec with
        # a perfectly good SourceFileLoader, and exec_module is where it
        # finally raises. Uncaught, that is an exit 1 -- "a real defect, do not
        # retry" -- for a file that simply is not there.
        print("probe error: could not read %s: %s\nThe probe reuses its run_picker "
              "rather than reimplementing a pty driver, so\nthere is nothing to fall "
              "back to." % (path, exc), file=sys.stderr)
        sys.exit(2)
    return mod


def main():
    if not hasattr(os, "geteuid"):
        # Windows. Not merely an unavailable API to route around: mode 0o555
        # does not make a directory unwritable there either, so the fixture this
        # probe is built on has no meaning on the platform. Calling geteuid
        # anyway would raise AttributeError and exit 1 -- "a real defect" -- for
        # what is only an unanswerable question.
        print("probe error: this platform has no os.geteuid. The unwritable-directory "
              "fixture\nthis probe depends on does not bind here either, so the "
              "question cannot be\nasked, let alone answered.", file=sys.stderr)
        return 2

    if os.geteuid() == 0:
        # Mode 0o555 does not bind at uid 0, so the save would SUCCEED and the
        # probe would report a pass it never measured. Refusing is the only
        # honest result available here.
        print("probe error: running as uid 0. The unwritable directory this probe "
              "depends on\ndoes not bind for root, so the save would succeed and the "
              "result would be vacuous.", file=sys.stderr)
        return 2

    lab = load_lab()
    work = None
    try:
        work = tempfile.mkdtemp(prefix="save-failure-probe.")
        cfgdir = os.path.join(work, "cfg")
        os.mkdir(cfgdir)
        cfg = os.path.join(cfgdir, "statusline-config.json")
        before = json.dumps({"items": ["model", "cwd"], "colors": True},
                            indent=1) + "\n"
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(before)
    except OSError as exc:
        # No picker has run at this point, so nothing has been measured. An
        # uncaught OSError from a full or read-only /tmp would still exit 1 and
        # name the picker as the culprit.
        print("probe error: could not build the probe workspace: %s\nThe fixture "
              "is a real directory with a real config in it, so there is\nnothing "
              "to fall back to." % exc, file=sys.stderr)
        if work is not None:
            print("working tree kept for inspection: %s" % work)
        return 2

    try:
        os.chmod(cfgdir, 0o555)
    except OSError as exc:
        # A filesystem that will not take the mode cannot host the fixture, and
        # an uncaught OSError here exits 1 -- "a real defect, do not retry" --
        # for a question that was never asked. Same class as the witness check
        # below, one step earlier: that one catches a mode that reports success
        # without binding, this one a mode that will not be set at all.
        print("probe error: could not make the config directory unwritable: %s\n"
              "The fixture this probe rests on cannot be built here, so the "
              "guarantee\ncannot be tested." % exc, file=sys.stderr)
        print("working tree kept for inspection: %s" % work)
        return 2

    try:
        # Prove the fixture binds BEFORE building a result on it. chmod can
        # report success and still not take -- an ACL, a mount option, a
        # filesystem with no POSIX modes -- and every one of those lets the save
        # SUCCEED while all three checks below still read green. Testing the
        # instrument is cheaper than explaining a vacuous pass later.
        witness = os.path.join(cfgdir, ".fixture-witness")
        try:
            open(witness, "w").close()
        except OSError:
            pass  # what the fixture is supposed to do
        else:
            os.unlink(witness)
            print("probe error: the config directory is still writable at mode 0o555, "
                  "so the save\nwould succeed and this probe would report a guarantee "
                  "it never tested.", file=sys.stderr)
            print("working tree kept for inspection: %s" % work)
            return 2

        rc, out = lab.run_picker(os.path.join(REPO, "statusline_picker.py"), cfg,
                                 keys=b"\r")
    finally:
        # Restored in a finally so a crash mid-probe does not leave an
        # undeletable tree behind. A failure to restore is reported and not
        # raised: the measurement has already happened by this point, so
        # letting it escape would turn a real answer into exit 1 -- and it
        # would do so from inside a finally, replacing whatever the try block
        # was already returning.
        try:
            os.chmod(cfgdir, 0o755)
        except OSError as exc:
            print("probe note: could not restore mode on %s: %s\nThe measurement "
                  "above still stands; the tree may need removing by hand."
                  % (cfgdir, exc), file=sys.stderr)

    try:
        with open(cfg, encoding="utf-8") as fh:
            after = fh.read()
        stranded = sorted(f for f in os.listdir(cfgdir) if f != os.path.basename(cfg))
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        # Attribution, and that is the whole distinction between this branch and
        # the next. The setup wrote cfg, chmod'd cfgdir and wrote a witness
        # inside it, so both existed with the right types when the picker
        # started, and r-x is enough to read and list afterwards even where the
        # restore to 0o755 failed. A config that is GONE, replaced by a
        # directory, or whose directory became a file is therefore the
        # after == before guarantee broken harder than a changed config -- this
        # probe's own assertion, not an environment it could not read. Filing it
        # as unmeasured would swallow the most severe failure the probe exists
        # to catch. The reverse error, blaming the picker for something outside
        # it, prints the kept tree and gets looked at.
        print("---")
        print("SAVE-FAILURE PROBE FAILED (1)")
        print("  - the picker removed or replaced the config state: %s" % exc)
        print("working tree kept for inspection: %s" % work)
        return 1
    except OSError as exc:
        # Everything else names no actor: a permission or I/O error on a path
        # that still exists and still has the right type. The mode restore is
        # reported rather than raised, so a fixture stuck at 0o555 reaches here.
        # An uncaught exception would exit 1, the code this file reserves for "a
        # real defect", so this reports 2 and keeps the tree instead of
        # asserting a verdict nothing measured.
        print("---")
        print("probe error: the post-run inspection could not read the config state "
              "back: %s\nThe picker ran, but nothing here observed whether the save "
              "held, so this is an\nunmeasured run and not a defect verdict."
              % exc, file=sys.stderr)
        print("working tree kept for inspection: %s" % work)
        return 2
    # Two predicates on purpose. The loose one is what a human wants to read;
    # the strict one is the assertion. They are not interchangeable: "error"
    # matches all eight of the picker's exit-2 routes, and asserting on it would
    # rebuild the exact hole this closes.
    diagnostic = next((l for l in out.splitlines()
                       if "could not save" in l or "error" in l), "(none)")
    reached_save = any("could not save" in l for l in out.splitlines())

    print("rc                                : %s   (2 = environment error, the "
          "documented save failure)" % rc)
    print("stderr                            : %s" % diagnostic.strip()[:110])
    print("the save was actually attempted   : %s" % reached_save)
    print("config byte-identical afterwards  : %s" % (after == before))
    print("temp files stranded beside it     : %r" % stranded)

    if rc == 2 and not reached_save:
        # Environment, not defect. save_config never ran, so returning 1 here
        # would report "a real defect -- do not retry" for a missing node.
        print("---")
        print("probe error: the picker exited 2 with no 'could not save' diagnostic, "
              "so it failed\nBEFORE reaching the save -- no node, no renderer, no tty, "
              "no termios, a config\nthat would not load. Every one of those leaves the "
              "config untouched and strands\nno temp, which is precisely what a clean "
              "pass looks like from out here. This run\nmeasured nothing about a failed "
              "save.", file=sys.stderr)
        print("working tree kept for inspection: %s" % work)
        return 2

    failures = []
    if rc != 2:
        failures.append("a failed save exited %s, not the documented 2" % rc)
    if after != before:
        failures.append("the config CHANGED under a failed save\n"
                        "    before %r\n    after  %r" % (before, after))
    if stranded:
        failures.append("a failed save stranded %r in the config directory" % stranded)

    print("---")
    if failures:
        print("SAVE-FAILURE PROBE FAILED (%d)" % len(failures))
        for f in failures:
            print("  - %s" % f)
        print("working tree kept for inspection: %s" % work)
        return 1

    try:
        shutil.rmtree(work)
    except OSError as exc:
        # PASSED is a statement about the picker and it has already been earned,
        # so a tree that will not delete does not retract it. It does have to be
        # said: ignore_errors=True left the workspace on disk under a clean
        # verdict, with no path printed to find it by.
        print("probe note: could not remove the working tree %s: %s"
              % (work, exc), file=sys.stderr)
    print("SAVE-FAILURE PROBE PASSED (0 failures)")
    print("A failed save leaves the config byte-identical and strands no temp, so")
    print("the documented claim holds for the PICKER's exit codes. It is the")
    print("CARRIER pseudo-codes that it does not cover -- see carrier_lab.py arm G.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
