# Overlap measurement

This directory measures the duplicate-work check. It does not change the check. `classify` and `verdict_of` in `template/kit/gantry_kit.py` stay as they are. A later change can make `gantry overlap` apply those rules itself and record `continue` when every candidate is unrelated. Run the same commands before and after that change.

The rule the cases lock: shared files, and the intent contained in the title or body or the title contained in the intent, is `duplicate`. Shared files otherwise is `related`. No shared files is `unrelated`. Comparison is a case-insensitive substring, after trimming, and a leading `./` on a path is ignored. The verdict is `continue` when every candidate is unrelated. Any duplicate or related candidate is `ask` (`verdict_of` returns `stop` for that).

## Correctness

`python3 evals/overlap/score.py` prints one pass or fail per case and the exact-match rate. A case passes when every candidate label matches and the verdict matches. Exit 0 when the rate is 1, and exit 1 when a case fails.

Score any classifier with the same shapes as the kit:

```sh
python3 evals/overlap/score.py --classify module:function --verdict module:function
```

`classify(intent, planned_files, candidate)` returns `duplicate`, `related`, or `unrelated`. `verdict(labels)` returns `continue`, `stop`, or `ask`. `stop` is reported as `ask`.

On the current `classify` and `verdict_of` (v0.2.0, unchanged by this harness):

```
16/16 passed  exact-match 1.000
classify 16/16  verdict_of 16/16
failures: none
```

No case fails. The cases still lock the sharp edges of the rule:

| Case | Verdict | What it locks |
| --- | --- | --- |
| `exact-duplicate-pr` | ask | Same files, title matches the intent ignoring case, `./src/app.py` still shares |
| `duplicate-intent-in-body` | ask | The intent is in the body and the title is different |
| `duplicate-branch-no-pr` | ask | Branch `retry-client` has no pull request; the name sits inside the intent and the file is shared |
| `related-pr-different-intent` | ask | Shared `src/app.py`, title and body are a different change |
| `unrelated-prs` | continue | Open pull requests whose files are disjoint from the plan, including a near title |
| `assigned-issue-matching-title` | continue | The issue title is the intent and `gather` leaves `files` empty, so it is unrelated |
| `active-worker-same-task` | ask | The worker title is the intent and the worker lists the planned files |
| `no-candidates` | continue | An empty candidate list continues |
| `near-miss-branch-different-work` | continue | Branch `fix-crash` touches `docs/banner.md` only |
| `mixed-duplicate-and-unrelated` | ask | One duplicate makes the whole set ask |
| `mixed-related-and-unrelated` | ask | One related candidate makes the whole set ask |
| `mixed-text-matches-without-files` | continue | A matching issue title and a matching worker title with no files are unrelated |
| `adversarial-short-title` | ask | Title `app` is contained in the intent |
| `adversarial-intent-inside-word` | ask | Intent `log` is contained in `catalog` |
| `adversarial-hyphenated-branch` | ask | Branch `fix-the-crash-in-app` shares the files and is related, because the hyphens keep it from containing the intent |
| `adversarial-empty-intent` | ask | An empty intent does not match the title; shared files are related |

`python3 tests/overlap_eval_test.py` expects `9/9 passed`.

## Efficiency

`scripts/overlap_efficiency.py` reads Claude Code session jsonl, the same shape as `scripts/context_attribution.py`. A check starts at the assistant turn that runs `gantry overlap` and ends at the recorded verdict: a later `gantry overlap --record classified` or `--record answered`, or a top-level `"verdict"` of `continue` or `stop` on that command's JSON result. Text inside quotes is ignored, so a worker spec that mentions the command is not a check.

For each closed check it reports coordinator turns on that span, context tokens added, and wall seconds from the overlap command's timestamp to the verdict's timestamp. Context tokens are `positive_growth` from `scripts/context_attribution.py --replay`. Unclosed checks are listed and left out of the means.

```sh
python3 scripts/overlap_efficiency.py --before <dir-or-jsonl>...
python3 scripts/overlap_efficiency.py --before <before>... --after <after>...
```

`--json <path>` writes the same numbers. With no `--after`, the after column is `n/a` and the report says `after: not recorded`.

### Synthetic fixtures

`evals/overlap/fixtures/before/synthetic-before.jsonl` and `evals/overlap/fixtures/after/synthetic-after.jsonl` exercise the script. They are not a measured baseline. The before file is a coordinator that classifies by hand, including one `worker-show` so replay scaling changes that check (1500 measured tokens, 1215 reported). The after file is one turn whose overlap result already contains `"verdict": "continue"`. The current kit does not print that field.

