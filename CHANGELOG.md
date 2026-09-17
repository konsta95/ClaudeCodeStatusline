# Changelog

Notable changes per release, newest first. Versions are git tags, and the dates live on the
tags and on the GitHub releases rather than here. Numbers in parentheses are pull requests.

## 0.2.0

### Added

- Colour schemes `codex`, `claude-code` and `mono`, per-component accent colours, and the
  granular `directory`, `branch` and `github` components. They are opt-in, so a bar with no
  config renders as before (#12).
- A native Windows console driver for the interactive picker: `msvcrt` key reads and VT output,
  speaking the same key-token language as the tty reader (#10).
- `node statusline.js --segments` reports a `default` flag per component, so the picker takes
  its no-config fallback from the renderer instead of holding its own (#17).
- A non-empty `NO_COLOR` turns the bar colour off (#19).
- Selftest: a launch-window key guard with a known-bad arm, a closing `N/M passed` receipt
  (#15), and checks that drive the real renderer (#19, #20).
- Labs: signal cases with a guard-removed control (#18), and an arm that can see renderer
  resolution in the installed layout (#14).

### Changed

- `STATUSLINE_PAYLOAD_DUMP` accepts `1`, `true`, `yes`, `on` for the default location, or an
  absolute path. `0`, `false`, `no`, `off`, an empty value and relative names leave the probe
  off (#19).
- The picker pane opens at the bottom of the terminal, with the cursor hidden while it owns
  the screen (#11).
- Saving creates a config directory that does not exist yet (#20).
- `lab/picker_lab.py` exits non-zero when any case reports a finding (#16).

### Fixed

- The picker fell back to every known component when no config existed, so a save that named
  no items wrote the opt-in components into a bar the user had not touched (#17).
- `SIGTERM`, `SIGHUP` and `SIGQUIT` left the terminal in raw mode with autowrap off and the
  cursor hidden. The pane is now restored before the signal takes effect (#18, #22).
- `Ctrl-C` pressed before the reader reached raw mode ended in a traceback. It is a cancel,
  exit `4` (#18).
- `STATUSLINE_PAYLOAD_DUMP=0` switched the probe on and wrote the payload into a file named
  `0` in the working directory (#19).
- A linked worktree with a relative `gitdir` lost its branch (#19).
- A reader that closed the pipe first produced an `EPIPE` stack trace and exit `1` (#19).
- One payload field of the wrong type replaced the whole bar with an error. The failing
  component is now marked `id!` and the rest renders (#19).
- Control bytes in a directory name or a payload string reached the terminal (#19).
- The filesystem root rendered as an empty component (#19).
- The renderer was not found from the installed layout, `~/.claude/tools/` with the renderer
  one level up (#13).
- `--scheme` alone reported nothing about the scheme while colours were off, and a missing
  renderer was reported by its first candidate path only (#20). `--show` names the saved
  scheme too (#22).
- The renderer error line ignored `NO_COLOR` and `"colors": false` (#22).

## 0.1.0

First release: the self-describing renderer, the interactive picker with live preview through
the real renderer, the `/statusline` slash command with AskUserQuestion popups and a tmux
pane, the exit-code contract, and the pty, exit-contract, carrier and failed-save labs.
