# Duplicate-work check

`gantry overlap` gathers candidates. The coordinator classifies them. The implementation is `cmd_overlap`, `gather`, `classify`, and `verdict_of` in `template/kit/gantry_kit.py`. The coordinator instructions are the "Duplicate-work check" section of `template/kit/gantry-coordinator.md`.

## Sub-features

- Two checkpoints. `scoped`, after the bug is reproduced or the feature is scoped, before any worker starts. `pre-pr`, right before the ship worker opens the pull request.
- The script reads open pull requests in the target repo only (`gh pr list --json`, with changed files, branch, and body), remote branches with no pull request (`git ls-remote`), assigned open issues, and the run's own active workers. It ranks by exact shared path count and prints compact JSON. The `sees` field says pushed GitHub work only. Unpushed local branches are invisible.
- Classification. Shared files and the intent contained in the title or body, or the title contained in the intent, is `duplicate` (`stop`). Shared files otherwise is `related` (`flag`). No shared files is `unrelated` (`ignore`). `verdict_of` is `stop` when any label is stop or flag, otherwise `continue`.
- On `stop` or `flag` the coordinator asks with AskUserQuestion, includes the links, and does not continue on its own. The guard cannot see that question. The agent text requires it.
- `gantry overlap --run <id>` appends a `gathered` event. `--record classified|answered` appends the coordinator's judgment. `gantry_run.refresh` folds `overlap.jsonl` into `state.json`. A worker `gh pr create` is denied until the pre-pr checkpoint is cleared (`classified` with `continue`, or `answered` with `proceed`).
- The guard allows the coordinator to run `gantry overlap` and denies every other `gantry` subcommand.

## How to get to it (user POV)

The operator does not run it. The coordinator runs it at the two checkpoints and stops to ask when a candidate is a duplicate or related.

## Driving it with sandbox.sh

`$S doctor "$V"` classifies one duplicate, one same-file unrelated title, and one different-file candidate as stop, flag, and ignore.

The coordinator is allowed to launch the script:

```sh
$S guard "$V" coordinator repo Bash '{"command":"gantry overlap --checkpoint scoped --intent x --files a.py"}'
# ALLOW
```

`tests/overlap_test.py` is the fixture. It seeds one real duplicate pull request, one same-file-but-unrelated pull request, and one unrelated pull request, then asserts stop, flag, and ignore. It records the check, clears it only after `proceed`, denies `gh pr create` until then, and confirms that skipping `pr_create_allowed` makes the deny assertion fail.

```sh
python3 tests/overlap_test.py    # expects 2/2 passed
```

## Gotchas

- The check cannot see unpushed local branches. Say that when you report a clear result.
- `gh` and `git ls-remote` talk to the real origin outside this sandbox. The unit fixture stubs both.
- A worker with no task id or run id fails closed on `gh pr create`.
