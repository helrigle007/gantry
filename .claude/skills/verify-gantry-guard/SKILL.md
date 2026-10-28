---
name: verify-gantry-guard
description: "Check the gantry PreToolUse guard's allow and deny promises, including shell parsing, registry activation, the gantry launcher denial, the ~/.gantry write deny, and the pre-pr gate, against template/kit/gantry-guard.py. Use when a change touches that hook, gantry_kit registry or overlap clearance, the config schema, role classification, git/gh/orca rules, worktree cleanup, or terminal send."
---

# Verify the gantry guard

The guard is `decide` in `template/kit/gantry-guard.py`. It imports `gantry_kit` from the same directory. Claude Code runs it as a user-level PreToolUse hook. A missed shell form runs as a real `git`, `gh`, `orca`, or `gantry` argv. This skill checks those promises on the real module.

The question is whether `decide` denies every command in a fixed grammar for which bash actually executes a forbidden argv, and whether activation matches the registry. That is a property of this Python, so the check runs the module. A model checker would describe a different program. `shlex`, the `Deny` exceptions, and the git and orca calls do not fit an SMT verifier, so the oracle is bash and the subject is `decide`.

## Names

Role names, tool names, and the config path live in one block at the top of `check.py`.

| Name | Value |
| --- | --- |
| `COORDINATOR_AGENT` | `gantry-coordinator` |
| `SHIP_TASK` | `gantry-ship` |
| `CLEANUP_TASK` | `gantry-cleanup` |
| `CONFIG_PATH` | `.git/gantry.json` |
| `MARK_ENV` | `GANTRY_COORDINATOR_HANDLE` |
| `COORD_TOOLS` | the coordinator's tool allowlist |
| `EDIT_TOOLS` | Edit, Write, MultiEdit, NotebookEdit |
| `SUBAGENT_TOOLS` | Agent, Task, Workflow, Monitor |
| `ORIGIN_KEY` | `https://github.com/example-user/example-fixture` |

The checker exits 2 when the hook's coordinator, ship, or cleanup name differs from that block, or when `CONFIG_PATH` is not in the hook. A rename edits the block and the mutation anchors that quote the hook, then re-runs this check.

## When to rerun

Rerun this check after any change to the guard, its config schema, or the activation and registry logic. That includes `template/kit/gantry-guard.py`, `registry_failure`, `look`, `fallback_governed`, and `cleared` in `template/kit/gantry_kit.py`. Keep the skill current with `/maintain-formal-verification-skill`.

A change that only edits the coordinator prompt or the mailbox does not need this check. CI runs the checker on every pull request. A green run means all 25 properties hold. If the kit guard's bytes change, move `SYNC_COMMIT` and `SYNC_BLOB` only after the check still describes the new hook.

## Run

From the repo root. `/usr/bin/bash` must report `GNU bash, version 5.2.21`. `python3` must be `3.12.3` or any `3.11.x`. CI uses Python 3.11 on `ubuntu-24.04`, which has that bash. `ubuntu-latest` is moving to 26.04 and bash 5.3, so the workflow pins `ubuntu-24.04`.

```sh
python3 -B .claude/skills/verify-gantry-guard/check.py
python3 -B .claude/skills/verify-gantry-guard/check.py --full
```

`--full` prints every failing command. The short run prints one failing command per shell shape.

Exit 0 means all 25 properties hold and `template/kit/gantry-guard.py` matches the sync blob. Exit 1 means a property failed or the kit guard bytes differ. Exit 2 means the oracle, the fixture, or a mutation baseline broke, and the result is not a guard verdict.

The run stubs `git`, `gh`, and `orca` on the oracle `PATH`. It builds a temporary repo whose origin is `https://github.com/example-user/example-fixture.git`, which normalizes to `ORIGIN_KEY`. The registry is `$HOME/.gantry/registry.json` inside the fixture home. `autoMerge` is true unless a case says otherwise. The fixture path is rejected when it contains `gantry-guard` or a space. Config is written under `.git/`, so it does not dirty porcelain. The run does not edit the hook.