```sh
python3 scripts/overlap_efficiency.py \
  --before evals/overlap/fixtures/before \
  --after evals/overlap/fixtures/after
```

```
metric                      before       after
checks                           3           2
turns mean                     4.0         1.0
turns median                   3.0         1.0
context tokens mean          738.3         0.0
context tokens median        900.0         0.0
wall seconds mean             23.3         2.5
wall seconds median           20.0         2.5
unclosed                         1           0
```

## Live baseline

This tree has no live transcript baseline. The harness was added in an environment where `claude` and `orca` were not on `PATH`, so no coordinator sessions were run and no session numbers were invented.

Record the v0.2.0 baseline on a Mac that already runs Gantry (Orca, Claude Code, `gh` logged in, `gantry` on `PATH`). Use a repository you control, not the Gantry source checkout. The commands below call that repository `example-org/example-repo`. Substitute your own owner, repository, and checkout. `gantry install` commits nothing in that checkout. The seed commands below are your own commits.

Set the variables, then seed, install the v0.2.0 kit, and run the ten sessions.

```sh
export GANTRY_SRC=/path/to/gantry
export CHECKOUT=/path/to/example-repo
export OWNER=example-org
export REPO=example-repo
export BASE="$HOME/gantry-overlap-baseline"
```

`$GANTRY_SRC` is a clone of this repo that contains tag `v0.2.0` and `scripts/overlap_efficiency.py`. `$CHECKOUT` has no spaces. `$BASE` sits outside both repos.

### Seed the example repo

Start from `main` with no other open pull requests, so the five tasks see only these candidates.

```sh
set -euo pipefail
cd "$CHECKOUT"
git checkout main
mkdir -p src tests
printf 'def app():\n    return 1\n' > src/app.py
printf 'def test_app():\n    assert True\n' > tests/test_app.py
printf 'def client():\n    return 1\n' > src/client.py
printf '# example\n' > README.md
git add src tests README.md
git -c user.name=example -c user.email=dev@example.com commit -m "Seed the example tree"
git push origin HEAD:main

git checkout -b fix-crash
printf 'def app():\n    return 0\n' > src/app.py
printf 'def test_app():\n    assert False\n' > tests/test_app.py
git add src/app.py tests/test_app.py
git -c user.name=example -c user.email=dev@example.com commit -m "Fix the crash in app"
git push -u origin HEAD
gh pr create --repo "$OWNER/$REPO" --title "Fix the crash in app" \
  --body "Fix the crash in app. The crash is in the request path."

git checkout main
git checkout -b banner
printf 'def app():\n    return "banner"\n' > src/app.py
git add src/app.py
git -c user.name=example -c user.email=dev@example.com commit -m "Rename the banner"
git push -u origin HEAD
gh pr create --repo "$OWNER/$REPO" --title "Rename the banner" --body "Cosmetic copy only."

git checkout main
git checkout -b docs-readme
printf '# example\n\nMore docs.\n' > README.md
git add README.md
git -c user.name=example -c user.email=dev@example.com commit -m "Update readme"
git push -u origin HEAD
gh pr create --repo "$OWNER/$REPO" --title "Update readme" --body "Docs only."

git checkout main
git checkout -b retry-client
printf 'def client():\n    return 2\n' > src/client.py
git add src/client.py
git -c user.name=example -c user.email=dev@example.com commit -m "Add retry-client"
git push -u origin HEAD

git checkout main
gh issue create --repo "$OWNER/$REPO" --title "Fix the crash in app" \
  --body "The crash is in the request path." --assignee @me
```

That leaves three open pull requests, branch `retry-client` with no pull request, and one issue assigned to the logged-in `gh` user.

### Install the v0.2.0 kit

```sh
cd "$GANTRY_SRC"
git rev-parse --verify v0.2.0
./bin/gantry install "$CHECKOUT"
./bin/gantry update "$CHECKOUT" --tag v0.2.0
"$HOME/.gantry/current/bin/gantry" --version
```

The version line is `0.2.0`. `update` prints `gantry: no PR opened`. Install still commits nothing in `$CHECKOUT`. Link `gantry` onto `PATH` the way the top README describes, so the Orca terminal can run it. `command -v gantry`, `command -v orca`, and `command -v claude` must all resolve.

