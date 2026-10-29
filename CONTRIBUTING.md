# Contributing

Run the test suites and the verify skill before opening a pull request.

```sh
python3 tests/guard_test.py
python3 tests/install_test.py
python3 tests/update_test.py
python3 tests/context_attribution_test.py
python3 tests/reset_test.py
python3 tests/overlap_test.py
python3 tests/overlap_eval_test.py
python3 tests/quiet_eval_test.py
python3 tests/poteto_test.py
```

Each suite prints `N/N passed`.

Then drive the change with `.claude/skills/verify/`. Tier 1 is `scripts/sandbox.sh`. A guard, install, update, pre-push, overlap, or launcher change needs a sandbox `doctor` with every line `ok`, plus the feature recipe for what you touched. See that skill for tier 2 and tier 3.
