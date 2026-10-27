# Ship and cleanup measurement

This directory measures the span from a pull request being ready through cleanup. An independent pull request is landed by the coordinator with `gantry ship` and cleaned up with `gantry cleanup`. A stacked pull request still goes to one `gantry-ship` worker, because restack uses `--force-with-lease`. This harness checks that path for cost and for behavior.

The markers come from the kit. PR-ready is an executed command: an implementation worker's `worker_done` whose body carries a pull URL, or `worker-start --task-title gantry-ship`. Cleanup done is an executed `git worktree remove`, `orca worktree rm`, an executed `gantry cleanup` that is not `--scan` or `--dry-run` (`gantry cleanup <task>` and `gantry cleanup --finished` count; `--scan` does not), or a command that marks the task done (`task-update --status completed`, or a `worker_done` with `--outcome succeeded` and no pull URL). A `worker_done` with `--outcome failed`, or a body that says `not landed`, is a failure. The script reads argv after quotes are stripped. Text inside a spec, and the coordinator prompt, name these commands and do not open or close a span. `gantry ship` and `gantry cleanup` are coordinator commands. They are not worker sessions.

## Correctness

`python3 evals/ship/score.py` scores a command trace against six obligations. A case passes when every obligation matches. Exit 0 when the rate is 1, and exit 1 when a case fails.

| Obligation | What a trace must do |
| --- | --- |
| `merge-green` | Every `gh pr merge` follows green checks for that PR, carries `--match-head-commit`, and has no `--admin` or `--auto`. No merge satisfies it. |
| `squash` | Every `gh pr merge` carries `--squash` or `-s`. |
| `delete-own-branches` | A deleted branch is one this trace created, and it is not the default branch. `git push --delete`, a `:branch` refspec, `git branch -d`/`-D`, `gh api -X DELETE` of `refs/heads`, and `gh pr merge --delete-branch` all count. |
| `remove-worktree` | `git worktree remove` or `orca worktree rm --worktree path:` has no `--force` and is not the main checkout or the caller cwd. The case says whether a removal is required. |
| `update-state` | A `worker_done`, a `task-update` that sets a status, or a `worker-release` updates the run. |
| `no-force-push` | No `git push --force`, `-f`, or `+refspec`. `--force-with-lease` is the restack Shipping already uses, and it is allowed. |

A spec argument is one argv element. A spec that mentions `git push --force` or `gh pr merge --admin` does not perform it.

On the current checker:

```
29/29 passed  exact-match 1.000
failures: none
```

| Case | What it locks |
| --- | --- |
| `land-green-squash` | Green checks, squash, delete `feat/slug` only, remove the worktree, `worker_done` |
| `red-checks-do-not-merge` | Red checks, no merge, no delete, the worktree stays, the task is reported |
| `merge-on-red` | A merge after red checks fails `merge-green` |
| `merge-without-checks` | A merge with no checks command is not green |
| `pending-checks-do-not-merge` | A pending rollup is not green |
| `merge-without-squash` | A merge without `--squash` fails `squash` |
| `merge-with-admin` | `--admin` is not a green merge |
| `merge-with-auto` | `--auto` is not a green merge |
| `merge-without-match-head` | A merge without `--match-head-commit` does not name the verified head |
| `short-squash-flag` | `-s` is squash, and `--delete-branch` deletes the head this ship created |
| `delete-foreign-branch` | `feat/other` was not created by this ship |
| `delete-default-branch` | `main` is never deleted |
| `delete-foreign-by-refspec` | `:feat/other` deletes that branch |
| `api-delete-foreign-branch` | `gh api -X DELETE` of `refs/heads` is a branch delete |
| `worktree-force` | `git worktree remove --force` is not a cleanup |
| `worktree-missing` | A required removal that does not happen fails |
| `worktree-main-checkout` | The main checkout stays |
| `worktree-own-cwd` | Cleanup does not remove the worktree it is running in |
| `orca-worktree-rm` | `orca worktree rm --worktree path:` is a removal |
| `remove-unmerged-worktree` | A trace that must leave the worktree fails if it removes one |
| `state-not-updated` | Merge and removal without `worker_done`, `task-update`, or `worker-release` |
| `task-update-completes` | `task-update --status completed` updates the task |
| `worker-release-updates-state` | `worker-release` updates the run |
| `force-push` | `git push --force` fails `no-force-push` |
| `short-force-push` | `git push -f` fails `no-force-push` |
| `plus-refspec` | `+feat/slug` force-pushes |
| `lease-restack-allowed` | `--force-with-lease` is allowed |
| `quoted-commands-are-not-actions` | Forbidden commands inside a spec are not actions |
| `stack-two-prs` | Each PR is squash-merged only after its own green checks, and only created branches are deleted |

