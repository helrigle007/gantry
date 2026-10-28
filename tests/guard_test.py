#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "template", "kit")
sys.path.insert(0, KIT)
import gantry_kit
HOOK = os.path.join(KIT, "gantry-guard.py")
OWN = "example-user/example-fixture"
OTHER = "example-user/other"

C, W, O = "coordinator", "worker", "operator"
T = "{T}"
ALLOW, DENY = True, False


def bash(cmd):
    return "Bash", {"command": cmd}


ROWS = [
    (C, ("Edit", {"file_path": "src/a.py"}), DENY, {}),
    (C, ("Write", {"file_path": "src/a.py"}), DENY, {}),
    (C, ("MultiEdit", {"file_path": "src/a.py"}), DENY, {}),
    (C, ("NoSuchTool", {}), DENY, {}),
    (C, ("NotebookEdit", {"notebook_path": "n.ipynb"}), DENY, {}),
    (C, ("Agent", {"prompt": "x", "subagent_type": "Explore"}), DENY, {}),
    (C, ("Task", {"prompt": "x", "subagent_type": "general-purpose"}), DENY, {}),
    (C, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore", "run_in_background": True}), DENY, {"mark": "own"}),
    (C, ("Task", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "general-purpose", "run_in_background": True}), DENY, {"mark": "own"}),
    (C, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "general-purpose"}), DENY, {"mark": "own", "agent_type": "general-purpose"}),
    (C, bash("rm -rf x"), DENY, {"mark": "own"}),
    (W, ("Agent", {"description": "Implement slugify", "prompt": "write slug.py", "subagent_type": "general-purpose", "run_in_background": True}), ALLOW, {}),
    (W, ("Task", {"description": "Implement slugify", "prompt": "write slug.py", "subagent_type": "Explore"}), ALLOW, {}),
    (W, ("Agent", {"description": "Implement slugify", "prompt": "write slug.py", "subagent_type": "Explore"}), ALLOW, {"mark": "leaked"}),
    (C, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), DENY, {"mark": "own", "orca": "down", "ancestor_launched": True}),
    (C, bash("git push"), DENY, {"mark": "own", "orca": "down", "ancestor_launched": None}),
    (C, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), DENY, {"mark": "own", "orca": "down", "ancestor_launched": None}),
    (C, bash("git push"), DENY, {"as": "term_worker"}),
    (C, ("Agent", {"prompt": "x", "subagent_type": "Explore"}), DENY, {"as": "term_worker"}),
    (W, ("Agent", {"description": "Implement slugify", "prompt": "write slug.py", "subagent_type": "Explore"}), ALLOW, {"mark": "leaked", "orca": "down", "ancestor_launched": False}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore", "run_in_background": True}), ALLOW, {}),
    (O, ("Task", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "general-purpose"}), ALLOW, {}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), ALLOW, {"agent_type": "general-purpose"}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), DENY, {"agent_type": "Explore", "ancestor_launched": None}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), ALLOW, {"agent_type": "Explore", "ancestor_launched": False}),
    (O, bash("git push"), ALLOW, {"agent_type": "Explore", "ancestor_launched": None}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), DENY, {"ancestor_launched": None, "as": None}),
    (O, ("Task", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "general-purpose"}), DENY, {"ancestor_launched": None, "as": None}),
    (O, ("Read", {"file_path": "README.md"}), ALLOW, {"ancestor_launched": None, "as": None}),
    (O, bash("git push origin main"), DENY, {"ancestor_launched": None, "as": None}),
    (O, bash("git push origin main"), DENY, {"orca": "down", "ancestor_launched": None}),
    (O, ("Agent", {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}), DENY, {"orca": "down", "ancestor_launched": None}),
    (O, bash("orca orchestration task-list --run run_1"), DENY, {"orca": "down", "ancestor_launched": None}),
    (C, ("Workflow", {}), DENY, {}),
    (C, ("Monitor", {"command": "git status"}), DENY, {}),
    (C, ("Read", {"file_path": "README.md"}), ALLOW, {}),
    (C, bash("orca orchestration check --wait --timeout-ms 180000"), DENY, {}),
    (C, ("Bash", {"command": 'orca orchestration check --ack d1 --wait --types "worker_done,question,escalation" --timeout-ms 180000 --json', "run_in_background": True}), DENY, {}),
    (C, ("Bash", {"command": ".claude/bin/gantry-mailbox wait --timeout-ms 180000", "run_in_background": True}), ALLOW, {}),
    (C, bash(".claude/bin/gantry-mailbox wait --types heartbeat,worker_done,question,escalation --timeout-ms 180000"), ALLOW, {}),
    (C, bash("gantry overlap --checkpoint scoped --intent 'Fix the crash in app' --files src/app.py"), ALLOW, {}),
    (W, bash("gantry uninstall"), DENY, {}),
    (W, bash("gantry overlap --record classified --checkpoint pre-pr --run run_1 --task task_impl --verdict continue"), DENY, {}),
    (W, bash("python3 /tmp/gantry_kit.py uninstall"), DENY, {}),
    (W, bash("gantry charter add 'Always check the linked ticket'"), DENY, {}),
    (W, bash("gantry charter add --global 'Never force-push a shared branch.'"), DENY, {}),
    (W, bash("gantry charter rm r-fc5233"), DENY, {}),
    (W, bash("gantry charter list"), DENY, {}),
    (W, bash("python3 /opt/gantry_kit.py charter add x"), DENY, {}),
    (C, bash("gantry uninstall"), DENY, {}),
    (C, bash("gantry restart"), DENY, {}),
    (C, bash("gantry --global charter add x"), DENY, {}),
    (C, bash("gantry charter add 'Always check the linked ticket'"), ALLOW, {}),
    (C, bash("gantry charter add 'Always run the team'\\''s lint before a PR'"), ALLOW, {}),
    (C, bash("gantry charter add --global 'Never force-push a shared branch.'"), ALLOW, {}),
    (C, bash("gantry charter list"), ALLOW, {}),
    (C, bash("gantry charter rm r-fc5233"), ALLOW, {}),
    (W, bash("gantry ship 9"), DENY, {}),
    (W, bash("gantry cleanup task_impl"), DENY, {}),
    (W, bash("gantry ship 9"), DENY, {"as": "term_ship"}),
    (C, bash("gantry ship 9"), ALLOW, {}),
    (C, bash("gantry ship 9 --task task_impl"), ALLOW, {}),
    (C, bash("python3 /opt/gantry_kit.py ship 9"), ALLOW, {}),
    (C, bash("gantry ship 9"), DENY, {"auto": False, "reason": "autoMerge is false"}),
    (C, bash("gantry ship 9 --admin"), DENY, {}),
    (C, bash("gantry ship 9 --auto"), DENY, {}),
    (C, bash("gantry ship 9 --force"), DENY, {}),
    (C, bash("gantry ship 9 --force-with-lease"), DENY, {}),
    (C, bash("gantry ship 9 -f"), DENY, {}),
    (C, bash("python3 /opt/gantry_kit.py ship 9 --admin"), DENY, {}),
    (C, bash("x=ship; gantry $x 9"), DENY, {}),
    (C, bash("gantry cleanup task_impl"), ALLOW, {}),
    (C, bash("gantry cleanup --scan"), ALLOW, {}),
    (C, bash("gantry cleanup --dry-run task_impl"), ALLOW, {}),
    (C, bash("gantry cleanup --finished"), ALLOW, {}),
    (C, bash("gantry wait"), ALLOW, {}),
    (C, bash("gantry wait --run run_1"), ALLOW, {}),
    (C, bash("gantry wait --dispatch ctx_a --dispatch ctx_b"), ALLOW, {}),
    (W, bash("gantry wait"), DENY, {}),
    (W, bash("gantry wait --run run_1"), DENY, {"as": "term_ship"}),
    (C, bash("python3 /opt/gantry_kit.py cleanup task_impl"), ALLOW, {}),
    (C, bash("gantry cleanup --force task_impl"), DENY, {}),
    (C, bash("gantry cleanup task_impl -f"), DENY, {}),
    (C, bash("gh pr merge 3 --squash --match-head-commit abc"), DENY, {"reason": "never runs gh pr merge"}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry/charters/global.json"}), DENY, {}),
    (W, ("Edit", {"file_path": "~/.gantry/charters/repos/github.com/example-user/example-fixture.json"}), DENY, {}),
    (W, ("MultiEdit", {"file_path": f"{T}/home/.gantry/registry.json"}), DENY, {}),
    (W, ("NotebookEdit", {"notebook_path": "~/.gantry/charters/n.ipynb"}), DENY, {}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry/current/kit/gantry-guard.py"}), DENY, {}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry"}), DENY, {}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry/charters/global.json"}), DENY, {"as": "term_ship"}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry-notes/a.md"}), ALLOW, {}),
    (W, ("Read", {"file_path": f"{T}/home/.gantry/charters/global.json"}), ALLOW, {}),
    (W, bash("cat ~/.gantry/charters/global.json"), ALLOW, {}),
    (O, ("Write", {"file_path": f"{T}/home/.gantry/charters/global.json"}), ALLOW, {}),
    (W, ("Write", {"file_path": f"{T}/home/.gantry/charters/global.json"}), ALLOW, {"cwd": T}),
    (W, bash("gh pr new --title x --body y"), DENY, {"reason": "pre-pr check"}),
    (W, bash("gh api --method POST repos/example-user/example-fixture/pulls/"), DENY, {"reason": "pre-pr check"}),
    (W, bash("git send-pack origin HEAD:refs/heads/main"), DENY, {}),
    (W, bash("git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/feat"), DENY, {}),
    (W, bash("git remote set-head origin feat"), DENY, {}),
    (W, bash("gh pr create --title x --body y"), DENY, {"reason": "pre-pr check"}),
    (C, ("Read", {"file_path": "/tmp/task-output/b1.output"}), ALLOW, {}),
    (C, bash("orca orchestration gate-list --run run_1 --json"), ALLOW, {}),
    (C, bash("orca orchestration worker-start --task t1"), ALLOW, {}),
    (C, bash("orca orchestration worker-start --spec 'land #3' --task-title gantry-ship --worktree new-child --agent claude --json"), ALLOW, {}),
    (C, bash("orca orchestration reset"), DENY, {}),
    (C, bash("orca worktree ps --json"), ALLOW, {}),
    (C, bash("orca terminal read --terminal t"), ALLOW, {}),
    (C, bash("orca terminal send --terminal t --text hi"), DENY, {}),
    (C, bash("orca terminal send --terminal term_worker --text hi --enter"), ALLOW, {}),
    (C, bash("orca terminal send --terminal term_done --text hi --enter"), ALLOW, {}),
    (C, bash("orca terminal send --text hi --enter"), DENY, {}),
    (C, bash("orca terminal send --terminal term_far --text hi --enter"), DENY, {}),
    (C, bash("orca terminal send --terminal term_main --text hi --enter"), DENY, {}),
    (W, bash("orca terminal send --terminal term_worker --text hi --enter"), DENY, {}),
    (W, bash("orca terminal send --terminal term_done --text hi --enter"), DENY, {"as": "term_clean"}),
    (C, bash("orca terminal create"), DENY, {}),
    (C, bash("orca terminal split"), DENY, {}),
    (C, bash("orca terminal rename --terminal term_worker"), DENY, {}),
    (C, bash("orca terminal switch --terminal term_worker"), DENY, {}),
    (C, bash("gh pr merge 1"), DENY, {}),
    (C, bash("gh pr view 1 --json state | jq .state"), ALLOW, {}),
    (C, bash("git status && git log --oneline -5 2>&1 | head"), ALLOW, {}),
    (C, bash("git branch --show-current"), ALLOW, {}),
    (C, bash("git branch newbranch"), DENY, {}),
    (C, bash("git diff --output=x.patch"), DENY, {}),
    (C, bash("rm -rf x"), DENY, {}),
    (C, bash("echo hi > f"), DENY, {}),
    (C, bash("echo hi >/dev/null 2>&1"), ALLOW, {}),
    (C, bash("cat <<EOF\nhi\nEOF"), DENY, {}),
    (C, bash("git commit -m x"), DENY, {}),
    (C, bash("git fetch origin"), ALLOW, {}),
    (C, bash("git fetch origin main"), ALLOW, {}),
    (C, bash("git fetch origin main"), ALLOW, {"on": "main", "dirty": True}),
    (C, bash("git fetch"), DENY, {}),
    (C, bash("git fetch https://github.com/someone/else.git"), DENY, {}),
    (C, bash("git fetch origin feat"), DENY, {}),
    (C, bash("git fetch --prune origin"), DENY, {}),
    (C, bash("git fetch origin +main:main"), DENY, {"on": "main"}),
    (C, bash("git fetch origin main:feat"), DENY, {"on": "main"}),
    (C, bash("git fetch origin main:main"), DENY, {}),
    (C, bash("git fetch origin main:main"), ALLOW, {"on": "main"}),
    (C, bash("git fetch origin main:main"), DENY, {"on": "main", "dirty": True}),
    (C, bash(f"git -C {T}/main fetch origin main:main"), ALLOW, {}),
    (C, bash("git pull --ff-only"), ALLOW, {"on": "main"}),
    (C, bash("git pull --ff-only origin main"), ALLOW, {"on": "main"}),
    (C, bash("git merge --ff-only origin/main"), ALLOW, {"on": "main"}),
    (C, bash("git pull"), DENY, {"on": "main"}),
    (C, bash("git pull --rebase"), DENY, {"on": "main"}),
    (C, bash("git pull --no-ff origin main"), DENY, {"on": "main"}),
    (C, bash("git pull --ff-only origin feat"), DENY, {"on": "main"}),
    (C, bash("git pull --ff-only"), DENY, {}),
    (C, bash("git pull --ff-only"), DENY, {"on": "main", "dirty": True}),
    (C, bash("git merge --ff-only origin/feat"), DENY, {"on": "main"}),
    (C, bash("git merge origin/main"), DENY, {"on": "main"}),
    (C, bash("git rebase origin/main"), DENY, {"on": "main"}),
    (C, bash("git reset --hard origin/main"), DENY, {"on": "main"}),
    (C, bash("git checkout main"), DENY, {}),
    (C, bash("git switch main"), DENY, {}),
    (C, bash("git branch -d feat"), DENY, {}),
    (W, bash("git pull --ff-only"), ALLOW, {}),
    (C, bash("npm test"), DENY, {}),
    (C, bash("git push"), DENY, {}),
    (C, bash("bash -c 'git status'"), ALLOW, {}),
    (W, bash("git push origin main"), DENY, {}),
    (W, bash("git push"), DENY, {"on": "main"}),
    (W, bash("git push"), ALLOW, {}),
    (W, bash("git push origin HEAD:main"), DENY, {}),
    (W, bash("git push origin HEAD:refs/heads/main"), DENY, {}),
    (W, bash("git push --force origin feat"), DENY, {}),
    (W, bash("git push -f origin feat"), DENY, {}),
    (W, bash("git push -fu origin feat"), DENY, {}),
    (W, bash("git push origin +feat"), DENY, {}),
    (W, bash("git push origin --mirror"), DENY, {}),
    (W, bash("git push --no-verify origin feat"), DENY, {}),
    (W, bash("git push --force-with-lease=refs/heads/feat:abc origin HEAD:refs/heads/feat"), ALLOW, {}),
    (W, bash("git push -u origin feat"), ALLOW, {}),
    (W, bash("git push https://github.com/someone/else.git feat"), DENY, {}),
    (W, bash("git push https://github.com/example-user/example-fixture.git feat"), ALLOW, {}),
    (W, bash("git -C . push origin main"), DENY, {}),
    (W, bash("git -C $D/pr4 push origin HEAD:refs/heads/feat"), DENY, {}),
    (W, bash("cd x && git push origin main"), DENY, {}),
    (W, bash('bash -c "git push origin main"'), DENY, {}),
    (W, bash("echo $(git push -f origin x)"), DENY, {}),
    (W, bash('echo "$(git push -f origin x)"'), DENY, {}),
    (W, bash("echo `git push origin main`"), DENY, {}),
    (W, bash("echo a#b\ngit push origin main"), DENY, {}),
    (W, bash("git push \\\n origin main"), DENY, {}),
    (W, bash("timeout 30 git push origin main"), DENY, {}),
    (W, bash("echo x | xargs git push origin"), DENY, {}),
    (W, bash("eval 'git status'"), DENY, {}),
    (W, bash("if true; then git push origin main; fi"), DENY, {}),
    (W, bash("{ git push origin main; }"), DENY, {}),
    (W, bash("! git push origin main"), DENY, {}),
    (W, bash("for x in 1; do git push origin main; done"), DENY, {}),
    (W, bash("while true; do git push origin main; break; done"), DENY, {}),
    (W, bash("until false; do git push origin main; break; done"), DENY, {}),
    (W, bash("case a in a) git push origin main;; esac"), DENY, {}),
    (W, bash("select x in a; do git push origin main; break; done </dev/null"), DENY, {}),
    (W, bash("f() { git push origin main; }; f"), DENY, {}),
    (W, bash("function f { git push origin main; }; f"), DENY, {}),
    (W, bash("if true; then git status; fi"), ALLOW, {}),
    (W, bash("{ git status; }"), ALLOW, {}),
    (W, bash("bash scripts/x.sh"), ALLOW, {}),
    (W, bash("sh scripts/x.sh"), ALLOW, {}),
    (W, bash("zsh scripts/x.sh"), ALLOW, {}),
    (W, bash("source push.sh"), DENY, {}),
    (W, bash(". ./push.sh"), DENY, {}),
    (C, bash("source push.sh"), DENY, {}),
    (C, bash(". ./push.sh"), DENY, {}),
    (C, bash("bash scripts/x.sh"), DENY, {}),
    (C, bash("sh scripts/x.sh"), DENY, {}),
    (C, bash("if true; then git status; fi"), ALLOW, {}),
    (C, bash("if true; then git push origin main; fi"), DENY, {}),
    (W, bash("x=push; git $x origin main"), DENY, {}),
    (W, bash("x=main; git push origin $x"), DENY, {}),
    (W, bash("x=main; git push origin HEAD:$x"), DENY, {}),
    (W, bash("d=--force; git push origin feat $d"), DENY, {}),
    (W, bash("cmd=git; $cmd push origin main"), DENY, {}),
    (W, bash("m=merge; gh pr $m 3 --squash"), DENY, {}),
    (W, bash("exec git push origin $x"), DENY, {}),
    (W, bash("npm test"), ALLOW, {}),
    (W, bash("python3 -m pytest -q"), ALLOW, {}),
    (W, bash("git add -A && git commit -m wip && git push -u origin feat"), ALLOW, {}),
    (W, bash("gt submit"), DENY, {}),
    (W, bash("git remote add evil https://github.com/x/y"), DENY, {}),
    (W, bash("git config url.https://evil/.insteadOf https://github.com/"), DENY, {}),
    (W, bash("git config user.name bot"), ALLOW, {}),
    (W, bash("git -c core.hooksPath=/dev/null commit -m x"), DENY, {}),
    (W, bash("git fetch https://github.com/someone/else.git"), DENY, {}),
    (W, bash("git fetch origin"), ALLOW, {}),
    (W, bash("git branch -D main"), DENY, {}),
    (W, bash("git branch -m main trunk"), DENY, {}),
    (W, bash("git branch -d old"), ALLOW, {}),
    (W, bash("git update-ref refs/heads/main HEAD"), DENY, {}),
    (W, bash("orca orchestration run-create --spec x"), DENY, {}),
    (W, bash("orca orchestration worker-start --task t"), DENY, {}),
    (W, bash("orca orchestration task-update --task t --task-title gantry-ship"), DENY, {}),
    (W, bash("orca orchestration send --to coordinator --body done"), ALLOW, {}),
    (W, bash("orca orchestration ask --to coordinator --body q"), ALLOW, {}),
    (W, bash("orca orchestration check"), ALLOW, {}),
    (W, bash("/Applications/Orca.app/Contents/Resources/bin/orca terminal list"), DENY, {}),
    (W, bash("orca status"), ALLOW, {}),
    (W, bash("gh repo delete x"), DENY, {}),
    (W, bash("gh repo view"), ALLOW, {}),
    (W, bash("gh label delete bug"), DENY, {}),
    (W, bash("gh secret list"), DENY, {}),
    (W, bash("gh auth login"), DENY, {}),
    (W, bash(f"gh api -X DELETE repos/{OTHER}"), DENY, {}),
    (W, bash(f"gh api -X DELETE repos/{OWN}"), DENY, {}),
    (W, bash(f"gh api repos/{OTHER}/branches/main/protection -X PUT"), DENY, {}),
    (W, bash(f"gh api repos/{OWN}/branches/main/protection -X PUT"), DENY, {}),
    (W, bash(f"gh api -X PUT repos/{OWN}/pulls/3/merge"), DENY, {}),
    (W, bash(f'gh api --method POST "repos/{OWN}/pulls/7/comments/123/replies" --input payload.json'), ALLOW, {}),
    (W, bash("gh api repos/{owner}/{repo}/pulls/7/comments --paginate"), ALLOW, {}),
    (W, bash("gh api repos/someone/else/pulls"), DENY, {}),
    (W, bash("gh api -X PATCH user -f name=x"), DENY, {}),
    (W, bash("gh api graphql -f query='mutation { deleteRef(input:{}) { clientMutationId } }'"), DENY, {}),
    (W, bash("gh api graphql -f query='query { viewer { login } }'"), ALLOW, {}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), ALLOW, {"auto": True, "as": "term_ship"}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), ALLOW, {"as": "term_ship"}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), DENY, {"auto": False, "as": "term_ship"}),
    (W, bash("gh pr merge 3 --squash"), DENY, {"as": "term_ship"}),
    (W, bash("gh pr merge 3 --admin --match-head-commit a"), DENY, {"as": "term_ship"}),
    (W, bash("gh pr merge 3 --auto --match-head-commit a"), DENY, {"as": "term_ship"}),
    (W, bash("gh pr merge 3 -s -d --match-head-commit a"), ALLOW, {"as": "term_ship"}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), ALLOW, {"as": "term_ship_kept"}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), ALLOW, {"as": "term_ship_done"}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), DENY, {}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), DENY, {"auto": True}),
    (W, bash("gh pr merge 3 --squash --match-head-commit abc"), DENY, {"as": "term_ship", "orca": "down"}),
    (W, bash("gh pr view 3"), ALLOW, {}),
    (W, bash("git log | grep example-work-account"), ALLOW, {}),
    (W, bash("git log | grep Example-Work-Account"), DENY, {"forbidden": ["example-work-account"], "reason": "forbidden"}),
    (C, bash("echo example-work-account"), DENY, {"forbidden": ["example-work-account"], "reason": "forbidden"}),
    (W, bash("echo example-work-account"), ALLOW, {"forbidden": []}),
    (W, bash("echo ok"), ALLOW, {"forbidden": [""]}),
    (W, bash("echo ok"), ALLOW, {"forbidden": [" "]}),
    (W, bash("echo example-work-account"), DENY, {"forbidden": [" example-work-account "], "reason": "forbidden"}),
    (W, bash("echo example-work''-account"), DENY, {"forbidden": ["example-work-account"], "reason": "forbidden"}),
    (W, bash("git status"), ALLOW, {"forbidden": ["example-work-account"]}),
    (W, bash("echo example-org/example-repo"), DENY, {"forbidden": ["example-org/example-repo"], "reason": "forbidden"}),
    (W, bash("ssh user@example-host true"), DENY, {"forbidden": ["user@example-host"], "reason": "forbidden"}),
    (O, bash("echo example-work-account"), ALLOW, {"forbidden": ["example-work-account"]}),
    (W, bash("git status"), DENY, {"forbidden": "example-work-account", "reason": "list of strings"}),
    (W, bash("git status"), DENY, {"forbidden": ["example-work-account", 1], "reason": "list of strings"}),
    (W, bash("echo example-work-account"), DENY, {"raw": '{"forbidden": ["example-work-account"],}', "reason": "JSON object"}),
    (W, bash("echo parent-marker"), ALLOW, {}),
    (W, ("Monitor", {"command": "echo example-work-account"}), DENY, {"forbidden": ["example-work-account"], "reason": "forbidden"}),
    (W, bash("GH_TOKEN=x gh pr list"), DENY, {}),
    (W, bash("env GIT_SSH_COMMAND=x git fetch"), DENY, {}),
    (W, bash("export GANTRY_X=1"), DENY, {}),
    (W, bash("gh pr list -R someone/else"), DENY, {}),
    (W, bash(f"gh pr list -R {OWN}"), ALLOW, {}),
    (W, ("Read", {"file_path": "/x/skills/poteto-mode/playbooks/orchestrate.md"}), DENY, {}),
    (W, bash("cat ~/.claude/skills/poteto-mode/scripts/orch/run.sh"), DENY, {}),
    (W, ("Write", {"file_path": ".claude/hooks/gantry-guard.py"}), DENY, {}),
    (W, ("Edit", {"file_path": ".claude/settings.json"}), DENY, {}),
    (W, ("Write", {"file_path": ".claude/bin/gantry-mailbox"}), DENY, {}),
    (W, ("Write", {"file_path": ".claude/bin/gantry_run.py"}), DENY, {}),
    (W, ("Write", {"file_path": ".claude/bin/gantry_run/__init__.py"}), DENY, {}),
    (W, bash("cp /tmp/x .claude/bin/gantry-mailbox"), DENY, {}),
    (W, bash("cp /tmp/x .claude/bin/gantry_run.py"), DENY, {}),
    (W, ("Write", {"file_path": ".git/config"}), DENY, {}),
    (W, ("Write", {"file_path": "src/foo.py"}), ALLOW, {}),
    (W, ("MultiEdit", {"file_path": ".claude/hooks/gantry-guard.py"}), DENY, {}),
    (W, ("MultiEdit", {"file_path": "src/foo.py"}), ALLOW, {}),
    (W, bash("sed -i s/a/b/ .claude/settings.json"), DENY, {}),
    (W, bash("cat x > .claude/gantry.json"), DENY, {}),
    (W, bash("cat .claude/settings.json"), ALLOW, {}),
    (W, bash("git diff .claude/settings.json"), ALLOW, {}),
    (W, ("RemoteTrigger", {}), DENY, {}),
    (C, ("RemoteTrigger", {}), DENY, {}),
    (W, ("Monitor", {"command": "git push origin main"}), DENY, {}),
    (W, bash("npm test && git add -A && git commit -m wip"), ALLOW, {}),
    (W, bash("git push origin --delete done"), ALLOW, {"merged": {"done"}}),
    (W, bash("git push origin --delete open"), DENY, {"merged": {"done"}}),
    (W, bash("git push origin :done"), ALLOW, {"merged": {"done"}}),
    (W, bash("git branch -D done"), ALLOW, {"merged": {"done"}}),
    (W, bash("git branch -D open"), DENY, {"merged": {"done"}}),
    (W, bash(f"gh api -X DELETE repos/{OWN}/git/refs/heads/done"), ALLOW, {"merged": {"done"}}),
    (W, bash(f"gh api -X DELETE repos/{OWN}/git/refs/heads/open"), DENY, {"merged": {"done"}}),
    (W, bash("ssh user@example-host 'sudo env GIT_SSH_COMMAND=\"ssh -i $(echo ~)/.ssh/k\" git clone git@github.com:x/y.git /opt/y'"), ALLOW, {}),
    (W, bash("echo '`git push origin main`'"), ALLOW, {}),
    (W, bash('echo "$(GIT_SSH_COMMAND=x git fetch)"'), DENY, {}),
    (W, bash("echo \"it's $(git push origin main)\""), DENY, {}),
    (W, bash("echo $(echo $(git push origin main))"), DENY, {}),
    (W, bash("orca orchestration task-list --run run_1"), DENY, {}),
    (O, bash("orca orchestration task-list --run run_b785451d144e --json"), ALLOW, {}),
    (O, bash("orca orchestration task-list --run run_1"), ALLOW, {"as": None}),
    (O, bash("gh repo deploy-key add k.pub --title example-host"), ALLOW, {}),
    (O, bash("gh repo deploy-key list"), ALLOW, {}),
    (O, bash("ssh user@example-host 'sudo env GIT_SSH_COMMAND=\"ssh -i $(echo ~)/.ssh/k\" git clone git@github.com:x/y.git /opt/y'"), ALLOW, {}),
    (O, bash("sed -n 1,40p .claude/hooks/gantry-guard.py"), ALLOW, {}),
    (O, ("Edit", {"file_path": ".claude/hooks/gantry-guard.py"}), ALLOW, {}),
    (O, bash("orca orchestration task-list --run run_1"), DENY, {"orca": "down"}),
    (W, bash("orca orchestration task-list --run run_1"), DENY, {"as": "term_ship_released"}),
    (C, bash("git status"), ALLOW, {"orca": "down"}),
    (C, bash("rm -rf x"), DENY, {"orca": "down"}),
    (C, bash("orca terminal close --terminal term_done"), DENY, {}),
    (C, bash(f"orca worktree rm --worktree path:{T}/wt-done"), DENY, {}),
    (W, bash("orca terminal close --terminal term_done"), ALLOW, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_ship_released --tab"), ALLOW, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_done"), DENY, {}),
    (W, bash("orca terminal close --terminal term_busy"), DENY, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_far"), ALLOW, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_far"), DENY, {}),
    (W, bash("orca terminal close --terminal term_takeover"), ALLOW, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_takeover_live"), DENY, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_main"), DENY, {"as": "term_clean"}),
    (W, bash("orca terminal close --terminal term_clean"), DENY, {"as": "term_clean"}),
    (W, bash("orca terminal close --worktree active --all"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-done --json"), ALLOW, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-done"), DENY, {"as": "term_clean", "cwd": f"{T}/wt-done"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-live"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-ahead"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-gone"), ALLOW, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-open"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-dirty"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/main"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-done --force"), DENY, {"as": "term_clean"}),
    (W, bash("orca worktree rm --worktree name:wt-done"), DENY, {"as": "term_clean"}),
    (W, bash(f"orca worktree rm --worktree path:{T}/wt-done"), DENY, {}),
    (W, bash(f"git worktree remove {T}/wt-done"), ALLOW, {}),
    (W, bash(f"git worktree remove {T}/wt-done"), DENY, {"cwd": f"{T}/wt-done"}),
    (W, bash(f"git worktree remove {T}/wt-live"), DENY, {}),
    (W, bash(f"git worktree remove {T}/wt-ahead"), DENY, {}),
    (W, bash("orca worktree ps --json"), DENY, {}),
    (W, bash(f"git worktree remove {T}/wt-gone"), ALLOW, {}),
    (W, bash(f"git worktree remove {T}/wt-open"), DENY, {}),
    (W, bash(f"git worktree remove --force {T}/wt-done"), DENY, {}),
    (W, bash("git worktree list"), ALLOW, {}),
]


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


# Remote branches: done and dirty merged, gone deleted on origin, open still open.
MERGED, ON_ORIGIN = {"done", "dirty"}, {"main", "feat", "done", "dirty", "open"}


def make_fixture(tmp):
    main, wt = os.path.join(tmp, "main"), os.path.join(tmp, "wt")
    os.makedirs(main)
    sh("git", "init", "-q", "-b", "main", cwd=main)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    sh("git", "remote", "add", "origin", f"https://github.com/{OWN}.git", cwd=main)
    sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=main)
    sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=main)
    sh("git", "config", "branch.main.remote", "origin", cwd=main)
    sh("git", "config", "branch.main.merge", "refs/heads/main", cwd=main)
    sh("git", "worktree", "add", "-q", "-b", "feat", wt, cwd=main)
    for b in ("done", "dirty", "gone", "open", "live"):
        sh("git", "worktree", "add", "-q", "-b", b, os.path.join(tmp, f"wt-{b}"), cwd=main)
    open(os.path.join(tmp, "wt-dirty", "scratch.txt"), "w").close()
    ahead = os.path.join(tmp, "wt-ahead")
    sh("git", "worktree", "add", "-q", "-b", "ahead", ahead, cwd=main)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "ahead", cwd=ahead)
    return main, wt


