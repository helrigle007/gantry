#!/usr/bin/env bash
# Offline gantry sandbox: scratch HOME, local bare remote, a repo whose origin
# URL names example-user/example-fixture but whose pushurl is the bare remote.
# Install writes under the scratch HOME and commits nothing in the repo.
# Nothing here touches GitHub, Orca, or the real ~/.claude.json.
#
#   sandbox.sh up [dir]                       build sandbox, run `gantry install`, print dir
#   sandbox.sh doctor <dir>                   health check, including worktree deny,
#                                             unregistered allow, overlap, and the rename
#   sandbox.sh guard <dir> <role> <repo|wt> <Tool> '<tool_input json>'
#                                             feed one PreToolUse payload to the user-level guard
#   sandbox.sh claude <dir> <role> '<prompt>'
#                                             real `claude -p` (haiku) in the sandbox, user hooks live
#   sandbox.sh down <dir>                     remove <dir>/sbx; <dir>/evidence stays
#
# Roles: coordinator, worker, ship (a worker whose task title is gantry-ship),
# cleanup (a worker whose task title is gantry-cleanup), operator (no Orca handle).
# worker, ship, cleanup, and coordinator run with sbx/bin/orca. The fake lists
# term_sbx_worker on run_sbx and term_sbx_done, a settled user_takeover worker on
# run_other, and answers run-current with run_sbx.
set -euo pipefail

kit=$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")/../../../.." && pwd)
slug=example-user/example-fixture
die() { echo "sandbox: $*" >&2; exit 1; }
own() { [ -f "$1/.gantry-verify" ] || die "$1 is not a gantry verify dir (no .gantry-verify marker)"; }
g() { git -c user.name=verify -c user.email=verify@local "$@"; }
role_env() {
  local home=$2/sbx/home
  case $1 in
    worker|ship|cleanup)
      local title=sbx-impl
      [ "$1" = ship ] && title=gantry-ship
      [ "$1" = cleanup ] && title=gantry-cleanup
      echo "-u GANTRY_COORDINATOR_HANDLE HOME=$home PATH=$2/sbx/bin:$PATH ORCA_TERMINAL_HANDLE=term_sbx_worker SBX_TASK_TITLE=$title CLAUDE_PROJECT_DIR=$2/sbx/repo" ;;
    coordinator) echo "-u ORCA_TERMINAL_HANDLE -u GANTRY_COORDINATOR_HANDLE HOME=$home PATH=$2/sbx/bin:$PATH CLAUDE_PROJECT_DIR=$2/sbx/repo" ;;
    operator) echo "-u ORCA_TERMINAL_HANDLE -u GANTRY_COORDINATOR_HANDLE HOME=$home PATH=$2/sbx/bin:$PATH CLAUDE_PROJECT_DIR=$2/sbx/repo" ;;
    *) die "role is coordinator, worker, ship, cleanup, or operator" ;;
  esac
}

write_orca() {
  cat > "$1/bin/orca" <<'PY'
#!/usr/bin/env python3
import json, os, sys
live = {"agentTerminalHandle": "term_sbx_worker", "taskId": "task_sbx", "runId": "run_sbx", "terminalState": "retained", "dispatchStatus": "running"}
done = {"agentTerminalHandle": "term_sbx_done", "taskId": "task_sbx_done", "runId": "run_other", "terminalState": "retained", "retainedReason": "user_takeover", "dispatchStatus": "completed"}
args = sys.argv[2:]
sub = args[0] if args else ""
run = args[args.index("--run") + 1] if "--run" in args else None
if sub == "run-current":
    result = {"run": {"id": "run_sbx"}}
elif sub == "worker-list":
    rows = [w for w in (live, done) if run in (None, w["runId"])]
    result = {"workers": rows, "page": {"nextCursor": None}}
elif sub == "task-list":
    result = {"tasks": [{"id": "task_sbx", "task_title": os.environ.get("SBX_TASK_TITLE", "sbx-impl")}, {"id": "task_sbx_done", "task_title": "old"}]}
else:
    result = {}
print(json.dumps({"ok": True, "result": result}))
PY
  chmod +x "$1/bin/orca"
}

