---
name: verify
description: Drive gantry the way an operator does and capture proof. gantry is a bash launcher (`bin/gantry`) that installs a user-level Claude Code guard hook and coordinator agent, plus a git pre-push stub, then starts an Orca coordinator. Use after changing `bin/gantry`, anything under `template/kit/`, or the coordinator agent, to prove install, guard, pre-push, overlap, launcher, or a full Orca run behaves as claimed.
---

# Verify gantry

gantry has no server or UI. Its surfaces are:

- `bin/gantry install <repo>`: copies the kit to `~/.gantry`, merges the guard and the context hooks into `~/.claude/settings.json`, copies the coordinator to `~/.claude/agents/`, installs a `pre-push` stub, writes trust into `~/.claude.json`, and registers the origin. It commits nothing in the target repo.
- `bin/gantry update [repo] [--tag <tag>]`: refreshes `~/.gantry` and re-merges user hooks. It opens no pull request.
- The installed guard, `~/.gantry/current/kit/gantry-guard.py`: a user-level PreToolUse hook. It is dormant until the repo's origin is registered.
- The installed `pre-push` stub: git's second wall under the guard, shared by every worktree.
- `gantry overlap`: ranks pushed GitHub work against a planned change, labels each candidate, and records `continue` or `stop`.
- `bin/gantry [objective]`: execs `claude --agent gantry-coordinator` inside an Orca terminal, with `--autocompact` when `contextReset.mode` is `compact`. `gantry restart` starts a fresh session on the bound Run.
- A full run: coordinator plus Orca workers against a fixture repo you control.

Verification runs in three tiers. Use the cheapest tier that exercises the path you changed.

| Tier | What it drives | Cost | Touches outside the sandbox |
| --- | --- | --- | --- |
| 1 sandbox | install, guard payloads, real `git push`, launcher refusals, overlap classify | seconds, free | nothing |
| 2 real Claude | `claude -p --model haiku` in the sandbox with the user hook live | ~1 min, a few cents | API usage only |
| 3 full Orca run | coordinator + workers + PRs on GitHub | 10-40 min, real tokens | GitHub PRs, branches, and merges on a fixture repo; Orca Runs |

Tier 3 is outward-facing. Ask the operator before starting it.

Feature map: [features/README.md](features/README.md). A proof covers every entry point the map lists for the feature you changed.

## Launch

All tier 1 and 2 work goes through one helper, `.claude/skills/verify/scripts/sandbox.sh`. Pass it a directory in your session scratchpad. Without one it uses `$GANTRY_VERIFY_DIR`, then `$TMPDIR/gantry-verify/<timestamp>`.

```sh
S=.claude/skills/verify/scripts/sandbox.sh
V=<scratchpad>/verify-1
$S up "$V"          # prints "gantry installed in ..." then $V
```

`up` does not need a host `orca` or a host pstack plugin. It stubs both under the scratch `HOME`, because `install` checks `orca` on `PATH` and `~/.claude/plugins/cache/*/pstack/*/skills/poteto-mode`. If `install` fails, `up` removes `$V/sbx` and keeps `$V/evidence`.

`up` builds `$V/sbx/`:

- `home/`: scratch `HOME` with `.claude.json` = `{}`, a pstack stub, and a fake `orca` on `sbx/bin`. `install` runs with `HOME=$V/sbx/home`, so the real `~/.claude.json` is never written.
- `remote.git`: local bare repo.
- `repo/`: main checkout. `origin` URL is `https://github.com/example-user/example-fixture.git`. `remote.origin.pushurl` is `remote.git`, so real pushes land locally. After install, `git status --porcelain` is empty. Nothing under `.claude/` is committed.
- `wt/`: linked worktree on branch `feat`, created after install. The user-level guard covers it without a commit.
- `other/`: a second repo whose origin is not registered. The guard allows it.

Ready when `up` prints the directory and `doctor` passes. `up` refuses an existing `$V/sbx`.

Tier 3 launches differently. See [features/orca-run.md](features/orca-run.md).

## Doctor

```sh
$S doctor "$V"
```

Every line must say `ok`. It checks the sandbox layout, the stored origin URL and pushurl, a clean `git status` after install, that the installed kit under `~/.gantry/current/kit` matches `template/kit/` byte for byte, that `gantry --version` is `0.3.0`, that user settings wire the guard, that the scratch `HOME` trusts the repo, that `.git/gantry.json` is the template, that a fresh worktree denies a coordinator `Write`, that the unregistered repo allows that same `Write`, that overlap classifies duplicate, related, and unrelated as stop, flag, and ignore, and that the installed kit contains none of the previous product name. The origin check reads `git config --get remote.origin.url`. A template mismatch means you edited `template/kit/` after `up`: run `down` and `up` again.

For tier 3, also run:

```sh
F=<fixture checkout>
orca status --json | jq '.result.runtime.state'      # "ready"
gh auth status                                        # an account that can read the fixture
git -C "$F" status -sb                                # see gotcha in orca-run.md
```

## Drive