def config_path(repo):
    common = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True).strip()
    return os.path.join(common, "gantry.json")


def set_auto(main, auto, forbidden=None):
    path = config_path(main)
    if auto is None and forbidden is None:
        if os.path.exists(path):
            os.remove(path)
        return
    data = {}
    if auto is not None:
        data["autoMerge"] = auto
    if forbidden is not None:
        data["forbidden"] = forbidden
    with open(path, "w") as f:
        json.dump(data, f)


def load_guard():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("gantry_guard", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def worker(handle, task, state, status, run="run_1", **extra):
    row = {"agentTerminalHandle": handle, "taskId": task, "runId": run, "terminalState": state, "dispatchStatus": status}
    row.update(extra)
    return row


# Newest first, like worker-list. term_main is the operator's own Orca terminal and has no row.
WORKERS = [
    worker("term_worker", "task_impl", "active", "running"),
    worker("term_ship", "task_ship", "active", "running"),
    worker("term_ship_kept", "task_ship_wt", "retained", "running"),
    worker("term_clean", "task_clean", "retained", "running"),
    worker("term_busy", "task_impl2", "active", "running"),
    worker("term_done", "task_impl3", "retained", "completed"),
    worker("term_ship_done", "task_ship_wt2", "reclaimable", "running"),
    worker("term_ship_released", "task_ship_old", "released", "completed"),
    worker("term_far", "task_far", "retained", "completed", run="run_2"),
    worker("term_takeover", "task_takeover", "retained", "completed", run="run_2", retainedReason="user_takeover"),
    worker("term_takeover_live", "task_live_takeover", "retained", "running", run="run_2", retainedReason="user_takeover"),
]
TASKS = {"run_1": [{"id": "task_impl", "task_title": "A: impl"}, {"id": "task_ship", "task_title": "gantry-ship"},
                   {"id": "task_ship_wt", "task_title": "gantry-ship"}, {"id": "task_clean", "task_title": "gantry-cleanup"},
                   {"id": "task_impl2", "task_title": "B: impl"}, {"id": "task_impl3", "task_title": "C: impl"},
                   {"id": "task_ship_wt2", "task_title": "gantry-ship"}, {"id": "task_ship_old", "task_title": "gantry-ship"}],
         "run_2": [{"id": "task_far", "task_title": "D: impl"}, {"id": "task_takeover", "task_title": "E: read"},
                   {"id": "task_live_takeover", "task_title": "F: read"}]}
PAGE = 4


def fake_orca(*args):
    args = list(args)
    if args[0] == "run-current":
        return {"run": {"id": "run_1"}}
    run = args[args.index("--run") + 1] if "--run" in args else None
    if args[0] == "task-list":
        return {"tasks": TASKS.get(run, [])}
    rows = [w for w in WORKERS if run in (None, w["runId"])]
    start = int(args[args.index("--cursor") + 1]) if "--cursor" in args else 0
    more = start + PAGE < len(rows)
    return {"workers": rows[start:start + PAGE], "page": {"nextCursor": str(start + PAGE) if more else None, "hasMore": more}}


def orca_down(*args):
    raise RuntimeError("orca orchestration exited 1")


FAKE_ORCA_BIN = f"""#!{sys.executable}
import importlib.util, json, sys
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("guard_test", {os.path.abspath(__file__)!r})
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
print(json.dumps({{"ok": True, "result": t.fake_orca(*[a for a in sys.argv[2:] if a != "--json"])}}))
"""


def run_hook(payload, handle, bindir, extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("ORCA_TERMINAL_HANDLE", "GANTRY_COORDINATOR_HANDLE")}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    if handle:
        env["ORCA_TERMINAL_HANDLE"] = handle
    if extra:
        env.update(extra)
    r = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True, env=env)
    return r.returncode, r.stderr.strip()