## Activation

`decide` calls `kit.governing` before any role rule.

- A readable registry whose `repos` object does not contain the normalized origin allows every tool call. `.git/gantry.json` can exist. The repo is dormant.
- A readable registry that contains the origin applies the role rules. The coordinator is a payload whose `agent_type` is `gantry-coordinator`, or a session whose `GANTRY_COORDINATOR_HANDLE` equals its own `ORCA_TERMINAL_HANDLE` (both non-empty), or a session whose ancestor argv carries `--agent gantry-coordinator`. A matching worker row is a worker. No worker and an ancestor that is not the coordinator is the operator, and the operator is allowed everything. When the ancestor walk is unreadable (`None`) and there is no mark and no worker, the role is `unidentified` (or `unconfirmed` if the orca call throws). Agent and Task are denied. Every other tool is checked as a worker, so a push to the default branch is denied and Read is allowed.
- A missing registry, or a registry that is not JSON with a `repos` object, uses the fallback. `.git/gantry.json` present means governed (the role rules). Absent means dormant.

## What is checked

Each property is one sentence. The harm is what a user gets if it breaks. The grammar is `CHAIN` and `CONTROL` in `check.py`, wrapped around the leaves in `shell_cases`. Bash is non-interactive (`--noprofile --norc`). `git`, `gh`, and `orca` on the oracle `PATH` record their argv and exit 0. `timeout`, `env`, `nice`, `xargs`, and `bash` are the system binaries.

| Property | Promise |
| --- | --- |
| coordinator-no-edit | The coordinator is denied Edit, Write, MultiEdit, and NotebookEdit. |
| coordinator-no-subagent | A session whose `agent_type` is the coordinator agent name is denied Agent, Task, Workflow, and Monitor. |
| coordinator-identity | The coordinator is that `agent_type`, or a matching `GANTRY_COORDINATOR_HANDLE`, or an ancestor `--agent` for that agent. |
| coordinator-no-push | The coordinator is denied every checked command for which bash executes `git push`. |
| coordinator-no-merge | The coordinator is denied every checked command for which bash executes `gh pr merge`. |
| coordinator-no-write | The coordinator is denied bash that creates a file, a heredoc, `git commit`, `git reset`, or `rm`. |
| coordinator-fetch-deny | The coordinator is denied fetch, pull, and merge forms outside a fast-forward of origin's default branch on a clean main checkout. |
| coordinator-fetch-allow | The coordinator may fetch origin's default branch, and may fast-forward a clean main checkout that is on that branch. |
| worker-push-argv | A worker push argv cannot target the default branch, force-push, or use `--mirror`, `--all`, or `--no-verify`. |
| ship-merge-argv | `gh pr merge` is allowed only for the gantry-ship worker, with `--match-head-commit`, without `--admin` or `--auto`, and only while `autoMerge` is true. |
| worker-orca-argv | A worker orca argv cannot `run-create`, `worker-start`, `task-create`, `task-update`, or `reset`. |
| cleanup-argv | Worktree removal refuses the main checkout, a dirty tree, an unmerged branch, a live worker path, `--force`, and the caller's own cwd. `orca terminal close` refuses a running worker and the caller's own terminal. |
| terminal-send | `orca terminal send` is allowed only for the coordinator, and only with `--terminal` set to a worker handle of this run. |
| shell-chain | Chains, quotes, command substitution, backticks, `bash -c`, and the checked wrappers cannot hide a forbidden argv. |
| shell-control | `if`, `then`, `while`, `until`, `for`, `case`, `select`, function definitions, subshells, and brace groups cannot hide a forbidden argv. A compound the guard cannot parse is denied. |
| shell-expand | A command name or a governed subcommand position that contains `$`, a backtick, `${`, `$(`, or `eval` is denied, and `eval` or `exec` with a dynamic argument is denied. |
| shell-file | `source` and `.` are denied for the coordinator and the worker. `bash`, `sh`, and `zsh` running a file are denied for the coordinator. A worker may run those shells on a file; the pre-push hook is the backstop for a push to the default branch. |
| shell-benign | A worker may run `git status`, a non-default push, a commit, `npm test`, `python3 -m pytest`, `bash scripts/x.sh`, and `git add && git commit && git push` of their own branch through the chain and control forms. The ship worker may run the guarded merge through those forms. |
| activation-dormant | A valid registry that does not list the origin allows every tool call. |
| activation-registered | A registry entry for the origin applies the role rules. |
| activation-closed | When the origin is registered and the role cannot be read, Agent and Task are denied and every other tool is checked as a worker. |
| activation-fallback | A missing or unreadable registry governs the repo when `.git/gantry.json` exists, and leaves it dormant when that file is absent. |
| worker-gantry | A worker cannot run `gantry` or `gantry_kit.py`. The coordinator may run `gantry overlap`, `gantry charter`, `gantry ship`, `gantry cleanup`, and `gantry wait`, and nothing else under `gantry`. `gantry ship` is denied while `autoMerge` is false and when the argv carries `--admin`, `--auto`, or `--force`. `gantry cleanup` is denied when the argv carries `--force`. |
| worker-home-write | A worker is denied Edit, Write, MultiEdit, and NotebookEdit of `$HOME/.gantry` and every path under it, written absolute or with a leading `~`. Read and `cat` of those paths stay allowed. |
| pre-pr | `gh pr create`, `gh pr new`, and a POST to `repos/<owner>/<repo>/pulls` are denied until the worker's pre-pr overlap check is cleared. The coordinator cannot open a PR. |

