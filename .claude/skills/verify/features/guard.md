# Guard hook

`template/kit/gantry-guard.py`, installed as `~/.gantry/current/kit/gantry-guard.py` and wired as a user-level PreToolUse hook with matcher `*`. Exit 0 allows. Exit 2 denies and returns stderr to the model. Any other failure is caught and turned into a deny ("guard failed closed"). The hook is dormant (allows everything, no role rules) until the current repo's origin is in `~/.gantry/registry.json`. A registered repo fails closed when the role is missing. When the registry file is missing or unreadable, the guard does not deny every session. It treats the repo as registered when `.git/gantry.json` exists in the git common dir, and as dormant otherwise, and writes one stderr line telling you to run `gantry doctor`.

## Sub-features

- Role: `agent_type` `gantry-coordinator`, or `GANTRY_COORDINATOR_HANDLE` equal to the inherited `ORCA_TERMINAL_HANDLE`, is the coordinator before any worker lookup. `bin/gantry` exports that variable set to the handle. A leaked copy on a worker names the coordinator's handle, so it does not match, and the worker keeps subagents. A worker is otherwise a session whose handle is some row's `agentTerminalHandle` in `orca orchestration worker-list --json` (every page, any terminal state). That row wins over an ancestor `--agent` flag, so `claude --agent gantry-coordinator` started in a terminal that still has a worker row, with no payload `agent_type` and no matching env mark, is still a worker. With no worker row, an ancestor started with `--agent gantry-coordinator` or `--agent=gantry-coordinator` is the coordinator. Linux reads `/proc` argv. Other hosts read one `ps -axww` table and match the raw command line, so an apostrophe or a trailing backslash in the objective still counts. `ps` does not preserve quoting, so an operator prompt that contains the text `--agent gantry-coordinator` is classified as the coordinator. A readable tree with none of those marks is the operator, including their own chat in an Orca terminal, and a different `agent_type` does not by itself make them the operator. If there is no worker row and the tree cannot be read, the session is unidentified and the worker rules apply, including a deny for `Agent` and `Task`. An Orca error with a handle present and a readable tree that was not launched as the coordinator fails closed to worker. The same error with an unreadable tree is unconfirmed and uses the same worker rules.
- Coordinator: only `Bash, Read, Skill, ToolSearch, AskUserQuestion, TodoWrite, TaskCreate, TaskUpdate, TaskList, TaskGet, Glob, Grep`. `Agent` and `Task` (the subagent tool's old name) are denied. Bash limited to `orca` (`status` and `skills` any action, `orchestration` except `reset` and `check`, `worktree list|show|ps`, `terminal list|show|read|wait`, and `terminal send`), `gantry overlap`, `gantry charter` (`add`, `list`, `rm`), `gantry ship`, `gantry cleanup`, `gantry-mailbox` (the installed path is `~/.gantry/current/kit/gantry-mailbox`), read-only git, `git fetch` from origin, a fast-forward of the default branch in a clean main checkout that is on that branch (`git pull --ff-only`, `git merge --ff-only origin/<default>`, or `git fetch origin <default>:<default>`), `gh pr view|checks|list`, `gh auth status`, and filters. No redirects except `2>&1`, `>&2`, `>/dev/null`, and stdin `<`. Never runs `gh pr merge`, `git push`, or `git worktree remove`. Never opens a pull request. `gantry ship` is allowed only while `autoMerge` is true and the argv has no `--admin`, `--auto`, or `--force`. The merge of an independent pull request runs inside that command, which the hook does not see. A worker `gh pr create` is denied until the pre-pr overlap check is cleared.
- Coordinator terminal send: `orca terminal send` needs `--terminal <handle>`. The handle must be a worker of the coordinator's bound run (`orca orchestration run-current`, then `worker-list --run`). It never targets the active terminal, the coordinator's own terminal, or a handle from another run. `terminal create|split|close|rename|switch` stay denied. Workers, including `gantry-cleanup`, cannot send. The send text is not inspected; the worker's own guard still applies to whatever that session runs.
- Worker push rules: no push to the default branch, no `--force`/`-f`/`+ref`/`--mirror`/`--all`/`--prune`/`--no-verify`, `--force-with-lease` allowed, only to own `origin`. Branch deletes only after the PR merged (asks GitHub).
- Worker merge rules: only the ship worker may run `gh pr merge`. The guard reads the worker row's task `task_title` from `task-list --run <runId> --json` and allows the merge only for `gantry-ship`. A new-child worker's terminal reads `retained` while it runs, so terminal state is ignored. An orca error or timeout, or any other title, denies. The merge also needs `--match-head-commit <sha>`, never `--admin`/`--auto`, and is denied while `.git/gantry.json` in the git common dir says `"autoMerge": false`.
- Worker Orca limits: `orchestration send|ask|check`, `status`, `skills` only.
- Cleanup: a worker whose task title is `gantry-cleanup` may also run `orca terminal close --terminal <handle>` for a settled worker (`dispatchStatus` completed or failed) from any run, other than itself. Terminal state is ignored, so a released `--worktree current` worker still listed as `user_takeover` can be closed once it has settled. A handle that is not a worker row stays denied. The same worker may run `orca worktree rm --worktree path:<path>` without `--force`. Any worker may run `git worktree remove <path>` without `--force`. Both removals need a clean worktree that is not the main checkout and not the caller's own cwd, on a non-default branch that merged on GitHub or is gone from origin with no commits of its own (`git rev-list --count origin/<default>..HEAD` is 0). A path named on a still-running worker row (`worktreePath`, `worktree`, `cwd`, or `path`) stays. A closed PR is not enough on its own when the branch still has commits that are not on the default branch.
- Protected files: guard, mailbox, run hook (`gantry_run.py`), settings, `gantry.json`, coordinator agent, `.git/`, and `~/.gantry` with everything under it (the charter, the registry, the installed kit). Write/Edit denied, with a leading `~` expanded first; Bash commands naming them denied except read-only ones (`cat`, `git diff`, ...). A sibling such as `~/.gantry-notes` is not protected. Workers cannot run `gantry` at all, so only the coordinator changes the charter, and only through `gantry charter`.
- Owner and account: git/gh only against this repo's own origin, once that origin parses as `host/owner/repo`. A missing or unparseable origin blocks network git and gh when the repo is registered. `gh --hostname` must be that host. A command that mentions any string in `.git/gantry.json` `forbidden` is denied, in the raw text or in a parsed argument, ignoring case. Blank entries are ignored. The list is empty by default. The installed hook reads it from the git common dir, including from a nested git repo, and does not read a parent repo's file. A file that is not a JSON object denies Bash and Monitor until it parses. A missing file denies nothing extra. gh/git auth env vars are denied, including a worker assigning `GANTRY_*` or `GH_HOST`. An origin that is not registered skips every role rule.
- Shell parsing: `&&`, `;`, pipes, `$(...)` and backticks outside single quotes (a single-quoted `ssh host '... $(...)'` runs remotely and is not parsed), `bash -c`, `env`/`timeout` wrappers, line continuations. `eval`, `xargs git`, `find -exec git`, `git -C $VAR` are denied because they hide the command.
- Blocks pstack's Orchestrate playbook and `RemoteTrigger` for the coordinator and workers.

## How to get to it (user POV)

Nobody calls it. Every tool call from any Claude session passes through the user-level hook. In a registered repo it governs the coordinator started by `gantry`, Orca workers, and a fresh worktree. A human's own session in that repo is allowed. An unregistered origin is allowed with no role rules.

## Driving it with sandbox.sh

Tier 1, payloads through the installed copy:

```sh
$S guard "$V" worker wt Bash '{"command":"git push origin HEAD:main"}'                    # DENY never push to main
$S guard "$V" worker wt Bash '{"command":"git push -u origin feat"}'                      # ALLOW
$S guard "$V" worker wt Bash '{"command":"gh pr merge 3 --squash --match-head-commit abc"}' # DENY only the gantry-ship worker merges
$S guard "$V" ship wt Bash '{"command":"gh pr merge 3 --squash --match-head-commit abc"}'   # ALLOW
$S guard "$V" operator wt Bash '{"command":"gh repo deploy-key list"}'                    # ALLOW the operator's own session
$S guard "$V" worker wt Edit '{"file_path":".claude/settings.json"}'                      # DENY off limits
$S guard "$V" worker wt Write '{"file_path":"~/.gantry/charters/global.json"}'            # DENY off limits
$S guard "$V" worker wt Bash '{"command":"gantry charter add x"}'                         # DENY only the coordinator runs gantry
$S guard "$V" coordinator repo Bash '{"command":"gantry charter list"}'                   # ALLOW
$S guard "$V" coordinator repo Bash '{"command":"gh pr merge 1 --squash"}'                # DENY coordinator never runs gh pr merge
$S guard "$V" coordinator repo Bash '{"command":"gantry ship 9"}'                        # ALLOW
$S guard "$V" coordinator repo Bash '{"command":"gantry cleanup task_impl"}'             # ALLOW
$S guard "$V" coordinator repo Bash '{"command":"gantry ship 9 --force"}'                # DENY
$S guard "$V" coordinator repo Bash '{"command":"gantry overlap --checkpoint scoped --intent x --files a.py"}'  # ALLOW
$S guard "$V" coordinator repo Bash "{\"command\":\"$V/sbx/home/.gantry/current/kit/gantry-mailbox wait --timeout-ms 180000\"}"  # ALLOW
$S guard "$V" coordinator repo Bash '{"command":"orca orchestration check --wait --timeout-ms 180000"}'    # DENY the mailbox daemon owns check
$S guard "$V" coordinator repo Bash '{"command":"orca terminal send --terminal term_sbx_worker --text check --enter"}'  # ALLOW own-run worker
$S guard "$V" coordinator repo Bash '{"command":"orca terminal send --text check --enter"}'               # DENY needs --terminal
$S guard "$V" coordinator repo Bash '{"command":"orca terminal send --terminal term_other --text check --enter"}'  # DENY not a worker of this run
$S guard "$V" worker wt Bash '{"command":"orca terminal send --terminal term_sbx_worker --text check --enter"}'  # DENY workers cannot send
$S guard "$V" coordinator repo Bash '{"command":"orca terminal create"}'                                 # DENY
$S guard "$V" coordinator repo Bash '{"command":"orca terminal close --terminal term_sbx_worker"}'       # DENY
$S guard "$V" cleanup wt Bash '{"command":"orca terminal close --terminal term_sbx_done"}'             # ALLOW settled worker, other run, user_takeover
$S guard "$V" cleanup wt Bash '{"command":"orca terminal close --terminal term_sbx_worker"}'           # DENY its own running terminal
$S guard "$V" cleanup wt Bash '{"command":"orca terminal close --terminal term_other"}'                # DENY not a gantry worker
$S guard "$V" worker wt Bash '{"command":"orca terminal close --terminal term_sbx_done"}'              # DENY only the cleanup task closes
$S guard "$V" coordinator repo Bash '{"command":"orca worktree ps --json"}'                           # ALLOW read-only scan
```

`worker`, `ship`, `cleanup`, and `coordinator` run with `sbx/bin/orca`. The fake lists `term_sbx_worker` as a retained, running worker of `run_sbx` (titled `gantry-ship` for `ship` and `gantry-cleanup` for `cleanup`) and `term_sbx_done` as a settled `user_takeover` worker of `run_other`. It answers `run-current` with `run_sbx`. `operator` and `coordinator` run with no handle, so a send to `term_sbx_worker` is not a send to the coordinator's own terminal. `tests/guard_test.py` denies a send to `term_main`, the coordinator handle in that fixture. A live Orca run (tier 3, [orca-run.md](orca-run.md)) proves the real lookup: the ship worker lands its PRs, and an implementation worker told to merge its own PR gets "only the gantry-ship worker merges". `tests/guard_test.py` covers roles, paging, cleanup across runs, and terminal send with `orca` stubbed, plus a hook subprocess set with a fake `orca` on PATH.

The guard reads `gantry.json` from the git common dir even when the call comes from `wt`, so an edit there takes effect in every worktree. Tier 1 proves the merge rule:

```sh
printf '%s\n' '{"autoMerge": false}' > "$V/sbx/repo/.git/gantry.json"
$S guard "$V" ship wt Bash '{"command":"gh pr merge 3 --squash --match-head-commit abc"}'
# DENY  gantry guard: autoMerge is false in .git/gantry.json: bring PRs to merge-ready and stop
$S guard "$V" coordinator repo Bash '{"command":"gantry ship 9"}'
# DENY  gantry guard: autoMerge is false in .git/gantry.json: bring PRs to merge-ready and stop
```

That edit changes the sandbox. Restore the template `gantry.json` or run `down` and `up` before a later ship ALLOW.

`forbidden` is the same file. An empty list denies nothing extra. A configured entry denies any command that mentions it, ignoring case:

```sh
$S guard "$V" worker wt Bash '{"command":"echo example-work-account"}'
# ALLOW
printf '%s\n' '{"autoMerge":true,"forbidden":["example-work-account"]}' > "$V/sbx/repo/.git/gantry.json"
$S guard "$V" worker wt Bash '{"command":"echo Example-Work-Account"}'
# DENY  gantry guard: example-work-account is listed in .git/gantry.json forbidden; never touch it from gantry
```

Tier 2, the same rule enforced inside Claude Code under bypass:

```sh
$S claude "$V" worker 'Run exactly this one Bash command, then report the exact output or error text: git push origin HEAD:main'
$S claude "$V" coordinator 'Run exactly this one Bash command and report the output: touch p4.txt'
ls "$V/sbx/repo/p4.txt"    # must not exist
```

Proof: the `DENY`/`ALLOW` lines in `$V/evidence/guard.jsonl`, the `permission_denials` in `claude-*.json`, and the unmoved ref or missing file.

Regression table: `python3 tests/guard_test.py`, expects `N/N passed`. Add a row there for any rule you change.

## Gotchas

- `guard` with `BROKEN(exit N)` is a real bug. Claude Code lets a call through on any exit other than 2.
- Branch-delete rows (`git push --delete`, `git branch -D`, `gh api -X DELETE .../git/refs/heads/x`) call `gh pr list --repo <origin slug>` for real. In this sandbox that slug is `example-user/example-fixture`. Offline or unauthenticated, they deny. `tests/guard_test.py` stubs this; the sandbox does not. On another owner the same call uses that origin's slug, and `gh` has to be logged in as an account that can read it.
- Outside the sandbox the hook calls the real `orca` with whatever `ORCA_TERMINAL_HANDLE` it inherits. Your own session's handle is not a worker row. Unless this process was launched as the coordinator, the guard treats you as the operator and allows everything. Pass a live worker's handle to see worker rules (`HOME=$V/sbx/home ORCA_TERMINAL_HANDLE=term_... python3 ~/.gantry/current/kit/gantry-guard.py < payload.json`). `GANTRY_COORDINATOR_HANDLE` set to that same handle makes the guard treat the session as the coordinator. The literal value `1` does not.
- Tier 2 must set `HOME` to the scratch home and pass `--setting-sources user,project,local`. The guard lives in user settings. A second user hook that matches `push --force` blocks `push --force-with-lease` and can block `reset --hard` on its own. A denial then needs the `denied:` line to show which hook fired.
- Haiku sometimes adds a `description` field or rewords the command. Read the `denied:` line to confirm the command that reached the hook.
