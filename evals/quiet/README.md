# Quiet-mode report

This directory checks the coordinator's user-visible lines. It does not run the coordinator. The rules live in the Say section of `template/kit/gantry-coordinator.md` and in `template/kit/gantry-ship.md`.

A line is one of:

```
▸ started <task-slug> · <n> worker(s) · overlap clear
▸ PR #N opened · overlap clear · checks running
✓ landed #N · squash-merged · cleaned up
! blocked #N · <short reason> · <what happens next>
! pstack <installed> newer than tested 0.15.36 · continuing
? <question>
```

The pstack line is printed once when the installed plugin is newer than the version this release tested, then the run continues. An equal or older version prints nothing.

`<n> worker(s)` is `1 worker` or `2 workers` in a real line. A question is followed by numbered options indented two spaces. A landed line may have one indented detail line. The Say section forbids a plan recap and narration of heartbeats, waits, fetches, gate listings, and a pre-pr check that continued.

## Correctness

`python3 evals/quiet/score.py` reads `cases.json` and prints one failure per missed rule. Exit 0 when every check passes.

`fixtures/before.txt` is a synthetic wall of text in the old shape: a plan recap, a heartbeat confirmation, and a gate listing. It is not a recorded session. The checker must reject it.

`fixtures/after.txt` is the same moment as the fixed lines: started, pull request opened, landed, one overlap question, and one block. It must match `cases.json` and pass the checker.

```
python3 evals/quiet/score.py
python3 tests/quiet_eval_test.py
```
