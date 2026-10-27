# Coordinator launch

`bin/gantry [objective]` (`cmd_start` in `template/kit/gantry_kit.py`). It exports `GANTRY_COORDINATOR=1` and `GANTRY_COORDINATOR_HANDLE` set to `ORCA_TERMINAL_HANDLE` and `exec`s `claude "$@" --agent gantry-coordinator --dangerously-skip-permissions --disallowedTools "Edit,Write,NotebookEdit,Agent,Task,Workflow,Monitor"`. When `.git/gantry.json` `contextReset.mode` is `compact` (the default, `thresholdTokens` 300000), it inserts `--autocompact <thresholdTokens>` after the objective. Claude Code accepts 100000–1000000, and the launcher clamps to that range. `mode` `restart` omits the flag. `gantry restart` exports the same handle plus `GANTRY_RESTART=1`.

`gantry restart` refreshes the handoff with `~/.gantry/current/kit/gantry_run.py write`, then starts a new `claude --agent gantry-coordinator` session (no `--resume`) whose prompt says to continue the bound Run and not to create one. The agent file must be the user-level copy, the origin must be registered, and the command must run from the main checkout.

## Sub-features

- Refuses outside a git repo, from a linked worktree, where the agent file is missing, and outside an Orca terminal (`ORCA_TERMINAL_HANDLE` unset).
- Passes the objective before `--disallowedTools`, which is variadic and would swallow it.
- The coordinator's tool list is the agent's `tools:` allowlist minus `disallowedTools`: Bash, Read, Skill, ToolSearch, AskUserQuestion.
- A new task stays `orca orchestration worker-start --spec`. To clarify or correct a worker already running, the agent reads that terminal, waits for `tui-idle`, then `orca terminal send --terminal <handle> --text '...' --enter`. The guard allows that send only for a worker of this run. The worker's own guard still applies to whatever it runs. Allow and deny payloads are in [guard.md](guard.md).
- An independent pull request is `gantry ship`. A stacked pull request still dispatches one `gantry-ship` worker so Shipping can restack. Cleanup runs when the user asks. `gantry cleanup --scan` lists each task as `close`, `remove`, or `skip`. `gantry cleanup <task>` acts on one confirmed task. `gantry cleanup --finished` is "just clean up". The coordinator does not dispatch `gantry-cleanup`. The main checkout stays, and a settled terminal there is closed. A terminal with no worker row is left open.

## How to get to it (user POV)

In an Orca terminal on the repo's main checkout: `gantry` for an interactive prompt, or `gantry "<objective>"`.

## Driving it with sandbox.sh

Refusals, tier 1. Always strip the handle, or the last case launches an interactive Claude inside your tool call:

```sh
K=<repo checkout>/bin/gantry
H=$V/sbx/home
(cd "$V/sbx/wt"   && env -u ORCA_TERMINAL_HANDLE HOME=$H $K hi); echo $?   # "run gantry from the main checkout (...)", 1
(cd "$V/sbx/repo" && env -u ORCA_TERMINAL_HANDLE HOME=$H $K hi); echo $?   # "run gantry inside an Orca terminal ...", 1
(cd /tmp          && env -u ORCA_TERMINAL_HANDLE HOME=$H $K hi); echo $?   # ". is not a git repository", 1
```

Tool list, tier 2 (same flags the launcher passes). `HOME` is the scratch home so `--agent` resolves the user-level coordinator:

```sh
cd "$V/sbx/repo" && HOME=$V/sbx/home claude -p 'reply ok' --model haiku --setting-sources user,project,local --dangerously-skip-permissions \
  --output-format stream-json --verbose --agent gantry-coordinator \
  --disallowedTools "Edit,Write,NotebookEdit,Agent,Task,Workflow,Monitor" \
  | head -1 | jq '.tools' > "$V/evidence/coordinator-tools.json"
```

Proof: the init tools list contains AskUserQuestion and contains no Edit, Write, Agent, Task, Workflow, or Monitor.

Live launch, tier 3: see [orca-run.md](orca-run.md). `orca terminal create --worktree path:<main checkout> --command "gantry '<objective>'"` is the only way to give it a real `ORCA_TERMINAL_HANDLE`.

## Gotchas

- Your own Claude session may run inside an Orca terminal, so `ORCA_TERMINAL_HANDLE` is set in your Bash tool. Check with `echo ${ORCA_TERMINAL_HANDLE:-unset}`.
- `gantry` runs from `bin/gantry` directly. It is only on PATH if the operator linked it into `~/.local/bin`. Orca terminals created by tier 3 need it on PATH; check `command -v gantry` first.