Guard, one payload per call. Role is `coordinator` or `worker`. Location is `repo` (main checkout) or `wt` (worktree on `feat`). Tool input is the JSON Claude Code would send. The helper runs the user-level guard with `HOME` set to the scratch home.

```sh
$S guard "$V" worker wt Bash '{"command":"git push origin HEAD:main"}'
# DENY  gantry guard: never push to main; push a task branch and open a PR
$S guard "$V" coordinator wt Write '{"file_path":"x.txt","content":"x"}'
# DENY  gantry guard: gantry coordinator never writes code or edits files; dispatch a worker
$S guard "$V" coordinator repo Bash '{"command":"gantry overlap --checkpoint scoped --intent x --files a.py"}'
# ALLOW
```

It prints `ALLOW`, `DENY  <reason>`, or `BROKEN(exit N)`. Any exit other than 0 or 2 is a guard bug: Claude Code treats it as a non-blocking error, so the call would go through.

Real git, for pre-push. Run inside the sandbox with the scratch `HOME`, because the stub execs `$HOME/.gantry/current/kit/pre-push`:

```sh
cd "$V/sbx/wt" && git -c user.name=v -c user.email=v@example.com commit -q --allow-empty -m w
HOME=$V/sbx/home git push -u origin feat                         # allowed
HOME=$V/sbx/home git push origin HEAD:main                       # refuses; main unmoved
```

Real Claude session (tier 2). `HOME` is the scratch home and `--setting-sources user,project,local` loads the user hook:

```sh
$S claude "$V" worker 'Run exactly this one Bash command, then report the exact output or error text: git push origin HEAD:main'
# result: ... gantry guard: never push to main ...
```

`worker` runs in `wt`. `coordinator` runs in `repo` with `--agent gantry-coordinator` and the launcher's `--disallowedTools` list. A denial shows in the output's `permission_denials`.

Launcher. Set `HOME` to the scratch home so the user agent and the registry are the sandbox copies:

```sh
K=<repo checkout>/bin/gantry
H=$V/sbx/home
(cd "$V/sbx/wt" && env -u ORCA_TERMINAL_HANDLE HOME=$H "$K" hi)
# gantry: run gantry from the main checkout (...), not a worktree
```

Always `env -u ORCA_TERMINAL_HANDLE` when driving the launcher from a Bash tool. Your own session may be inside an Orca terminal. With the handle set, `gantry` in `repo` passes every check and `exec`s an interactive `claude` inside your tool call.

Per-feature recipes: [features/](features/README.md).

## Evidence

Everything goes to `$V/evidence/`:

- `install.txt`: install stdout and stderr, written by `up`.
- `guard.jsonl`: one line per `guard` call with role, cwd, tool, input, verdict, and stderr.
- `claude-<n>-<role>.json`: full `claude -p --output-format json` result, including `permission_denials`.

Save anything else you capture there too, such as git output. Name the files after what they prove.

Proof standards:

- Drive the real path. For guard changes, tier 1 payloads are the fast loop. A claim that Claude Code enforces the rule needs one tier 2 run.
- Capture the action and the resulting state. A denied push proves nothing until `remote.git` shows `main` unmoved. An install proves nothing until `git status --porcelain` is empty and `doctor` is all `ok`.
- Check side effects: files created or not, refs moved or not, `~/.claude.json` entries. Confirm the real `~/.claude.json` has no entry for the sandbox: `grep -c "$V/sbx" ~/.claude.json` prints `0`.
- `tests/guard_test.py` is the regression table. Run it (`python3 tests/guard_test.py`, expects `N/N passed`). It loads the template guard with a registered fixture origin.
- `tests/install_test.py`, `tests/update_test.py`, and `tests/overlap_test.py` run the real launcher against temp fixtures.
- `merged_on_github` calls `gh` with the sandbox origin slug `example-user/example-fixture`. Offline or unauthenticated, those rows deny. `tests/guard_test.py` stubs this call.
- `orca_task_title` calls `orca`. A worker `gh pr merge` is allowed only for the Orca worker whose task title is `gantry-ship`, so every tier 1 and tier 2 merge denies unless the role is `ship`. Prove a merge ALLOW in tier 3.

## Cleanup

```sh
$S down "$V"     # removes $V/sbx only; $V/evidence stays
ls "$V/evidence"
```

`down` refuses a directory without the `.gantry-verify` marker that `up` writes. Tier 1 and 2 start no background processes. `claude -p` exits on its own.

Tier 3 cleanup closes only the terminal handle you created and stops only Dispatches in the Run you created. See [features/orca-run.md](features/orca-run.md#cleanup). Never kill `claude` or `orca` by process name: the operator's own sessions run under the same names.

## Helpers

`scripts/sandbox.sh` is executable. Run it with no arguments for usage. Subcommands: `up [dir]`, `doctor <dir>`, `guard <dir> <role> <repo|wt> <Tool> '<json>'`, `claude <dir> <role> '<prompt>'`, `down <dir>`.

Other repo scripts used by tier 3: `scripts/ping-runs.py <repo> N` times coordinator responsiveness, `scripts/race-check.py <repo> N timeout` times worker first heartbeats.