Gantry 0.2.0 changed one promise on purpose and added one. worker-gantry was "The coordinator may run `gantry overlap` and nothing else under `gantry`." It then also allowed `gantry charter`, because the coordinator saves the user's standing rules with `gantry charter`. worker-home-write is new. `PROTECTED_PATH` is end-anchored and `check_write` did not expand `~`, so a worker Write of `~/.gantry/charters/global.json` was allowed before. The evidence is the guard commit `039bd7b` and the charter and `~/.gantry` rows in `tests/guard_test.py`.

Gantry 0.3.0 extends worker-gantry again. It was "A worker cannot run `gantry` or `gantry_kit.py`. The coordinator may run `gantry overlap` and `gantry charter`, and nothing else under `gantry`." It is now the sentence in the table. The coordinator may run `gantry ship` and `gantry cleanup`. `gantry ship` is denied while `autoMerge` is false and when the argv carries `--admin`, `--auto`, or `--force`. `gantry cleanup` is denied when the argv carries `--force`. A worker still cannot run `gantry`. The coordinator is still denied every bash `gh pr merge`. The merge of an independent pull request runs inside `gantry ship`, which this hook does not see. The evidence is the guard commit `e13cc98` and the ship and cleanup rows in `tests/guard_test.py` and `tests/ship_cmd_test.py`.

The worker-home-write cases cover Write, Edit, MultiEdit, and NotebookEdit under `$HOME/.gantry`, the `~` form, `$HOME/.gantry` itself, and the ship worker. A sibling `$HOME/.gantry-notes`, Read, `cat`, the operator, and a dormant repo stay allowed.

The pre-pr cases cover `gh pr create`, `gh pr new`, `gh api --method POST` with and without a trailing slash, `gh api -X POST`, and the `https://api.github.com/repos/.../pulls` form. Clearance is one `classified` / `continue` event for task `task_impl` in `$TMPDIR/gantry-mailbox/run_1/overlap.jsonl`.

A single-quoted `$(...)` stays allowed. The guard peels compound keywords and checks each simple command, so `if true; then git status; fi` is allowed and `if true; then git push origin main; fi` is denied. A `{` that the guard cannot place in front of `git`, `gh`, `orca`, or `gantry` is denied.

## Coverage