def pretool(cwd, tool, tool_input, **extra):
    body = {"session_id": "sess_test", "hook_event_name": "PreToolUse", "cwd": cwd,
            "permission_mode": "bypassPermissions", "tool_name": tool, "tool_input": tool_input,
            "tool_use_id": "toolu_test"}
    body.update(extra)
    return json.dumps(body)


def hook_checks(tmp, wt):
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "orca"), "w") as f:
        f.write(FAKE_ORCA_BIN)
    os.chmod(os.path.join(bindir, "orca"), 0o755)

    def payload(cmd, **extra):
        return json.dumps({"cwd": wt, "tool_name": "Bash", "tool_input": {"command": cmd}, **extra})

    spawn = {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore", "run_in_background": True}
    cases = [
        ("worker terminal gets worker rules", payload("git push origin main"), "term_worker", 2, None),
        ("worker in page 2 of worker-list gets worker rules", payload("orca orchestration task-list"), "term_done", 2, None),
        ("operator's Orca terminal is unrestricted", payload("orca orchestration task-list --run run_1"), "term_main", 0, None),
        ("session outside Orca is unrestricted", payload("gh repo deploy-key list"), None, 0, None),
        ("coordinator keeps coordinator rules", payload("rm -rf x", agent_type="gantry-coordinator"), "term_main", 2, None),
        ("coordinator with agent_type cannot spawn Agent", pretool(wt, "Agent", spawn, agent_type="gantry-coordinator"), "term_main", 2, None),
        ("coordinator with agent_type cannot spawn Task", pretool(wt, "Task", {**spawn, "subagent_type": "general-purpose"}, agent_type="gantry-coordinator"), "term_main", 2, None),
        ("coordinator without agent_type cannot spawn Agent", pretool(wt, "Agent", spawn), "term_main", 2, {"GANTRY_COORDINATOR_HANDLE": "term_main"}),
        ("coordinator without agent_type cannot spawn Task", pretool(wt, "Task", {**spawn, "subagent_type": "general-purpose"}), "term_main", 2, {"GANTRY_COORDINATOR_HANDLE": "term_main"}),
        ("coordinator marked general-purpose cannot spawn Agent", pretool(wt, "Agent", {**spawn, "subagent_type": "general-purpose"}, agent_type="general-purpose"), "term_main", 2, {"GANTRY_COORDINATOR_HANDLE": "term_main"}),
        ("literal GANTRY_COORDINATOR_HANDLE=1 does not deny Agent", pretool(wt, "Agent", spawn), "term_main", 0, {"GANTRY_COORDINATOR_HANDLE": "1"}),
        ("operator Orca terminal can spawn Agent", pretool(wt, "Agent", spawn), "term_main", 0, None),
        ("operator Orca terminal can spawn Task", pretool(wt, "Task", {**spawn, "subagent_type": "general-purpose"}), "term_main", 0, None),
        ("worker can spawn Agent", pretool(wt, "Agent", {**spawn, "description": "Implement slugify", "prompt": "write slug.py", "subagent_type": "general-purpose"}), "term_worker", 0, None),
        ("worker can spawn Task", pretool(wt, "Task", {**spawn, "description": "Implement slugify", "prompt": "write slug.py"}), "term_worker", 0, None),
        ("worker keeps Agent when coordinator env leaked", pretool(wt, "Agent", spawn), "term_worker", 0, {"GANTRY_COORDINATOR_HANDLE": "term_main"}),
        ("retained ship worker merges", payload("gh pr merge 3 --squash --match-head-commit abc"), "term_ship_kept", 0, None),
        ("malformed stdin fails closed", "not json", "term_worker", 2, None),
        ("missing tool_input fails closed", json.dumps({"tool_name": "Bash", "cwd": wt}), "term_worker", 2, None),
    ]
    checks = []
    for name, text, handle, want, extra in cases:
        code, reason = run_hook(text, handle, bindir, extra)
        ok = code == want and (code == 0 or reason.startswith("gantry guard:"))
        checks.append((name, ok, f"exit {code} {reason}"))
    checks += argv_checks(tmp, bindir, wt)
    checks.extend(installed_hook_checks(tmp, wt, bindir))
    return checks


PARENT = """import subprocess, sys
payload = sys.stdin.buffer.read()
proc = subprocess.run([sys.executable, sys.argv[1]], input=payload, capture_output=True)
sys.stderr.buffer.write(proc.stderr)
raise SystemExit(proc.returncode)
"""


def argv_checks(tmp, bindir, wt):
    parent = os.path.join(tmp, "parent.py")
    with open(parent, "w") as f:
        f.write(PARENT)
    spawn = {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore", "run_in_background": True}
    base = {k: v for k, v in os.environ.items() if k not in ("ORCA_TERMINAL_HANDLE", "GANTRY_COORDINATOR_HANDLE")}
    base["PATH"] = f"{bindir}:{base.get('PATH', '')}"

    def run(tool, argv, handle=None):
        env = dict(base)
        if handle:
            env["ORCA_TERMINAL_HANDLE"] = handle
        proc = subprocess.run([sys.executable, parent, HOOK, *argv], input=pretool(wt, tool, spawn),
                              capture_output=True, text=True, env=env)
        return proc.returncode, proc.stderr.strip()

    cases = [
        ("argv --agent denies Agent with no agent_type", "Agent", ["--agent", "gantry-coordinator"], None, 2),
        ("argv --agent= denies Task with no agent_type", "Task", ["--agent=gantry-coordinator"], None, 2),
        ("argv --agent in an Orca terminal denies Agent", "Agent", ["--agent", "gantry-coordinator"], "term_main", 2),
        ("argv without --agent in an Orca terminal allows Agent", "Agent", [], "term_main", 0),
    ]
    checks = []
    for name, tool, argv, handle, want in cases:
        code, reason = run(tool, argv, handle)
        ok = code == want and (code == 0 or reason.startswith("gantry guard:"))
        checks.append((name, ok, f"exit {code} {reason}"))
    return checks


def launch_checks():
    guard = load_guard()
    checks = []
    samples = [
        ("apostrophe objective keeps the flag", "claude fix the user's login --agent gantry-coordinator", True),
        ("trailing backslash keeps the flag", "claude fix the login\\ --agent gantry-coordinator", True),
        ("equals form", "claude --agent=gantry-coordinator fix", True),
        ("flag inside the objective is treated as the launcher", "claude fix --agent gantry-coordinator the login", True),
        ("plain command is not the launcher", "claude fix the login", False),
        ("other agent is not the launcher", "claude --agent explore fix", False),
        ("name prefix is not the launcher", "claude --agent gantry-coordinator-extra", False),
        ("unmatched quote still matches the raw flag", "claude 'fix the user's login' --agent gantry-coordinator", True),
    ]
    for name, command, expect in samples:
        got = guard.command_marks_coordinator(command)
        checks.append((name, got is expect, repr(got)))

    def rows(mapping):
        return lambda pid: mapping.get(pid)

    got = guard.walk_rows(100, rows({100: (50, False), 50: (1, True)}))
    checks.append(("walk finds an ancestor flag", got is True, repr(got)))
    got = guard.walk_rows(100, rows({100: (50, False), 50: (1, False)}))
    checks.append(("walk to init is not the coordinator", got is False, repr(got)))
    got = guard.walk_rows(100, rows({100: (50, False)}))
    checks.append(("missing ancestor is unreadable", got is None, repr(got)))
    chain = {i: (i - 1, False) for i in range(8, 41)}
    got = guard.walk_rows(40, rows(chain))
    checks.append(("walk cap without init is unreadable", got is None, repr(got)))
    text = "  10  1 claude fix the user's login --agent gantry-coordinator\n    5  1 claude fix the login\n"
    table = guard.parse_ps_table(text)
    got = guard.walk_rows(10, lambda pid: guard.ps_row(pid, table))
    checks.append(("ps line with an apostrophe matches", got is True, repr(got)))
    text = "  11  1 claude fix the login\\ --agent gantry-coordinator\n"
    table = guard.parse_ps_table(text)
    got = guard.walk_rows(11, lambda pid: guard.ps_row(pid, table))
    checks.append(("ps line with a trailing backslash matches", got is True, repr(got)))
    text = "  12  1 claude fix the login\n"
    table = guard.parse_ps_table(text)
    got = guard.walk_rows(12, lambda pid: guard.ps_row(pid, table))
    checks.append(("ps line without the flag is not the coordinator", got is False, repr(got)))
    return checks


def owner_checks():
    guard = load_guard()
    guard.orca = fake_orca
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        main_dir = os.path.realpath(os.path.join(tmp, "main"))
        os.makedirs(main_dir)
        sh("git", "init", "-q", "-b", "main", cwd=main_dir)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main_dir)
        sh("git", "remote", "add", "origin", "https://github.com/example-org/example-repo.git", cwd=main_dir)
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=main_dir)
        gantry_kit.register(gantry_kit.normalize_origin("https://github.com/example-org/example-repo.git"), main_dir)
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_worker"

        def fetch(url=None):
            if url:
                sh("git", "remote", "set-url", "origin", url, cwd=main_dir)
                key = gantry_kit.normalize_origin(url)
                if key:
                    gantry_kit.register(key, main_dir)
            return guard.decide({"cwd": main_dir, "permission_mode": "bypassPermissions", "tool_name": "Bash",
                                 "tool_input": {"command": "git fetch origin"}})

        allowed, reason = fetch()
        checks.append(("https org origin allowed", allowed, reason))
        allowed, reason = fetch("git@github.com:octocat/hello.git")
        checks.append(("ssh user origin allowed", allowed, reason))
        allowed, reason = fetch("https://ghe.example.com/acme/widgets.git")
        checks.append(("enterprise https origin allowed", allowed, reason))
        allowed, reason = fetch("ssh://git@ghe.example.com/acme/widgets.git")
        checks.append(("enterprise ssh origin allowed", allowed, reason))
        allowed, reason = guard.decide({"cwd": main_dir, "permission_mode": "bypassPermissions", "tool_name": "Bash",
                                        "tool_input": {"command": "gh pr list --hostname ghe.example.com"}})
        checks.append(("enterprise hostname allowed", allowed, reason))
        allowed, reason = guard.decide({"cwd": main_dir, "permission_mode": "bypassPermissions", "tool_name": "Bash",
                                        "tool_input": {"command": "gh pr list --hostname github.com"}})
        checks.append(("other hostname denied", not allowed and "gh --hostname must be ghe.example.com" in reason, reason))
        sh("git", "remote", "set-url", "origin", "/tmp/not-a-remote", cwd=main_dir)
        allowed, reason = fetch()
        checks.append(("unparseable origin is dormant", allowed, reason))
        sh("git", "remote", "remove", "origin", cwd=main_dir)
        allowed, reason = fetch()
        checks.append(("missing origin is dormant", allowed, reason))
        sh("git", "remote", "add", "origin", "https://github.com/example-org/example-repo.git", cwd=main_dir)
        gantry_kit.register(gantry_kit.normalize_origin("https://github.com/example-org/example-repo.git"), main_dir)
        with open(config_path(main_dir), "w") as f:
            json.dump({"autoMerge": False}, f)
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_ship"
        allowed, reason = guard.decide({"cwd": main_dir, "permission_mode": "bypassPermissions", "tool_name": "Bash",
                                        "tool_input": {"command": "gh pr merge 3 --squash --match-head-commit abc"}})
        checks.append(("any origin still honors autoMerge false", not allowed and "autoMerge is false" in reason, reason))
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
    return checks


