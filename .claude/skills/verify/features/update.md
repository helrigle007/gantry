# Update the kit

`bin/gantry update [repo] [--tag <tag>]`. The implementation is `cmd_update` in `template/kit/gantry_kit.py`.

## Sub-features

- Refreshes `~/.gantry` from the recorded install source, or from the checkout that contains `template/kit`. `--tag` uses `git archive` of that tag (`template/kit` and `bin/gantry`) and does not move the operator's branch.
- Swaps `~/.gantry/current` with one rename after `python kit --version` succeeds. A release directory whose tree hash does not match the source is rebuilt.
- Re-merges user hooks and refreshes the coordinator agent. A foreign status line stays. A coordinator file that is not Gantry's stays.
- With a repo argument, refreshes the `pre-push` stub when it is absent or contains `gantry pre-push`. Leaves any other `pre-push` alone. Also adds `/.claude/worktrees/` to `.git/info/exclude` when that directory is not already excluded. The line is idempotent and is not committed. Update with no repo argument does not touch an exclude file.
- Prints `gantry: version <old> -> <new>`, then either `refreshed ~/.gantry` or `already matches the template`, then `gantry: no PR opened`.
- Does not commit in the target repo and does not run `gh pr create`. An update with no origin still exits 0.

## How to get to it (user POV)

After pulling a newer kit, the operator runs `gantry update`. Every registered repo picks up the new guard on the next tool call, because the hook path goes through `~/.gantry/current`.

## Driving it with tests/update_test.py

```sh
python3 tests/update_test.py    # expects N/N passed
```

The suite corrupts an installed kit file, checks the refresh, checks that a scratch file in the home survives, checks that no `gh` pull request is opened, pins `--tag` without moving the source HEAD, and leaves a foreign status line in place.

Proof: the printed version arrow, `gantry: no PR opened`, and a clean `git status` in any repo you passed.

## Gotchas

- `update` does not register a new repo. Run `install` for that.
- Nothing forces a version bump. The printed version is `VERSION` in `gantry_kit.py`.
- `core.hooksPath` still skips the stub. `doctor` mentions it. `update` does not refuse it.
