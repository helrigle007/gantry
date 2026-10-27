# Changelog

## 0.3.0

- `gantry ship` lands a pull request whose check rollup is empty when the repo has no Actions workflows and no required status checks on the base branch. When checks are configured but none have registered yet, it polls a few times and blocks only if the rollup stays empty or a check is not green. Red, pending, and failing checks still block.
- The coordinator lands an independent pull request with `gantry ship <pr>` and removes the worktree with `gantry cleanup <task>`. It does not dispatch a `gantry-ship` or `gantry-cleanup` worker for that path. A stacked pull request still goes to one `gantry-ship` worker, because restack uses `--force-with-lease` and that step stays outside the coordinator. While `autoMerge` is false, the guard denies `gantry ship` and the ship worker still babysits.
- `gantry ship` squash-merges only after green checks, with `--match-head-commit`, and deletes only a non-default branch this run recorded. It never force-pushes and never passes `--admin`, `--auto`, or `--force`. `gantry cleanup` removes a clean worktree that is not the main checkout and not the caller cwd, then marks the task completed. It never uses `--force`.
- The coordinator prompt points at `gantry-ship.md` for the procedure. The guard still denies the coordinator `gh pr merge`, `git push`, and `git worktree remove`. The merge runs inside `gantry ship`.
- `gantry --version` prints `0.3.0`.

## 0.2.3

- `gantry install` and `gantry update` add `/.claude/worktrees/` to the checkout's `.git/info/exclude` when that directory is not already excluded. The line is local and idempotent. Nothing is committed. Claude Code can create that directory when a worker uses the Agent tool with worktree isolation; it stays out of `git status`.
- The ship measurement loop counts a session by the timestamp of its first record, and only when that session is this checkout. It waits until those sessions go quiet before the next iteration. `scripts/ship_efficiency.py` names the coordinator session on each span and does not count Claude Code `agent-*.jsonl` subagents as workers.
- `gantry --version` prints `0.2.3`.

## 0.2.2

- `gantry overlap` leaves out the current task's own pushed branch, active worker, and open pull request before it classifies. It matches the task id, the branch recorded for that task, and the branch checked out in the task's worktree. Another task's branch that shares files is still a stop. A check that cannot identify the task is unchanged.
- `gantry --version` prints `0.2.2`.

## 0.2.1

- `gantry overlap` labels every candidate and prints a top-level `verdict` of `continue` or `stop`. It records that classification, so a `continue` clears the pre-pr check without a second command. On `stop` the coordinator asks, then records `--answer proceed` or `--answer hold`.
- `gantry --version` prints `0.2.1`.

## 0.2.0

- The coordinator saves your standing rules in your exact words with `gantry charter add`, and `gantry charter list` and `gantry charter rm <id>` show and remove them. A rule applies to the current repo, or to every repo with `--global`. Rules live in `~/.gantry/charters/`, outside every repo.
- The charter comes back after every compaction, `/clear`, new session, and `gantry restart`. The handoff carries it just above `Next:`, and a plain start or `/clear` injects only the charter. A coordinator brief copies every rule for the repo and every global rule onto a `Charter:` line.
- The guard lets the coordinator run `gantry charter` and denies worker edits anywhere under `~/.gantry`, including a path written with `~`. Before 0.2.0, a worker could write files under `~/.gantry`, such as `registry.json`.
- `gantry doctor` prints how many charter rules apply, globally and for this repo, and fails when a charter file does not parse. Per-repo `uninstall` keeps the charter. `uninstall --all` deletes it.
- The README shows how to put `gantry` on PATH on macOS.
- `gantry --version` prints `0.2.0`.

## 0.1.0

- Gantry installs its kit under `~/.gantry` and registers the coordinator and the guard at user level. Installing or updating a repo commits nothing in that repo and opens no pull request against it.
- The guard stays dormant until the repo's origin is registered. A registered repo enforces the coordinator and worker rules in every worktree, including one created after install. A missing or unreadable registry falls back to `.git/gantry.json` per repo instead of denying every session. `gantry doctor` fails on an unreadable registry. `gantry install` will not overwrite one unless you pass `--repair`.
- `gantry overlap` ranks open pull requests, remote branches, assigned issues, and the run's own workers against a planned change. The coordinator classifies the top candidates and stops to ask before continuing on a duplicate or a related match.
- `gantry --version` prints `0.1.0`.