Sync commit: `e13cc9870d596deb39de71d2e526657aba3c8c18`

Sync blob of `template/kit/gantry-guard.py`: `ed07e6a9e84d30d83316b0af2f96438af0908cf1`

The run prints whether the hook file matches that blob. A shallow checkout that does not have the commit still compares `git hash-object` to the pinned blob. When the commit is present, its blob must equal the pin. Move both constants, and this section, only after you have read the hook at the new commit and the check still describes it.

Covered on that commit: `decide`, `role_of`, `commands`, `expand_compound`, `check_resolved`, `split_segments`, `substitutions`, `check_segment`, `check_bash`, `check_git`, `git_push`, `removable_worktree`, `check_gh`, `gh_pr`, `gh_api` for the pulls POST, `check_orca`, `orca_terminal_send`, `check_gantry`, `coord_gantry`, `check_write`, `in_gantry_home`, `deny_source`, `launcher_args`, `coord_fetch`, `coord_ff`, `coord_git_cmd`, and `gantry_kit.look` / `cleared` through the activation and pre-pr cases.

Not covered: the rest of `gh_api`, GraphQL, and the mailbox daemon. The mailbox one-waiter loop is the next target, and TLA+ fits it. This file does not check it.

## Shell contract

All 25 properties are required. The three shell holes that used to fail now hold.

**shell-control.** `expand_compound` peels `if`, `then`, `elif`, `else`, `while`, `until`, `for`, `do`, `case`, `select`, `function`, `!`, and braces, and the guard checks each simple command. `if true; then git push origin main; fi`, `{ git push origin main; }`, `for`/`while`/`until`/`select`/`case`, `f() { git push origin main; }; f`, and `bash -c 'if true; then git push origin main; fi'` are denied. `if true; then git status; fi` and `{ git status; }` are allowed. A `{` the guard cannot place in front of `git`, `gh`, `orca`, or `gantry` is denied.

**shell-expand.** The command name, a git subcommand, and the arguments of a branch-moving git command must be literal. The same goes for `gh pr create`, `gh pr new`, `gh pr merge`, and `gh api`, and for the first words of `orca` and `gantry`. `eval` is denied. `exec` with a `$` or a backtick in its arguments is denied. `cmd=git; $cmd push origin main`, `x=push; git $x origin main`, `x=main; git push origin $x`, `d=--force; git push origin feat $d`, `m=merge; gh pr $m 3 --squash`, and `exec git push origin $x` are denied. `git commit -m` text and `git add` paths may still contain `$`. A single-quoted `$(...)` stays allowed.

**shell-file.** `source` and `.` are denied for the coordinator and the worker, including `source push.sh` and `. ./push.sh` when `push.sh` contains `git push origin main`. `bash`, `sh`, and `zsh` running a file are denied for the coordinator. A worker may run `bash scripts/x.sh`, `sh scripts/x.sh`, and `zsh scripts/x.sh` even when the file contains `git push origin main`. The checker does not execute `template/kit/pre-push`. That hook is the backstop: it refuses a push whose remote ref is the default branch.

Workers may still run `npm test`, `python3 -m pytest -q`, `git add -A && git commit -m wip && git push -u origin feat`, and the same commands inside `if`.

## A failure the check has seen

A property that has never failed does not count. For each property that holds on the sync commit, this copies the guard and `gantry_kit.py` to a temp directory, flips one anchor, and re-runs that property. The repo hook is not modified. The unmodified property must pass first, or the command exits 2. Exit 0 means the broken copy was caught. Exit 1 means it still passed.

```sh
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate dormancy
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate fail-closed
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate fallback
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate worker-gantry
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate coord-charter
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate worker-home-write
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate drop-compound
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate keep-expand
python3 -B .claude/skills/verify-gantry-guard/check.py --mutate allow-source
```