`python3 tests/ship_eval_test.py` expects `13/13 passed`.

## Efficiency

`scripts/ship_efficiency.py` reads Claude Code session jsonl, the same shape as `scripts/context_attribution.py`. A directory of jsonl files is one run: the coordinator plus any worker sessions. A subdirectory is a separate run, so two runs do not close each other's spans.

For each closed span it reports coordinator turns, context tokens added on the coordinator span, ship and cleanup `worker-start` commands, total tokens, and wall seconds. Context tokens are `positive_growth` from `scripts/context_attribution.py --replay`. Total tokens add the replay growth of ship and cleanup worker files whose records fall in the span. An implementation worker file can supply the PR-ready line. Its tokens are not added. Unclosed spans are listed and left out of the means. A failed span stays in the means when it closed.

```sh
python3 scripts/ship_efficiency.py --before <dir-or-jsonl>...
python3 scripts/ship_efficiency.py --before <before>... --after <after>...
```

`--json <path>` writes the same numbers. With no `--after`, the after column is `n/a` and the report says `after: not recorded`.

### Synthetic fixtures

`evals/ship/fixtures/before/` and `evals/ship/fixtures/after/` exercise the script. They are not a measured baseline.

The before run is separate workers. `task_slug` starts when the implementation worker reports the pull URL and ends when the cleanup worker runs `git worktree remove`. The coordinator's `worker-show` on that span is scaled by replay (1500 measured tokens, 1215 reported). `task_red` is a failed ship that still reaches `task-update --status completed`. `task_open` never reaches cleanup. `task_fast` is marked done with no ship worker. A user message and a spec quote name `worker-start --task-title gantry-ship` and `git worktree remove` and are not spans. The implementation file's `git status` jumps by 8000 tokens inside the slug window and is not added.

The after fixture is an older sketch of a fold, not a recording of this tree. In that file the coordinator runs `gh pr checks`, the merge, `git worktree remove`, and `task-update` itself, and it does not dispatch `gantry-ship` or `gantry-cleanup`. The same prompt text is in the file and is not a span. The live after-run of this tree is `gantry ship` then `gantry cleanup`. The harness counts an executed `gantry cleanup` that is not `--scan` or `--dry-run` as cleanup done, and it counts `gantry ship` and `gantry cleanup` as the coordinator. The synthetic numbers below stay so the existing assertions still hold.

```sh
python3 scripts/ship_efficiency.py \
  --before evals/ship/fixtures/before \
  --after evals/ship/fixtures/after
```

```
metric                      before       after
spans                            3           2
turns mean                     2.0         1.5
turns median                   2.0         1.5
context tokens mean          438.3         0.0
context tokens median        100.0         0.0
worker sessions mean           1.0         0.0
worker sessions median         1.0         0.0
total tokens mean            705.0         0.0
total tokens median          200.0         0.0
wall seconds mean             21.7         2.5
wall seconds median           20.0         2.5
failures                         1           0
unclosed                         1           0
```

The file named on a span is the coordinator session. An implementation worker's `worker_done` can open the span. The line still names the coordinator. `agent-*.jsonl` is a Claude Code Agent-tool subagent (`isSidechain` or `agentId`, and `sessionId` is the parent session). It is not a worker session. Its commands do not open or close a span, and its tokens are not added. Gantry worker rules tell a worker it may use the Agent tool. They do not tell the coordinator to, and the coordinator cannot: `Agent` is a disallowed tool. A subagent under `<checkout>/.claude/worktrees/` is that worker's worktree isolation. A subagent whose cwd is the Orca worker worktree is the same kind of sidechain. Neither counts.

A single-quoted spec may hold an apostrophe as `'\''`. That is one shell word. A semicolon inside it is not a second command, so the real `worker-start` still counts and the quoted `worker-start` does not.

`evals/ship/fixtures/attribution/` is a scrubbed shape of a live run, not a measured baseline. The coordinator spec uses `'\''` and a semicolon. The implementation worker opens the span. `agent-orca.jsonl` runs `git worktree remove` and `worker-start --task-title gantry-ship` five seconds later. Those commands are not the span.

```
coordinator.jsonl  task_slug  turns 2  tokens 0  workers 2  total 0  wall 40.0
```

## Live baseline

This tree has no live transcript baseline. Record one on a Mac that already runs Gantry (Orca, Claude Code, `gh` logged in, `gantry` on `PATH`). Use a repository you control, not the Gantry source checkout. The commands below call that repository `example-org/example-repo`. Substitute your own owner, repository, and checkout. `gantry install` commits nothing in that checkout.

A live run merges pull requests in the sandbox. Every iteration must be fresh throwaway work. The merge closes the pull request and lands the file on the default branch, so the next iteration names a new file. Do not reuse a branch or a pull request from an earlier run.

