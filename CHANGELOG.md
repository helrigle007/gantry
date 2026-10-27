# Changelog

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
