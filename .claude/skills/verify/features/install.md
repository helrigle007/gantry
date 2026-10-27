# Install into a repo

`bin/gantry install [repo] [--repair]`. The implementation is `cmd_install` in `template/kit/gantry_kit.py`.

## Sub-features

- Preconditions: `repo` is a git repo, `origin` parses as `https://host/owner/repo` or `git@host:owner/repo` (github.com or any other host, including GitHub Enterprise), `orca` is on PATH, pstack's `poteto-mode` exists under `$HOME/.claude/plugins/cache/*/pstack/*/skills/`. A missing origin, or one that does not parse, fails before the kit is copied.
- Installs the kit under `~/.gantry/releases/<version>-<hash>/` and points `~/.gantry/current` at it. The tree is `bin/gantry` plus `template/kit/`. A second install with the same bytes does not rebuild the release.
- Merges the guard's PreToolUse wrapper and the context hooks (`PreCompact`, `SessionStart`, `UserPromptSubmit`) into `~/.claude/settings.json` without dropping existing hooks. Commands that already point at `.gantry/current/kit/` and `gantry-guard.py` or `gantry_run.py` are replaced, so a rerun does not duplicate them. Sets `statusLine` when it is absent or already points at the run hook. Leaves any other status line alone. The first install copies the previous settings to `~/.gantry/prior/settings.json`.
- Copies `gantry-coordinator.md` to `~/.claude/agents/`. A file that is not the Gantry coordinator is left alone.
- Installs a `pre-push` stub into the shared hooks dir (`git-common-dir/hooks`). The stub contains `gantry pre-push` and execs `$HOME/.gantry/current/kit/pre-push`. Leaves anyone else's `pre-push` alone and says so on stderr.
- Seeds `.git/gantry.json` in the git common dir when it is missing, and never rewrites it later. It does not write an owner list. It appends `/.claude/worktrees/` to `.git/info/exclude` when that directory is not already excluded. The line is idempotent and is not committed. A second install does not duplicate it. `git status` stays empty when Claude Code has created `.claude/worktrees/`.
- Writes `projects[<realpath of main checkout>].hasTrustDialogAccepted = true` into `$HOME/.claude.json`, atomically, refusing if the file changed mid-write. The file must already exist.
- Registers the normalized origin in `~/.gantry/registry.json` last. SSH and HTTPS for the same repo share one key. Given a worktree path, registers the main checkout. An unreadable registry is left untouched unless `--repair` is passed. `--repair` rebuilds it from checkout paths still present in the file whose git common dir contains `gantry.json`, or writes an empty registry and says so, then registers the repo being installed.
- Commits nothing in the target repo. `git status --porcelain` stays empty. Opens no pull request.
- Warns on stderr when a user or project PreToolUse hook blocks a command pstack needs: the lease push, `git reset --hard origin/<branch>`, `gh pr merge --squash --match-head-commit`. It skips commands that contain `gantry-guard.py`. Exit 2, or a JSON `deny`/`block`, counts as blocked. It never edits those hooks and never changes install's exit status.

## How to get to it (user POV)

The operator runs `gantry install /path/to/repo` once per repo. Workers and later worktrees pick up the user-level guard without a commit in the target repo.

## Driving it with sandbox.sh

`$S up "$V"` runs install with `HOME=$V/sbx/home` and records `$V/evidence/install.txt`. `$S doctor "$V"` then checks clean git status, the user hook, a fresh worktree denial, an unregistered allow, and the kit bytes.

```sh
K=<repo checkout>/bin/gantry; H=$V/sbx/home
git -C "$V/sbx/repo" status --porcelain          # empty
HOME=$H $K install "$V/sbx/repo"                 # idempotent; still one guard command
git -C "$V/sbx/repo" status --porcelain          # empty
HOME=$H $K install "$V/sbx/wt"                   # prints the main checkout path
```

`tests/install_test.py` covers hook merge, idempotence, worktree enforcement, the dormant unregistered repo, prior-settings restore, and a clean status (`python3 tests/install_test.py`, expects `N/N passed`).

Proof: `doctor` all `ok`, plus `grep -c "$V/sbx" ~/.claude.json` prints `0`.

## Gotchas

- Never run `install` against a sandbox without `HOME=$V/sbx/home`. It writes the real `~/.claude.json` and the real user settings.
- The trust key is the realpath. On macOS the default temp directory is a symlink, so `pwd` can disagree with `pwd -P`. Compare against `pwd -P`.
- The guard is user-level. A fresh `git worktree add` is covered without committing anything. Excluding the kit inside the repo would drop it from worktrees, which is why the kit lives in `~/.gantry`.
- The hook-conflict probe executes the operator's hooks. It feeds them fake commands on stdin and never runs those commands, but a hook with side effects still fires once per command.
- The `orca` precondition is `command -v`. It does not check that Orca is running. `up` stubs it.