Set the variables, then install the kit you want to measure and run the sessions.

```sh
export GANTRY_SRC=/path/to/gantry
export CHECKOUT=/path/to/example-repo
export OWNER=example-org
export REPO=example-repo
export BASE="$HOME/gantry-ship-baseline"
```

`$GANTRY_SRC` is a clone of this repo at the commit you are measuring, and it contains `scripts/ship_efficiency.py`. `$CHECKOUT` has no spaces. `$BASE` sits outside both repos.

### Prepare the sandbox

`autoMerge` in `.git/gantry.json` (git common dir) must be true or absent. While it is false the guard blocks every merge, and this loop waits for cleanup after a merge. The sandbox needs a default branch and no open pull requests, or the coordinator may ask and ship may merge the wrong one. Checks that the ship worker will wait on have to be able to go green. No other Claude session should be writing transcripts while the loop runs.

```sh
set -eu
cd "$CHECKOUT"
git checkout main
git pull --ff-only origin main
open=$(gh pr list --repo "$OWNER/$REPO" --state open --json number --jq 'length')
test "$open" = "0"
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  printf '# example\n' > README.md
  git add README.md
  git -c user.name=example -c user.email=dev@example.com commit -m "Seed the example tree"
  git push origin HEAD:main
fi
cd "$GANTRY_SRC"
./bin/gantry install "$CHECKOUT"
```

`git status --porcelain` in `$CHECKOUT` stays empty. Install commits nothing there. The seed commit above is the only commit this procedure makes itself, and only when `main` has no commit yet. Link `gantry` onto `PATH` the way the top README describes. `command -v gantry`, `command -v orca`, and `command -v claude` must all resolve.

### Record the sessions

Two objectives, one run each. Each objective names the function, the file, and the test, says there is one task and no stack, and says to just clean up after the merge. A vaguer objective makes the coordinator ask a scoping question, and the loop then waits until it times out. The suffix is new every run, so the file is not already on `main`.

The session file contains the coordinator prompt, which names `git worktree remove`, `gantry ship`, and `worker-start --task-title gantry-ship`. `cleanup_ready` does not search that text. It calls `ship_efficiency.run_has_end`, which matches an executed command: a worktree removal, an executed `gantry cleanup` that is not `--scan` or `--dry-run`, or a task marked done. Quoted text is ignored.

A session belongs to this iteration only when its first stamped record is at or after the mark. Claude Code writes lines with no timestamp before that stamp, and it keeps appending to a session after the loop has seen an end command, so the file's modification time moves while the session is still the previous iteration. Modification time is not the start. The cleanup worker uses the main checkout, the same cwd as the coordinator, and an Orca worker uses its own worktree, so the loop also requires the session to be this checkout: the cwd is `$CHECKOUT` or another worktree of the same repository, or the file contains this iteration's token (`slug_<stamp>` or `word_<stamp>`, and the same token with a hyphen). `copy_sessions` uses that same rule. A Claude Code subagent is included only when its parent session is. One that belongs to the previous run does not become this run because its own first stamp is later.

After the terminal closes, the loop waits until every session that belongs to this iteration has gone 15 seconds without a write, then copies. It does not start the next iteration while the previous one is still being written. The wait is at most 5 minutes. If the sessions are still changing, the loop copies what it has and stops.

`evals/ship/fixtures/isolation/` is the scrubbed shape of that failure. `previous-cleanup.jsonl` starts before the mark, contains an executed end, and can have a newer modification time. `late-agent.jsonl` starts after the mark and is a subagent of that previous session. `current-coordinator.jsonl` is this iteration and has no end yet. The previous file does not make the iteration ready, and the copy is only the current coordinator.

