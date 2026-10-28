# Full Orca run

An objective goes to a coordinator in an Orca terminal on a fixture checkout you control. The coordinator creates an Orca Run, briefs workers in new-child worktrees, answers their `ask`s, and dispatches a ship worker that lands PRs. This tier opens PRs, pushes branches, and merges on GitHub. Get the operator's OK before starting, and tell them the objective you will use. `$F` is the fixture checkout and `$E` is `$V/evidence`.

## Sub-features

- Run creation and task DAG with `--deps`; stacked tasks use `--base-branch origin/<parent>`.
- Worker briefs name one playbook and the slash skills that task needs. The coordinator chooses the pre-PR skills per task and names them on the Skills line. An unnamed skill does not run. A spec starts with `/poteto-mode` only when the coordinator names a lead worker for a large or open-ended task, and that lead does not pass `/poteto-mode` to helpers it spawns.
- Parallel `worker-start --worktree new-child`; delivery confirmed by first heartbeat.
- Worker `orchestration ask` answered by `orchestration reply`, or gated to the human with `gate-create`.
- Background `~/.gantry/current/kit/gantry-mailbox wait` so the coordinator stays responsive (`scripts/ping-runs.py <repo> N`). Its daemon holds the Run's Orca waiter, so Orca never types a message notice plus Enter into the coordinator terminal. Proof: type a draft in the coordinator while workers report, and the draft stays unsent. Tier 1 install and guard coverage is [mailbox.md](mailbox.md). The coordinator also runs `gantry overlap` before the first worker and again before the ship worker opens a pull request. See [overlap.md](overlap.md).
- Ship worker: Shipping with `autoMerge: true`; Babysit `drive` plus a landing gate with `autoMerge: false`.
- On-demand cleanup: the coordinator scans every run when the user asks, and offers the same list after ship. It reports each item as `close`, `remove`, or `skip` with a reason. `gantry-cleanup` acts only on confirmed items, or on every finished item when the user said to just clean up.
- Worker subagent fan-out instead of Orca workers.

## How to get to it (user POV)

Operator opens an Orca terminal on the fixture's main checkout, types `gantry "<objective>"`, watches the Agent Dashboard, reads the coordinator's report with PR links.

## Driving it with Orca

Precheck: doctor lines in SKILL.md all pass, `command -v gantry` resolves, and `orca terminal list --json` shows no live terminal titled `gantry-verify-*` from someone else.

Start, recording the handle:

```sh
F=<fixture checkout>; E=$V/evidence; STAMP=$(date +%s)
orca terminal create --worktree path:$F --title gantry-verify-$STAMP \
  --command "gantry 'Add a truncate(text, n) helper to textkit with tests'" --json | tee $E/orca-terminal.json
H=$(jq -r '.result.terminal.handle' $E/orca-terminal.json)
```

Observe:

```sh
orca terminal read --terminal $H --json | jq -r '.result' > $E/coordinator-terminal.txt
orca orchestration run-list --json | jq '.result.runs[0]'          # newest Run; note its id as R
orca orchestration task-list --run $R --json > $E/tasks.json
orca orchestration worker-list --run $R --json > $E/workers.json
orca orchestration gate-list --run $R --json > $E/gates.json
(cd "$F" && gh pr list --state all --limit 10 --json number,title,state,headRefName,mergedAt) > $E/prs.json
```

Transcripts live under the coordinator project dir and each worker worktree's project dir. Copy the session `.jsonl` you need into `$E`.

To talk to the coordinator like the operator does: `orca terminal send --terminal $H --text "<message>" --enter --json`.

Ask it to clean up with `orca terminal send --terminal $H --text 'clean up' --enter --json`. The first reply is the `gantry cleanup --scan` list. A second message, `just clean up`, is `gantry cleanup --finished`. Confirm a subset by naming the task. Proof is that list, then the worktree gone or still skipped, and the main checkout still present. The coordinator does not dispatch `gantry-cleanup`. Tier 1 allow and deny payloads are in [guard.md](guard.md).

Proof: the Run's tasks reach `completed`, each PR has a `## Verification` section and a verifier verdict comment, and `git -C $F log origin/main` shows the landed commits in dependency order. An independent pull request is merged by `gantry ship` in the coordinator session (`gh pr merge --squash --match-head-commit` inside that command). A stack still has a ship task whose `task_title` is `gantry-ship`. An implementation worker told to merge its own PR gets "only the gantry-ship worker merges". The coordinator told to run `gh pr merge` itself gets "coordinator never runs gh pr merge".

Teardown removes only what this run created. Leave PRs, branches, and merge commits on GitHub. List them for the operator. Evidence in `$V/evidence` stays.

```sh
orca orchestration worker-list --run $R --terminal-state active --json    # dispatches still running in YOUR run
orca orchestration worker-stop --dispatch <id> --json                     # each one, only if the run is abandoned
orca terminal close --terminal $H --json
```

## Gotchas

- Workers branch from committed `main`, so a `template/` change reaches them only after it lands on fixture `main`. Run `<repo checkout>/bin/gantry update "$F"` and merge its PR before a run meant to prove a guard or agent change.
- A user-level hook runs in every worker. A pattern of `push --force` also matches `--force-with-lease` and blocks Shipping's restack. A stacked PR whose parent squash differs from its commits stalls there.
- `/usr/local/bin/orca` is unreadable for the user on the operator's Mac. `~/.local/bin/orca` fixes it. Workers fail at every `orca` call if that link is gone.
- New-child workers wedge on Claude's trust dialog unless `~/.claude.json` trusts the fixture's main checkout. `gantry install` writes that.
- `gate-resolve` works only from the Run's coordinator terminal or the Agent Dashboard. Resolve a gate by sending the coordinator a message.
- Orca's contract: absence of a heartbeat never authorizes stop or retry. Do not `worker-stop` a quiet worker mid-run to hurry verification. `worker-stop` in the teardown block is only for a run you are abandoning.
- A released `--worktree current` worker can stay open as `user_takeover` after `worker-release` (issue 6). Cleanup closes that terminal when worker-list still shows it settled, and does not remove the main checkout. If `terminal list` shows a handle that worker-list does not, the report names it and leaves it. That terminal may be the operator's, so the guard will not close it. The verify teardown above still closes only `$H`, the terminal this run created.
