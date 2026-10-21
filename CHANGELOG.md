# Changelog

## 0.1.0

- Gantry installs its kit under `~/.gantry` and registers the coordinator and the guard at user level. Installing or updating a repo commits nothing in that repo and opens no pull request against it.
- The guard stays dormant until the repo's origin is registered. A registered repo enforces the coordinator and worker rules in every worktree, including one created after install. A missing or unreadable registry falls back to `.git/gantry.json` per repo instead of denying every session. `gantry doctor` fails on an unreadable registry. `gantry install` will not overwrite one unless you pass `--repair`.
- `gantry overlap` ranks open pull requests, remote branches, assigned issues, and the run's own workers against a planned change. The coordinator classifies the top candidates and stops to ask before continuing on a duplicate or a related match.
- `gantry --version` prints `0.1.0`.