def installed_hook_checks(tmp, wt, bindir):
    main = os.path.join(tmp, "main")
    nested = os.path.join(wt, "nested-repo")
    os.makedirs(nested, exist_ok=True)
    sh("git", "init", "-q", cwd=nested)
    env = {k: v for k, v in os.environ.items() if k not in ("ORCA_TERMINAL_HANDLE", "GANTRY_COORDINATOR_HANDLE")}
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["ORCA_TERMINAL_HANDLE"] = "term_worker"
    env["CLAUDE_PROJECT_DIR"] = main

    def run(cmd, cwd):
        payload = json.dumps({"cwd": cwd, "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse",
                              "tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": "toolu_test"})
        proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True, env=env)
        return proc.returncode, proc.stderr.strip()

    set_auto(main, None, ["example-work-account"])
    nested_code, nested_reason = run("echo example-work-account", nested)
    set_auto(main, None, None)
    parent_code, parent_reason = run("echo parent-marker", nested)
    return [
        ("nested repo still applies the main checkout list", nested_code == 2 and "forbidden" in nested_reason, f"exit {nested_code} {nested_reason}"),
        ("installed hook ignores a parent repo list", parent_code == 0, f"exit {parent_code} {parent_reason}"),
    ]


def plant_parent(tmp):
    sh("git", "init", "-q", "-b", "main", cwd=tmp)
    path = os.path.join(tmp, ".git", "gantry.json")
    with open(path, "w") as f:
        json.dump({"autoMerge": False, "forbidden": ["parent-marker"]}, f)


