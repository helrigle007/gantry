# Gantry

Gantry turns Claude Code inside Orca into a coordinator-plus-workers setup. One coordinator agent plans and dispatches but never writes code. Each worker is a Claude Code session in its own Orca worktree. The coordinator names one playbook and the few pstack skills that task needs. pstack's Opening a PR, Babysit, and Shipping playbooks own PRs and merging.

Every Gantry session runs with permissions bypassed. Nothing depends on a permission prompt. The rules hold through tool removal, a user-level PreToolUse hook, and a git `pre-push` hook. The hook is dormant until the repo is registered.

## Requirements

- macOS with Orca running (tested on 1.4.214) and `orca` on PATH. See [ground truth](docs/ground-truth.md#machine-fix-the-orca-launcher) if `orca --version` prints "Unable to determine Orca.app path from symlink".
- Claude Code (tested on 2.1.283) with the pstack plugin enabled at user scope.
- `gh` logged in as an account that can access the target repo. `origin` may be any git remote that parses as `https://host/owner/repo` or `git@host:owner/repo`, on `github.com` or a GitHub Enterprise host.
- Orca's "new agent tab" arguments for Claude include `--dangerously-skip-permissions`. That is Orca's default.

### Put gantry on PATH

The first install runs from this checkout, as `./bin/gantry install /path/to/repo`. Then link the installed launcher onto your PATH. The launcher resolves its own symlink, so the link follows every `gantry update`.

```sh
mkdir -p ~/.local/bin && ln -sf ~/.gantry/current/bin/gantry ~/.local/bin/gantry
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
exec zsh
```

macOS starts zsh by default, and `~/.local/bin` is not on its PATH until `~/.zshrc` adds it. Skip the `echo` line if your `~/.zshrc` already does.

## Install into a repo

```sh
gantry install /path/to/repo
```

`install` registers the repo and installs the kit if `~/.gantry` is missing. It commits nothing in the target repo and opens no pull request. `git status --porcelain` in that repo stays empty.

What it writes:

1. A release under `~/.gantry/releases/<version>-<hash>/`, with `current` pointing at it. The tree holds `bin/gantry`, the guard, the coordinator agent, the mailbox, the run hook, and `pre-push`.
2. User-level hooks in `~/.claude/settings.json`. The guard is a PreToolUse command. The run hook is `PreCompact`, `SessionStart`, and `UserPromptSubmit`. Hooks you already have stay. A status line you already set stays. The first install saves your previous settings so `uninstall --all` can put them back.
3. `~/.claude/agents/gantry-coordinator.md`. A file that is not the Gantry coordinator is left alone.
4. A one-line `pre-push` stub in the repo's shared git hooks directory, so every worktree runs it. A different `pre-push` is left alone.
5. `.git/gantry.json` in the git common directory, seeded once and then kept. Worktrees share it.
6. Trust for the main checkout in `~/.claude.json`.
7. An entry in `~/.gantry/registry.json`, keyed by the normalized origin URL. SSH and HTTPS for the same repo share a key.

The guard allows every tool call until that origin is registered. After registration the coordinator and worker rules apply, including in a worktree created later with `git worktree add`. A missing role fails closed. When `~/.gantry/registry.json` is missing or unreadable, the guard and the pre-push hook do not deny every session on the machine. They fall back to `.git/gantry.json` in the current repo. A repo that has the file stays governed, and one that does not stays dormant. Each writes one stderr line telling you to run `gantry doctor`. `doctor` reports an unreadable registry as a failure. `gantry install` refuses to overwrite an unreadable registry unless you pass `--repair`. `--repair` rebuilds the registry from the checkouts it can still find, or starts empty and says so.

## Update, uninstall, doctor

```sh
gantry update [repo] [--tag <tag>]
gantry uninstall [repo]
gantry uninstall --all
gantry doctor [repo]
gantry --version
```

`update` refreshes `~/.gantry` from the kit source, or from a git tag when you pass `--tag`. It swaps `current` only after the new tree loads. It re-merges user hooks and refreshes the agent. It opens no pull request. `gantry: no PR opened` is the last line.

`uninstall` drops one repo from the registry and removes Gantry's `pre-push` stub. It leaves that repo's charter in place, so installing again brings the rules back. `uninstall --all` removes the user-level hooks and the agent, restores the settings saved at first install when the remaining hooks match that file, and then removes `~/.gantry`. That deletes every charter. Copy `~/.gantry/charters` first if you want to keep your rules.

`doctor` prints the version, the kit path, whether this origin is registered, how many charter rules apply here, globally and for this repo, and whether the user hooks, agent, and `pre-push` stub are Gantry's. The charter line reads `charter: every repo 1, this repo 2`. A scope that does not parse reads `unreadable`, and `this repo none` means the directory has no parseable origin. An unreadable registry or charter file is a failure. A missing registry is reported and is not a failure. If `core.hooksPath` is set, it says the stub will not run.

## Duplicate-work check

Every coding task is checked twice. Once after the bug is reproduced or the feature is scoped, when the files and a one-line intent are known, before any worker starts. Again right before the ship worker opens the pull request.

`gantry overlap` gathers the candidates and prints compact JSON. It reads open pull requests in the target repo (`gh pr list --json`, with changed files, branch, and body), remote branches with no pull request (`git ls-remote`), assigned open issues, and the run's own active workers. It ranks them by how many planned files they share.

The coordinator reads the top few and classifies each as duplicate, related, or unrelated, with a one-line reason. A duplicate or a related match stops the run. The coordinator asks with AskUserQuestion and includes the links. It does not continue on its own. Each check is logged on the run and folded into `state.json`.

The check sees work pushed to GitHub. Unpushed local branches are invisible.

The guard lets the coordinator run `gantry overlap`. A worker `gh pr create` stays denied until the pre-pr check is cleared.

## Start a coordinator

Open an Orca terminal in the repo's main checkout, not a worktree, and run:

```sh
gantry
```

This runs `claude --agent gantry-coordinator --dangerously-skip-permissions` with the edit tools disallowed. When `contextReset.mode` is `compact`, the launcher also passes `--autocompact` at `thresholdTokens`.

`gantry restart`, in the same Orca terminal, starts a fresh coordinator session on the Run that terminal is already bound to and loads a handoff derived from Orca.

## Hand it an objective

Type the objective at the coordinator's prompt, or pass it on the command line:

```sh
gantry "Add a slugify helper and a word-count helper, then a CLI that exposes slugify"
```

The coordinator answers a read-only request, such as a question about the repo, its history, or its PRs, directly in its terminal without creating a Run. It asks questions only when the objective is ambiguous. Otherwise it creates an Orca Run, splits the objective into tasks with dependencies, and starts the independent workers in parallel. Each task spec names one playbook and the skills that task needs, including the pre-PR skills the coordinator chose for that task, and carries the Gantry worker rules. It starts with `/poteto-mode` only when the coordinator assigns a lead worker to a large or open-ended task.

Before the first worker, and again before the ship worker opens a pull request, the coordinator runs the duplicate-work check above.

Workers open PRs through pstack's Opening a PR playbook, with runtime evidence under `## Verification`. A dependent task stacks on its parent's branch. When every PR is open, the coordinator dispatches one ship worker with task title `gantry-ship`, which runs Shipping. The guard lets only that worker run `gh pr merge`. Shipping verifies each PR independently and lands the verified run bottom-up with `gh pr merge --match-head-commit`.

A worker's blocking question reaches the coordinator through `orca orchestration ask`. The coordinator answers questions that fall within the objective. Anything else becomes an Orca decision gate for you. Watch progress in Orca's Agent Dashboard. The coordinator reports in its terminal with PR links.

## Standing rules

Tell the coordinator a standing rule in plain words, such as "Always check the linked ticket before scoping." It saves your exact words with `gantry charter add` and answers in one line. It does not ask first. The rule comes back after every compaction, `/clear`, new session, and `gantry restart`. The coordinator copies every rule for this repo and every global rule onto each worker's brief, with its id.

```sh
gantry charter add 'Always check the linked ticket before scoping'
gantry charter add --global 'Never force-push a shared branch.'
gantry charter list
gantry charter rm r-1a2b3c
```

A rule applies to the current repo unless you pass `--global`, which applies it in every repo on this machine. `add` prints the rule's id. Adding the same words again prints the same id and changes nothing. `g-` ids are global and `r-` ids belong to the repo you run the command in, so `rm` needs no scope flag. Outside a git repo, only `add --global` and `rm` of a `g-` id work.

Rules live outside every repo. `~/.gantry/charters/global.json` holds the global rules. `~/.gantry/charters/repos/<host>/<owner>/<repo>.json` holds one repo's rules, keyed by the same normalized origin as the registry. `gantry install --repair` does not touch them. Workers cannot run `gantry charter`, and the guard denies worker edits anywhere under `~/.gantry`.

## Set autoMerge

`.git/gantry.json` in the git common directory, shared by every worktree:

```json
{ "autoMerge": true }
```

`true`, the default, means the ship worker lands verified PRs. With `false`, the ship worker runs Babysit in `drive` mode to bring PRs to merge-ready, and the guard blocks every `gh pr merge`. The coordinator then opens a decision gate on a landing task. To land, set `autoMerge` to `true`, then tell the coordinator to resolve the gate with `land`. Orca resolves a gate only from the Run's own coordinator or the Agent Dashboard, not from another terminal. The guard reads this file from the git common directory, so an edit takes effect in every worktree at once.

The same file holds `contextReset`. The template defaults are compact mode at 300000 tokens:

```json
{
  "autoMerge": true,
  "contextReset": { "mode": "compact", "thresholdTokens": 300000 },
  "forbidden": []
}
```

`mode` is `compact` or `restart`. `thresholdTokens` is a plain token count. In `compact`, a hook writes a handoff before compaction and injects it when the session compacts, resumes, or starts again in a terminal that already has a Run. In `restart`, the status line flags the threshold and one prompt notice tells the coordinator to stop; you then run `gantry restart`. Orca stays the source of truth. The handoff at `$TMPDIR/gantry-mailbox/<run>/handoff.md` is regenerated from Orca and the mailbox journal.

`forbidden` is a list of accounts, repos, or hosts. It is empty when the key is missing. Blank entries are ignored. A coordinator or worker command that mentions an entry, ignoring case, in the raw text or in a parsed argument, is denied. The installed hook reads that list from the registered repo's common directory, including when the command runs inside a nested git repo. It does not read a parent repo's file. A `gantry.json` that is not a JSON object denies those commands until it parses. The operator's own session stays unrestricted. An unregistered origin is not governed at all.

## What the guard enforces

These rules apply only when the current repo's origin is registered.

| Rule | Who | How |
| --- | --- | --- |
| No file edits | coordinator | `tools:` allowlist, `disallowedTools`, and the guard's tool allowlist |
| Bash limited to `orca` (never `orchestration check`), `gantry overlap`, `gantry charter`, `~/.gantry/current/kit/gantry-mailbox`, read-only `git`, `git fetch` from origin, a fast-forward of the default branch in a clean main checkout, and `gh pr view/checks/list` | coordinator | guard |
| No merging, and no opening a pull request | coordinator always | guard |
| No `gh pr create` until the pre-pr overlap check is cleared | workers | guard |
| No merging | every worker except the `gantry-ship` task's, all workers when `autoMerge` is false | guard |
| Merges only as `gh pr merge --match-head-commit`, never `--admin` or `--auto` | workers | guard |
| No push to the default branch, no plain force push (`--force-with-lease` only) | coordinator and workers (guard), everyone (`pre-push`) | guard and `pre-push` |
| git and gh only against the repo's own `origin`, and any `forbidden` entry is denied when a command mentions it | coordinator and workers | guard |
| No deleting repos, settings, or branches, except merged task branches | coordinator and workers | guard |
| Orca use limited to `orchestration send`, `ask`, and `check` | workers, except the `gantry-cleanup` task's, which may also close settled worker terminals from any run and remove clean worktrees whose branch merged or is gone from origin with no unpushed commits | guard |
| No pstack Orchestrate playbook | workers | guard |
| No edits to the guard, mailbox, run hook, settings, agent, `gantry.json`, or anything under `~/.gantry`, the charter included | workers | guard |

A worker is a session whose `ORCA_TERMINAL_HANDLE` belongs to an Orca worker dispatch. The coordinator is the session `gantry` starts. The hook payload's `agent_type` is `gantry-coordinator`, or `GANTRY_COORDINATOR_HANDLE` equals that same `ORCA_TERMINAL_HANDLE`, or an ancestor was started with `--agent gantry-coordinator`. Either of the first two wins even when that terminal still has a worker row. A leaked `GANTRY_COORDINATOR_HANDLE` on a worker names the coordinator's handle, not the worker's, so the worker keeps its tools. The operator's own chat, including one in an Orca terminal, stays unrestricted once the process tree is readable and was not launched as the coordinator. If the session is neither a confirmed worker nor a readable operator, the worker rules apply, including a deny for `Agent` and `Task`. When Orca itself cannot be queried and the process tree cannot be read, the same worker rules apply.

The guard parses shell commands. A worker that writes a script which pushes on its own gets past the guard, but the `pre-push` hook still stops a push to the default branch or a plain force push. GitHub branch protection would be the server-side wall. A private repo on the free plan cannot enable it.

## Migrating an existing install

Install Gantry, register the repo, then remove the old committed `.claude/` spudpod files from the target repo in a normal PR.

```sh
gantry install /path/to/repo
```

## Next: cut or add

Cut:

- The coordinator's 180-second heartbeat confirmation. The race never reproduced in 8 starts, and Orca's own `ready` means input was accepted. Keep it until one more Orca release, then drop it.
- `scripts/race-check.py`, once the race is retired upstream.

Add:

- A user hook whose pattern is `push --force` also matches Shipping's `--force-with-lease` restack. `push (--force|-f)([^-]|$)` allows the lease form. A stack whose parent squash differs from the original commits stalls until that pattern changes.
- GitHub branch protection or rulesets as the server-side wall, once a repo is public or on Pro. `install` can apply them when the API allows.

## Layout

- `bin/gantry` is the launcher. `gantry --version` prints `0.2.0`.
- `template/kit/` is the source the launcher installs under `~/.gantry`.
- `scripts/race-check.py <repo> [n] [timeout]` starts N new-child workers in parallel and times each first heartbeat. `scripts/ping-runs.py <repo> <n>` times coordinator responsiveness.
- `tests/guard_test.py`, `tests/install_test.py`, `tests/update_test.py`, `tests/context_attribution_test.py`, `tests/reset_test.py`, `tests/overlap_test.py`, `tests/registry_test.py`, and `tests/charter_test.py` are the regression suites.
- `docs/ground-truth.md` records what the installed tools actually do.

## License

Apache License, Version 2.0. See [LICENSE](LICENSE).