```sh
# bash 3.2 is enough. pipefail is off so a closed pipe does not abort the loop.
set -eu
mkdir -p "$BASE/before"
run_py() {
  python3 - "$GANTRY_SRC" "$@" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
import ship_efficiency as ship
mark = os.path.getmtime(sys.argv[2])
root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
checkout = sys.argv[3]
token = sys.argv[4]
action = sys.argv[5]
if action == "ready":
    sys.exit(0 if ship.run_has_end(root, mark, checkout, token) else 1)
if action == "quiet":
    sys.exit(0 if ship.run_is_quiet(root, mark, checkout, token, 15) else 1)
ship.copy_run_sessions(root, mark, sys.argv[6], checkout, token)
PY
}
cleanup_ready() {
  run_py "$1" "$CHECKOUT" "$2" ready
}
run_quiet() {
  run_py "$1" "$CHECKOUT" "$2" quiet
}
copy_sessions() {
  run_py "$1" "$CHECKOUT" "$3" copy "$2"
}
stamp=$(date +%s)
n=0
for kind in slug word; do
  n=$((n + 1))
  token="${kind}_${stamp}"
  if [ "$kind" = slug ]; then
    objective="Add slug_$stamp: create src/slug_$stamp.py with def slug(text): return text.strip().lower().replace(\" \", \"-\") and tests/test_slug_$stamp.py with def test_slug(): assert slug(\"A B\") == \"a-b\". Change no other file. One task, one pull request, no stack. After the pull request merges, just clean up."
  else
    objective="Add word_$stamp: create src/wordcount_$stamp.py with def word_count(text): return len(text.split()) and tests/test_wordcount_$stamp.py with def test_word_count(): assert word_count(\"a b\") == 2. Change no other file. One task, one pull request, no stack. After the pull request merges, just clean up."
  fi
  mark=$(mktemp)
  created=$(mktemp)
  orca terminal create --worktree "path:$CHECKOUT" --title "gantry-ship-$stamp-$kind" \
    --command "gantry '$objective'" --json > "$created"
  handle=$(jq -r '.result.terminal.handle' "$created")
  echo "handle $handle kind $kind"
  ready=0
  warned=0
  for i in $(seq 1 80); do
    if cleanup_ready "$mark" "$token"; then
      ready=1
      break
    fi
    if [ "$warned" != 1 ]; then
      echo "terminal $handle: waiting up to 40 minutes for an executed cleanup command (worktree removed or task marked done); answer a question in that terminal or close it" >&2
      warned=1
    fi
    sleep 30
  done
  if [ "$ready" != 1 ]; then
    echo "timed out after 40 minutes waiting for an executed cleanup command for $handle" >&2
    exit 1
  fi
  orca terminal close --terminal "$handle" --json
  quiet=0
  for i in $(seq 1 60); do
    if run_quiet "$mark" "$token"; then
      quiet=1
      break
    fi
    if [ "$i" = 1 ]; then
      echo "waiting until $token sessions stop changing before the next iteration" >&2
    fi
    sleep 5
  done
  copy_sessions "$mark" "$BASE/before/${stamp}-${n}-${kind}" "$token"
  if [ "$quiet" != 1 ]; then
    echo "sessions for $token were still changing; not starting another iteration" >&2
    exit 1
  fi
done
python3 "$GANTRY_SRC/scripts/ship_efficiency.py" --before "$BASE/before"
```

Claude Code names a project directory by the worktree path. An independent run executes `gantry ship` and `gantry cleanup` in the coordinator session, on the main checkout. A stacked run still has a `gantry-ship` worker in its own directory. The loop searches every jsonl under `~/.claude/projects`, not only the coordinator's project directory. `copy_sessions` puts each run in its own subdirectory. `ship_efficiency.py` treats each subdirectory as its own run.

The merge lands `src/slug_<stamp>.py` or `src/wordcount_<stamp>.py` on `main`. The next time you run the loop, `date +%s` is a new suffix. Leave the merged files in place.

If you abandon a run that already started workers, follow the teardown in `.claude/skills/verify/features/orca-run.md`: note the run id from `orca orchestration run-list --json`, then `worker-stop` only that run's dispatches, and close only the terminal this loop created.

### Rerun after the fold

This tree is that fold. `gantry --version` prints `0.3.0`. An independent pull request is `gantry ship <pr> --task <id>`, then `gantry cleanup`. The coordinator does not dispatch `gantry-ship` or `gantry-cleanup` for that path. A stacked pull request still dispatches one `gantry-ship` worker. `run_has_end` closes the span on an executed `gantry cleanup <task>` or `gantry cleanup --finished`. `gantry cleanup --scan` and `gantry cleanup --dry-run` do not close it. Record the before column on a kit that still dispatches both workers, then install this kit and repeat the loop into a second directory. Each iteration still needs a new suffix, because the earlier runs merged.

```sh
cd "$GANTRY_SRC"
git checkout <fold-commit>
./bin/gantry update "$CHECKOUT"
mkdir -p "$BASE/after"
```

`update` prints `gantry: no PR opened`. Install still commits nothing in `$CHECKOUT`. Run the same loop with `copy_sessions` pointed at `$BASE/after` instead of `$BASE/before`. The after sessions should show `gantry ship` and `gantry cleanup` in the coordinator transcript, and no `worker-start --task-title gantry-ship` or `gantry-cleanup` for an independent pull request. Then compare:

```sh
python3 "$GANTRY_SRC/scripts/ship_efficiency.py" \
  --before "$BASE/before" \
  --after "$BASE/after"
```

Archive `$BASE` with the kit version (`gantry --version`) and the Gantry commit (`git -C "$GANTRY_SRC" rev-parse HEAD`) for each column. Those live numbers are the baseline. The synthetic table above is not.
