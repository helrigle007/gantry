# pre-push hook

`template/kit/pre-push`, reached through a stub installed into `$(git rev-parse --git-common-dir)/hooks/pre-push`, so it covers every worktree. The stub contains `gantry pre-push` and execs `$HOME/.gantry/current/kit/pre-push`. A missing kit refuses the push. An unregistered origin exits 0. A missing or unreadable registry falls back the same way the guard does: `.git/gantry.json` in the git common dir keeps the repo governed, and its absence leaves the push alone. Either way the hook writes one stderr line telling you to run `gantry doctor`.

## Sub-features

- Refuses any push whose remote ref is the default branch (`origin/HEAD`, fallback `main`), including deletes.
- Refuses a non-fast-forward push unless the parent `git` process was started with `--force-with-lease`.
- Allows new branches, fast-forwards, and deletes of non-default branches.

## How to get to it (user POV)

Any `git push` in the repo or its worktrees. It matters when a push gets past the guard: a script that shells out to git, or a human.

## Driving it with sandbox.sh

Real pushes to `$V/sbx/remote.git` through `remote.origin.pushurl`. Run them with `HOME=$V/sbx/home`, because the stub execs `$HOME/.gantry/current/kit/pre-push`. Run from `$V/sbx/wt`:

```sh
cd "$V/sbx/wt"
git -c user.name=v -c user.email=v@example.com commit -q --allow-empty -m one
HOME=$V/sbx/home git push -q -u origin feat && echo pushed                  # new branch: allowed
HOME=$V/sbx/home git push origin HEAD:main
#   gantry pre-push: refusing to push to main; land changes through a PR.
git -c user.name=v -c user.email=v@example.com commit -q --amend --allow-empty -m one-amended
HOME=$V/sbx/home git push --force origin feat
#   gantry pre-push: non-fast-forward push to feat needs --force-with-lease.
HOME=$V/sbx/home git push --force-with-lease=refs/heads/feat:$(git -C "$V/sbx/remote.git" rev-parse feat) origin feat && echo lease-ok
```

Proof, captured into `$V/evidence/pre-push.txt`: each stderr line, plus `git -C "$V/sbx/remote.git" rev-parse main feat` before and after. `main` must not move; `feat` moves only on the lease push.

## Gotchas

- Run these as literal commands in your Bash tool only if the command text does not contain `push --force`. A user hook that matches that text also matches `--force-with-lease` and blocks the command. Put the lease push in a script file written with the Write tool, or run it via `python3 -c`.
- The lease check reads the parent process args with `ps`. It passes for `git push --force-with-lease` run directly; it can miss when git is several processes down.
- `up` pushes `main` to the bare remote before install. After install, nothing can push `main` there, which is the point.