up() {
  local v=${1:-${GANTRY_VERIFY_DIR:-${TMPDIR:-/tmp}/gantry-verify/$(date +%Y%m%d-%H%M%S)}}
  [ ! -e "$v/sbx" ] || die "$v/sbx already exists; run down first or pick another dir"
  mkdir -p "$v/sbx/home/.claude/plugins/cache/x/pstack/0/skills/poteto-mode" "$v/sbx/bin" "$v/evidence"
  v=$(cd "$v" && pwd -P)
  touch "$v/.gantry-verify"
  local s=$v/sbx
  echo '{}' > "$s/home/.claude.json"
  write_orca "$s"
  git init -q --bare -b main "$s/remote.git"
  git init -q -b main "$s/repo"
  g -C "$s/repo" commit -q --allow-empty -m init
  git -C "$s/repo" remote add origin "https://github.com/$slug.git"
  git -C "$s/repo" config remote.origin.pushurl "$s/remote.git"
  git -C "$s/repo" push -q origin main
  git -C "$s/repo" update-ref refs/remotes/origin/main HEAD
  git -C "$s/repo" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
  git init -q -b main "$s/other"
  git -C "$s/other" remote add origin "https://github.com/example-org/example-other.git"
  if ! HOME=$s/home PATH=$s/bin:$PATH "$kit/bin/gantry" install "$s/repo" >"$v/evidence/install.txt" 2>&1; then
    cat "$v/evidence/install.txt" >&2
    rm -rf "$s"
    die "install failed; removed $s so the next up can retry. evidence kept in $v/evidence"
  fi
  cat "$v/evidence/install.txt"
  git -C "$s/repo" worktree add -q -b feat "$s/wt"
  echo "$v"
}