| Mutation | What it breaks | Witness |
| --- | --- | --- |
| dormancy | the early allow when `governing` returns `None` is skipped | unregistered coordinator Write `src/a.py` denied |
| fail-closed | the unidentified Agent/Task deny is skipped, and the role is still remapped to worker | Agent allowed with no handle and an unreadable ancestor. `git push origin main` stays denied |
| fallback | `look` returns `None` instead of `fallback_governed` | unreadable registry plus `.git/gantry.json`, worker `git push origin main` allowed |
| worker-gantry | `check_gantry` returns immediately | worker `gantry uninstall` and `python3 /opt/gantry_kit.py uninstall` allowed |
| coord-charter | `COORD_GANTRY` drops `charter` | coordinator `gantry charter list` denied |
| worker-home-write | `check_write` checks only `PROTECTED_PATH` | worker Write `$HOME/.gantry/charters/global.json` allowed |
| coord-edit | adds the edit tools to `COORD_TOOLS` | coordinator Write `src/a.py` allowed |
| coord-subagent | adds Agent, Task, Workflow, and Monitor to `COORD_TOOLS` and skips the subagent deny | coordinator Agent allowed |
| coord-push | adds `push` to `COORD_GIT` | `git push -u origin feat` allowed for the coordinator |
| coord-merge | `gh_pr` returns before the merge checks and `COORD_GH` includes `pr merge` | coordinator `gh pr merge 3 --squash --match-head-commit abc` allowed |
| coord-redirect | `redirect_ok` returns true | `echo hi > secret.txt` allowed for the coordinator |
| fetch-any | adds fetch, pull, merge, rebase, and reset to `COORD_GIT` | `git rebase origin/main` and `git reset --hard origin/main` allowed. `git fetch --prune origin` stays denied, because `coord_fetch` runs first |
| push-default | the default-branch check in `git_push` becomes true | worker `git push origin main` allowed |
| ship-title | the ship-task check becomes true | implementation worker `gh pr merge 3 --squash --match-head-commit abc` allowed |
| worker-orca | worker orchestration actions become `ANY` | worker `orca orchestration run-create --spec x` allowed |
| cleanup-porcelain | the clean-tree check becomes true | `git worktree remove` of the dirty fixture worktree allowed |
| terminal-send | `orca_terminal_send` returns immediately | `orca terminal send --terminal term_far --text hi --enter` allowed |
| drop-subst | the recursive `substitutions` walk is skipped | worker `echo "$(git push origin main)"` allowed |
| deny-compound | an unknown command word is denied | worker `if true; then git status; fi` denied |
| drop-compound | compound words are not peeled | worker `if true; then git push origin main; fi` allowed |
| keep-expand | `check_resolved` returns immediately | worker `x=push; git $x origin main` allowed |
| allow-source | `source` and `.` return immediately | worker `source push.sh` allowed. `bash push.sh` stays allowed for the worker |

## Bug or modeling error

Replay the printed command with bash 5.2.21 and a `git` that only logs its argv. If that log does not contain the argv in the failure, the oracle is wrong. Fix `check.py`. If `decide` returns `guard failed closed` plus an exception name, the command exits 2. Fix `check.py`. If bash ran the argv and `decide` allowed the command, the hook is wrong. Report the command. Do not delete the case, and do not edit the hook from this skill.

A new failure, or a recorded finding that disappeared, means the hook or the check moved. Read the diff from the sync commit. Update a property only when the commit message, the hook, or `tests/guard_test.py` shows the promise was changed on purpose, and write the old sentence, the new sentence, and that evidence into this file. Then move the sync commit and the blob pin.

## Outside this check

Python, node, and other interpreters. Aliases and interactive history. Every byte string. The bound is the grammar in `check.py`. The checker does not run `template/kit/pre-push`; shell-file relies on it as the backstop for a worker shell file. The mailbox daemon (`daemon`, one unfiltered `check --wait`, ack, exit when the terminal is orphaned). That ordering question is the next target, and TLA+ fits it. This file does not check it.

After a merge that edited the hook without this skill, run `/maintain-formal-verification-skill`.