def main():
    guard = load_guard()
    failures, total = 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        os.environ["HOME"] = home
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        plant_parent(tmp)
        main_dir, wt = make_fixture(tmp)
        gantry_kit.register(gantry_kit.normalize_origin(f"https://github.com/{OWN}.git"), main_dir)
        for w in WORKERS:
            if w["agentTerminalHandle"] == "term_busy":
                w["worktreePath"] = os.path.join(tmp, "wt-live")
            if w["agentTerminalHandle"] == "term_done":
                w["worktreePath"] = os.path.join(tmp, "wt-done")
        guard.Repo.on_origin = lambda self, branch: branch in ON_ORIGIN
        for role, (tool, tool_input), expect, opts in ROWS:
            if "raw" in opts:
                raw_path = config_path(main_dir)
                with open(raw_path, "w") as f:
                    f.write(opts["raw"])
            else:
                set_auto(main_dir, opts.get("auto"), opts.get("forbidden"))
            tool_input = {k: v.replace(T, tmp) if isinstance(v, str) else v for k, v in tool_input.items()}
            cwd = opts.get("cwd")
            cwd = cwd.replace(T, tmp) if isinstance(cwd, str) else (main_dir if opts.get("on") == "main" else wt)
            payload = {"cwd": cwd, "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse",
                       "session_id": "sess_test", "tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_test"}
            if "agent_type" in opts:
                payload["agent_type"] = opts["agent_type"]
            elif role == C and opts.get("mark") != "own":
                payload["agent_type"] = "gantry-coordinator"
            handle = opts.get("as", {W: "term_worker", O: "term_main", C: "term_main"}[role])
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            os.environ.pop("GANTRY_COORDINATOR_HANDLE", None)
            if handle:
                os.environ["ORCA_TERMINAL_HANDLE"] = handle
            if opts.get("mark") == "own" and handle:
                os.environ["GANTRY_COORDINATOR_HANDLE"] = handle
            elif opts.get("mark") == "leaked":
                os.environ["GANTRY_COORDINATOR_HANDLE"] = "term_main"
            saved_launch = guard.ancestor_launched_coordinator
            if "ancestor_launched" in opts:
                guard.ancestor_launched_coordinator = lambda value=opts["ancestor_launched"]: value
            guard.merged_on_github = lambda slug, branch, merged=opts.get("merged", MERGED): slug == OWN and branch in merged
            guard.orca = orca_down if opts.get("orca") == "down" else fake_orca
            scratch = os.path.join(cwd, "dirty-scratch.txt") if opts.get("dirty") else None
            if scratch:
                open(scratch, "w").close()
            try:
                allowed, reason = guard.decide(payload)
            finally:
                if scratch and os.path.exists(scratch):
                    os.remove(scratch)
                if "ancestor_launched" in opts:
                    guard.ancestor_launched_coordinator = saved_launch
            ok = allowed == expect and "failed closed" not in reason and (not opts.get("reason") or opts["reason"] in reason)
            failures += not ok
            total += 1
            label = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            print(f"{'PASS' if ok else 'FAIL'} [{role} {tool} as {handle}] {label!r} -> {'allow' if allowed else 'deny'}"
                  + ("" if ok else f" (expected {'allow' if expect else 'deny'}) {reason}"))
        for name, ok, detail in hook_checks(tmp, wt):
            failures += not ok
            total += 1
            print(f"{'PASS' if ok else 'FAIL'} [hook] {name}" + ("" if ok else f" ({detail})"))
        for name, ok, detail in launch_checks():
            failures += not ok
            total += 1
            print(f"{'PASS' if ok else 'FAIL'} [launch] {name}" + ("" if ok else f" ({detail})"))
        for name, ok, detail in owner_checks():
            failures += not ok
            total += 1
            print(f"{'PASS' if ok else 'FAIL'} [owner] {name}" + ("" if ok else f" ({detail})"))
    print(f"{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