prove_worktree_deny() {
  local s=$1/sbx hook=$1/sbx/home/.gantry/current/kit/gantry-guard.py err code=0
  local payload
  payload=$(python3 - "$s/wt" <<'PY'
import json, sys
print(json.dumps({"cwd": sys.argv[1], "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "x.txt", "content": "x"}, "agent_type": "gantry-coordinator"}))
PY
)
  err=$(printf '%s' "$payload" | env -u ORCA_TERMINAL_HANDLE -u CLAUDE_PROJECT_DIR HOME=$s/home PATH=$s/bin:$PATH "$hook" 2>&1 >/dev/null) || code=$?
  [ "$code" = 2 ] && printf '%s' "$err" | grep -q "never writes code"
}

prove_unregistered_allow() {
  local s=$1/sbx hook=$1/sbx/home/.gantry/current/kit/gantry-guard.py code=0
  local payload
  payload=$(python3 - "$s/other" <<'PY'
import json, sys
print(json.dumps({"cwd": sys.argv[1], "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "x.txt", "content": "x"}, "agent_type": "gantry-coordinator"}))
PY
)
  printf '%s' "$payload" | env -u CLAUDE_PROJECT_DIR -u ORCA_TERMINAL_HANDLE HOME=$s/home PATH=$s/bin:$PATH "$hook" >/dev/null 2>&1 || code=$?
  [ "$code" = 0 ]
}

prove_overlap() {
  python3 - "$1/sbx/home/.gantry/current/kit" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import gantry_kit as k
intent = "fix the crash in app"
planned = ["src/app.py"]
rows = [
    {"files": ["src/app.py"], "title": "fix the crash in app", "body": ""},
    {"files": ["src/app.py"], "title": "rename the banner", "body": "colors"},
    {"files": ["README.md"], "title": "docs", "body": ""},
]
labels = [k.classify(intent, planned, row) for row in rows]
actions = [k.ACTIONS[label] for label in labels]
ok = labels == ["duplicate", "related", "unrelated"] and actions == ["stop", "flag", "ignore"] and k.verdict_of(labels) == "stop"
sys.exit(0 if ok else 1)
PY
}

prove_rename() {
  local root=$1/sbx/home/.gantry/current old
  [ -d "$root/kit" ] || return 1
  old=$(printf '%s%s' spud pod)
  if grep -R -n -I "$old" "$root"; then
    return 1
  fi
  return 0
}

doctor() {
  local v=$1 s=$1/sbx bad=0
  own "$v"
  check() { if eval "$2" >/dev/null 2>&1; then echo "ok   $1"; else echo "FAIL $1"; bad=1; fi; }
  check "sandbox exists" "[ -d '$s/repo' ] && [ -d '$s/wt' ] && [ -d '$s/remote.git' ] && [ -d '$s/other' ]"
  check "origin url names $slug" "[ \"\$(git -C '$s/repo' config --get remote.origin.url)\" = 'https://github.com/$slug.git' ]"
  check "pushurl is the local bare remote" "[ \"\$(git -C '$s/repo' config remote.origin.pushurl)\" = '$s/remote.git' ]"
  check "git status clean after install" "[ -z \"\$(git -C '$s/repo' status --porcelain)\" ]"
  check "worktree exists and is clean" "[ -d '$s/wt' ] && [ -z \"\$(git -C '$s/wt' status --porcelain)\" ]"
  check "no project kit was committed" "! git -C '$s/repo' ls-files | grep -q '^.claude/'"
  for f in gantry-guard.py gantry-coordinator.md gantry-ship.md gantry-mailbox gantry_run.py gantry.json pre-push gantry_kit.py; do
    check "kit $f matches template" "cmp -s '$kit/template/kit/$f' '$s/home/.gantry/current/kit/$f'"
  done
  check "version is 0.3.1" "[ \"\$(HOME='$s/home' '$s/home/.gantry/current/bin/gantry' --version)\" = 0.3.1 ]"
  check "user settings wire the guard" "grep -q gantry-guard.py '$s/home/.claude/settings.json'"
  check "user agent is the coordinator" "grep -q 'name: gantry-coordinator' '$s/home/.claude/agents/gantry-coordinator.md'"
  check "pre-push stub is gantry" "grep -q 'gantry pre-push' '$s/repo/.git/hooks/pre-push'"
  check "config lives in the git common dir" "python3 -c 'import json,sys; d=json.load(open(\"$s/repo/.git/gantry.json\")); sys.exit(0 if d.get(\"autoMerge\") is True and d.get(\"forbidden\") == [] else 1)'"
  check "scratch HOME trusts the repo" "python3 -c 'import json,sys; sys.exit(not json.load(open(\"$s/home/.claude.json\"))[\"projects\"][\"$s/repo\"][\"hasTrustDialogAccepted\"])'"
  check "origin is registered" "python3 -c 'import json,sys; d=json.load(open(\"$s/home/.gantry/registry.json\")); sys.exit(0 if \"https://github.com/$slug\" in d.get(\"repos\", {}) else 1)'"
  check "fake orca lists the sandbox worker" "'$s/bin/orca' orchestration worker-list --json | grep -q term_sbx_worker"
  check "fake orca lists a settled worker from another run" "'$s/bin/orca' orchestration worker-list --json | grep -q term_sbx_done"
  check "fake orca answers run-current" "'$s/bin/orca' orchestration run-current --json | grep -q run_sbx"
  check "guard denies a coordinator write in a fresh worktree" "prove_worktree_deny '$v'"
  check "unregistered repo allows a coordinator write" "prove_unregistered_allow '$v'"
  check "overlap classifies stop, flag, ignore" "prove_overlap '$v'"
  check "installed kit has no previous product name" "prove_rename '$v'"
  return $bad
}

guard() {
  local v=$1 role=$2 where=$3 tool=$4 input=$5
  own "$v"
  local cwd=$v/sbx/$where hook=$v/sbx/home/.gantry/current/kit/gantry-guard.py
  local payload
  payload=$(python3 - "$role" "$cwd" "$tool" "$input" <<'PY'
import json, sys
role, cwd, tool, inp = sys.argv[1:]
p = {"cwd": cwd, "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": json.loads(inp)}
if role == "coordinator":
    p["agent_type"] = "gantry-coordinator"
print(json.dumps(p))
PY
)
  local err code=0
  # shellcheck disable=SC2046
  err=$(printf '%s' "$payload" | env $(role_env "$role" "$v") "$hook" 2>&1 >/dev/null) || code=$?
  local verdict=ALLOW
  [ "$code" = 0 ] || verdict=DENY
  [ "$code" = 0 ] || [ "$code" = 2 ] || verdict="BROKEN(exit $code)"
  python3 - "$v/evidence/guard.jsonl" "$role" "$where" "$tool" "$input" "$verdict" "$err" <<'PY'
import json, sys
path, role, where, tool, inp, verdict, err = sys.argv[1:]
open(path, "a").write(json.dumps({"role": role, "cwd": where, "tool": tool, "input": json.loads(inp), "verdict": verdict, "stderr": err}) + "\n")
PY
  echo "$verdict${err:+  $err}"
}

run_claude() {
  local v=$1 role=$2 prompt=$3
  own "$v"
  local n out cwd=$v/sbx/wt args=()
  [ "$role" = coordinator ] && { cwd=$v/sbx/repo; args=(--agent gantry-coordinator --disallowedTools "Edit,Write,NotebookEdit,Agent,Task,Workflow,Monitor"); }
  n=$(ls "$v/evidence" | grep -c '^claude-' || true)
  out=$v/evidence/claude-$((n + 1))-$role.json
  # user source is required: the guard and the agent live in the scratch HOME.
  # shellcheck disable=SC2046
  (cd "$cwd" && env $(role_env "$role" "$v") claude -p "$prompt" --model haiku --setting-sources user,project,local --dangerously-skip-permissions \
     --output-format json ${args[@]+"${args[@]}"}) > "$out" || true
  python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("result:", (d.get("result") or "")[:400])
for x in d.get("permission_denials") or []:
    print("denied:", x.get("tool_name"), json.dumps(x.get("tool_input"))[:200])
print("evidence:", sys.argv[1])
PY
}

down() {
  own "$1"
  rm -rf "$1/sbx"
  echo "removed $1/sbx; evidence kept in $1/evidence"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  doctor) doctor "$2" ;;
  guard) shift; guard "$@" ;;
  claude) shift; run_claude "$@" ;;
  down) down "$2" ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
