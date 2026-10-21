# gantry feature map

One file per operator-facing feature. Each says how to reach it, how to drive it with `scripts/sandbox.sh` or Orca, and what end state proves it. `$S` is `.claude/skills/verify/scripts/sandbox.sh` and `$V` is a sandbox from `$S up`.

| Feature | File | Tier | Entry points |
| --- | --- | --- | --- |
| Install into a repo | [install.md](install.md) | 1 | `gantry install [repo]` |
| Update the kit | [update.md](update.md) | 1 | `gantry update [repo] [--tag <tag>]` |
| Guard hook | [guard.md](guard.md) | 1, 2 | user-level PreToolUse in a registered repo, coordinator and worker roles, `autoMerge` |
| pre-push hook | [pre-push.md](pre-push.md) | 1 | any `git push` from any worktree of a registered repo |
| Duplicate-work check | [overlap.md](overlap.md) | 1 | `gantry overlap` after scope and before the ship worker opens a PR |
| Coordinator launch | [coordinator-launch.md](coordinator-launch.md) | 1, 3 | `gantry`, `gantry "<objective>"` |
| Mailbox | [mailbox.md](mailbox.md) | 1, 3 | coordinator `~/.gantry/current/kit/gantry-mailbox wait` |
| Full Orca run | [orca-run.md](orca-run.md) | 3 | objective to coordinator, workers, PRs, ship |

When you add a feature, add a row here and a file with the same four sections.