### Record ten sessions

Five objectives, twice each, each in its own Orca terminal. Stop at the scoped overlap record. The metric ends at that record, so later lines in the file stay out of the numbers.

```sh
# bash 3.2 is enough. pipefail is off so a closed pipe from head does not abort the loop.
set -eu
mkdir -p "$BASE/before"
objectives=(
  "Fix the crash in app. The files are src/app.py and tests/test_app.py."
  "Add retry-client. The file is src/client.py."
  "Rename the banner. The file is src/app.py."
  "Document the retry helper. The file is docs/retry.md."
  "Add a slug helper. The files are src/slug.py and tests/test_slug.py."
)
key=$(printf '%s' "$(cd "$CHECKOUT" && pwd -P)" | sed 's/[^A-Za-z0-9]/-/g')
proj="$HOME/.claude/projects/$key"
for objective in "${objectives[@]}"; do
  for run in 1 2; do
    stamp=$(date +%s)
    mark=$(mktemp)
    created=$(mktemp)
    slug=$(printf '%s' "$objective" | tr ' A-Z' '-a-z' | tr -cd 'a-z0-9-' | cut -c1-40)
    orca terminal create --worktree "path:$CHECKOUT" --title "gantry-overlap-$stamp" \
      --command "gantry '$objective'" --json > "$created"
    handle=$(jq -r '.result.terminal.handle' "$created")
    echo "handle $handle objective $objective run $run"
    session=""
    ready=0
    for _ in $(seq 1 60); do
      if [ -d "$proj" ]; then
        session=$(find "$proj" -name '*.jsonl' -newer "$mark" -exec ls -t {} + 2>/dev/null | head -1)
      fi
      if [ -z "$session" ]; then
        session=$(find "$HOME/.claude/projects" -name '*.jsonl' -newer "$mark" -exec ls -t {} + 2>/dev/null | head -1)
      fi
      if [ -n "$session" ] && grep -q "gantry overlap --record" "$session"; then
        ready=1
        break
      fi
      sleep 10
    done
    if [ "$ready" != 1 ]; then
      echo "no overlap record yet for $handle (session ${session:-none})" >&2
      echo "If a question is waiting: orca terminal send --terminal $handle --text 'hold' --enter --json" >&2
      exit 1
    fi
    cp "$session" "$BASE/before/${stamp}-${run}-${slug}.jsonl"
    orca terminal close --terminal "$handle" --json
  done
done
python3 "$GANTRY_SRC/scripts/overlap_efficiency.py" --before "$BASE/before"
```

Claude Code names the project directory by taking the checkout's absolute path and replacing every character that is not a letter or digit with `-`. If that directory is missing, the loop uses the newest session jsonl under `~/.claude/projects` written after the terminal was created.

The first three objectives are duplicates or related work, so the coordinator should ask. The docs and slug objectives share no seeded files, so the rule says continue. If a question is waiting and the loop has not seen `--record` yet, send `hold` from another shell:

```sh
orca terminal send --terminal <handle> --text 'hold' --enter --json
```

If you abandon a run that already started workers, follow the teardown in `.claude/skills/verify/features/orca-run.md`: note the run id from `orca orchestration run-list --json`, then `worker-stop` only that run's dispatches, and close only the terminal this loop created.

### Rerun after the labeling change

Check out the commit that makes `gantry overlap` label candidates and record `continue` when every candidate is unrelated. Install that kit, leave the seeded repository as it is, and repeat the ten sessions into a second directory.

```sh
cd "$GANTRY_SRC"
git checkout <that-commit>
./bin/gantry update "$CHECKOUT"
mkdir -p "$BASE/after"
```

Run the session loop again with `cp` pointed at `$BASE/after` instead of `$BASE/before`. On that kit the verdict may be inside the first overlap result (`"verdict": "continue"`) with no `--record` line. Wait until the session contains either `gantry overlap --record` or `"verdict":"continue"` or `"verdict":"stop"`, then copy it. Then compare:

```sh
python3 "$GANTRY_SRC/scripts/overlap_efficiency.py" \
  --before "$BASE/before" \
  --after "$BASE/after"
```

Archive `$BASE` with the kit version (`gantry --version`) and the Gantry commit (`git -C "$GANTRY_SRC" rev-parse HEAD`) for each column. Those live numbers are the baseline. The synthetic table above is not.
