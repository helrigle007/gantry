# Ship and clean up

Read this file when every implementation task has settled with a pull request, or when the user asks to clean up. Run the commands below. Do not merge, push, delete a branch, or remove a worktree yourself.

Re-read `gantry.json` in the git common dir first. `autoMerge` defaults to true when the key is missing. Refresh the default branch the same way as before a code-writing `worker-start`.

Pass every id and pull number as a literal. Do not pass `--admin`, `--auto`, or `--force`.

## Independent pull request

When `autoMerge` is true, run this once per settled pull request whose base is the default branch:

`gantry ship <number-or-url> --task <task id>`

You may chain several of those with `;` in one Bash call. Read each JSON line. What you print for each line is the Say this to the user section. Do not restate the JSON.

- `landed`: checks were green, or the repo has no Actions workflows and no required status checks, so an empty rollup is not a failure. The pull request was squash-merged at that head, and the head was deleted only when this run recorded that branch and it is not the default branch. The task is marked completed.
- `blocked`: checks were red, pending, or failing; checks are configured but none registered after a short wait (`checks have not registered`); the pull request was closed unmerged; the head was the default branch; or this run has no record of the pull request. When the reason is `checks have not registered`, run `gantry ship` again. Do not ask and do not start a fix task. Any other blocked reason: do not clean up. Dispatch a fix worker for that pull request. Follow poteto-mode's Bug fix playbook for it. Base is that pull request's branch. Ownership is that pull request only. The fix worker pushes to the same branch. Ship again after it settles.
- `stack`: the base is not the default branch, or another open pull request targets this head. Do not retry `gantry ship`. Dispatch one ship worker for that stack. See Stacked pull requests.
- `held`: `autoMerge` is false. You should not have run the command.

## Stacked pull requests

Restack rewrites a child onto the parent's squash and pushes with `--force-with-lease`. That stays a `gantry-ship` worker. `gantry ship` does not restack and does not force-push.

Dispatch one worker for the stacked pull requests only, not for the independent ones `gantry ship` already landed:

`orca orchestration worker-start --spec '<spec>' --task-title gantry-ship --worktree new-child --base-branch origin/<default> --agent claude --json`

End with the same jq filter as a code-writing `worker-start`. `<pstack>` is the `GANTRY_PSTACK` path. The spec is:

```
You are a poteto-agent. Read <pstack>/skills/poteto-mode/SKILL.md in full before any work, including its Principles index. Do not match a playbook. Do not spawn a poteto-agent to run the playbook. Do not pass /poteto-mode to a helper.
Read <pstack>/skills/poteto-mode/playbooks/shipping.md and follow it for this stack. Use playbooks/babysit.md for CI failures and review threads.
Charter: <as in an implementation spec>
Land these PRs: <urls, bottom-to-top per stack>
<GANTRY WORKER RULES>
Dependency order: <parent -> child chains>
Report per PR in worker_done, one line each: landed with merge commit, or not landed with the verdict and reason. If a landing decision needs a human, ask the coordinator. The coordinator prints one Say line to the user and does not forward this report.
```

Give `gantry-ship` only to this worker and to a landing task. A pull request the ship worker reports as not landed because verification failed gets the same fix task as a `blocked` line above.

## autoMerge false

Do not run `gantry ship`. The guard denies it, and the command will not merge.

Dispatch one ship worker with this spec:

```
You are a poteto-agent. Read <pstack>/skills/poteto-mode/SKILL.md in full before any work, including its Principles index. Do not match a playbook. Do not spawn a poteto-agent to run the playbook. Do not pass /poteto-mode to a helper.
Read <pstack>/skills/poteto-mode/playbooks/babysit.md and follow it. Do not merge; autoMerge is false and the guard blocks merges.
Charter: <as in an implementation spec>
Babysit these PRs to merge-ready, drive mode: <urls>
<GANTRY WORKER RULES>
Report per PR in worker_done, one line each: merge-ready or blocked, with the reason. The coordinator prints one Say line to the user and does not forward this report.
```

After that worker settles, create a landing task with `task-create --task-title gantry-ship --spec 'Land <urls>'`, and put the Charter line under `Land <urls>` as in an implementation spec. Put a gate on it: `gate-create --task <land task> --question "PRs are merge-ready and autoMerge is false. Land them?" --options '["land (set autoMerge true first)","hold"]'`. Print that question as the Say line below, and keep one background `gantry wait` running. Nothing lands until the user resolves the gate and `autoMerge` reads true. Then run `gantry ship` for each independent pull request, or dispatch the stacked ship worker when a line comes back `stack`.

## Clean up

Run this when the user asks, including a bare "clean up", and after a landed ship when the user said to just clean up. Do not dispatch `gantry-cleanup`.

1. `gantry cleanup --scan` prints one JSON line per task. `op` is `remove`, `close`, or `skip`, with the reason. Read it. Do not paste it.
2. When the user said to just clean up, run `gantry cleanup --finished`. It acts on every line whose `op` is `remove` or `close`. Do not ask again.
3. Otherwise run `gantry cleanup <task>` only for tasks the user confirms. Several task ids may go in one command. The confirmation is the question line in Say this to the user.

The command refuses `--force`. It skips a dirty tree, an unmerged branch, commits that are not on the default branch, a running worker, the caller's cwd, and a worktree a live worker is in. A settled worker whose worktree is the main checkout has its terminal closed, and the checkout stays. A released `--worktree current` terminal that worker-list still shows as settled `user_takeover` is closed the same way. A removed or closed task is marked completed, and a settled dispatch is released.

A terminal that worker-list does not show is left open. It may be the operator's.

## Say this to the user

The coordinator prompt's Say section is the whole user-facing report. Do not restate this file, the scan JSON, or a worker_done body.

```
✓ landed #N · squash-merged · cleaned up
! blocked #N · <short reason> · <what happens next>
? <question>
```

- After `landed`, and cleanup for that task has finished: `✓ landed #N · squash-merged · cleaned up`. That line is the whole report. One short detail line under it, indented two spaces, only when something unexpected happened.
- After `landed`, when this procedure does not clean up: `✓ landed #N · squash-merged · left in place`.
- `blocked`, other than `checks have not registered`: `! blocked #N · <short reason> · <what happens next>`, then the fix task this file already specifies. Do not explain the playbook. `checks have not registered` retries `gantry ship` with no line. Print the blocked line only if that retry stays blocked.
- `stack`: the started line for the ship worker. No landed line, and no paragraph about restack.
- autoMerge false: `? PRs are merge-ready and autoMerge is false. Land them?` then `1. Land (set autoMerge true first)` and `2. Hold`.
- Cleanup that still needs a confirmation: `? Clean up these tasks?` then one numbered option per `remove` or `close` task (`<task> (<op>)`) and a Hold option. Do not paste the scan JSON.
