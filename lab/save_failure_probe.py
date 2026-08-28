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
output, do not retry), 2 the environment cannot answer the question.

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
    spec.loader.exec_module(mod)
    return mod


def main():
    if os.geteuid() == 0:
        # Mode 0o555 does not bind at uid 0, so the save would SUCCEED and the
        # probe would report a pass it never measured. Refusing is the only
        # honest result available here.
        print("probe error: running as uid 0. The unwritable directory this probe "
              "depends on\ndoes not bind for root, so the save would succeed and the "
              "result would be vacuous.", file=sys.stderr)
        return 2

    lab = load_lab()
    work = tempfile.mkdtemp(prefix="save-failure-probe.")
    cfgdir = os.path.join(work, "cfg")
    os.mkdir(cfgdir)
    cfg = os.path.join(cfgdir, "statusline-config.json")
    before = json.dumps({"items": ["model", "cwd"], "colors": True}, indent=1) + "\n"
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(before)

    os.chmod(cfgdir, 0o555)
    try:
        rc, out = lab.run_picker(os.path.join(REPO, "statusline_picker.py"), cfg,
                                 keys=b"\r")
    finally:
        # Restored in a finally so a crash mid-probe does not leave an
        # undeletable tree behind.
        os.chmod(cfgdir, 0o755)

    with open(cfg, encoding="utf-8") as fh:
        after = fh.read()
    stranded = sorted(f for f in os.listdir(cfgdir) if f != os.path.basename(cfg))
    diagnostic = next((l for l in out.splitlines()
                       if "could not save" in l or "error" in l), "(none)")

    print("rc                                : %s   (2 = environment error, the "
          "documented save failure)" % rc)
    print("stderr                            : %s" % diagnostic.strip()[:110])
    print("config byte-identical afterwards  : %s" % (after == before))
    print("temp files stranded beside it     : %r" % stranded)

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

    shutil.rmtree(work, ignore_errors=True)
    print("SAVE-FAILURE PROBE PASSED (0 failures)")
    print("A failed save leaves the config byte-identical and strands no temp, so")
    print("the documented claim holds for the PICKER's exit codes. It is the")
    print("CARRIER pseudo-codes that it does not cover -- see carrier_lab.py arm G.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
