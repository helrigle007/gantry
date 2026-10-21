#!/usr/bin/env python3
import sys
sys.dont_write_bytecode = True

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

BASH = "/usr/bin/bash"
BASH_PREFIX = "GNU bash, version 5.2.21"
PYTHON_VERSIONS = ("3.12.3", "3.11")
SYNC_COMMIT = "563cbc261f34c4fa645d3ede28ff21ce250b1498"
SYNC_BLOB = "81f5cabc47a2520dbb6f1b56669481f5a103391b"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GUARD_PATH = os.path.join(ROOT, "template", "kit", "gantry-guard.py")
KIT_PATH = os.path.join(ROOT, "template", "kit", "gantry_kit.py")
FIXTURE_SLUG = "example-user/example-fixture"
ORIGIN_KEY = "https://github.com/example-user/example-fixture"

COORDINATOR_AGENT = "gantry-coordinator"
SHIP_TASK = "gantry-ship"
CLEANUP_TASK = "gantry-cleanup"
CONFIG_PATH = ".git/gantry.json"
MARK_ENV = "GANTRY_COORDINATOR_HANDLE"
COORD_TOOLS = (
    "Bash", "Read", "Skill", "ToolSearch", "AskUserQuestion", "TodoWrite",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Glob", "Grep",
)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SUBAGENT_TOOLS = ("Agent", "Task", "Workflow", "Monitor")

MERGED = {"done", "dirty"}
ON_ORIGIN = {"main", "feat", "done", "dirty", "open"}

PROPS = {
    "coordinator-no-edit": (
        "The coordinator is denied Edit, Write, MultiEdit, and NotebookEdit.",
        "The coordinator would change the repo itself instead of dispatching a worker.",
    ),
    "coordinator-no-subagent": (
        "A session whose agent_type is the coordinator agent name is denied Agent, Task, Workflow, and Monitor.",
        "A subagent runs tools the coordinator is not allowed to run.",
    ),
    "coordinator-identity": (
        "The coordinator is a payload whose agent_type is the coordinator agent name, or a session whose GANTRY_COORDINATOR_HANDLE equals its own terminal, or a session whose ancestor argv carries --agent for that agent.",
        "A coordinator session would be treated as the operator and could spawn subagents or push.",
    ),
    "coordinator-no-push": (
        "The coordinator is denied every checked command for which bash executes git push.",
        "The coordinator would publish a branch instead of leaving pushes to a worker.",
    ),
    "coordinator-no-merge": (
        "The coordinator is denied every checked command for which bash executes gh pr merge.",
        "The coordinator would merge a PR instead of the gantry-ship worker.",
    ),
    "coordinator-no-write": (
        "The coordinator is denied bash that creates a file, a heredoc, git commit, git reset, or rm.",
        "The coordinator would write the worktree from a shell redirect, a commit, or a reset.",
    ),
    "coordinator-fetch-deny": (
        "The coordinator is denied fetch, pull, and merge forms outside a fast-forward of origin's default branch on a clean main checkout.",
        "The coordinator would prune, force-update, rebase, or fast-forward a dirty checkout.",
    ),
    "coordinator-fetch-allow": (
        "The coordinator may fetch origin's default branch, and may fast-forward a clean main checkout that is on that branch.",
        "Workers would be branched from a stale local default.",
    ),
    "worker-push-argv": (
        "A worker git push argv cannot target the default branch, force-push, or use --mirror, --all, or --no-verify.",
        "A task branch would move main, or a rewritten history would be forced onto origin.",
    ),
    "ship-merge-argv": (
        "gh pr merge is allowed only for the gantry-ship worker, with --match-head-commit, without --admin or --auto, and only while autoMerge is true.",
        "An implementation worker would merge, or a ship worker would merge a different commit than the one that was verified.",
    ),
    "worker-orca-argv": (
        "A worker orca argv cannot run-create, worker-start, task-create, task-update, or reset.",
        "A worker would create another Orca run or worker instead of using Claude Code subagents.",
    ),
    "cleanup-argv": (
        "git worktree remove and orca worktree rm refuse the main checkout, a dirty tree, an unmerged branch, a live worker path, --force, and the caller's own cwd. orca terminal close refuses a running worker and the caller's own terminal.",
        "Cleanup would delete the primary checkout, uncommitted work, or a branch that is still open.",
    ),
    "terminal-send": (
        "orca terminal send is allowed only for the coordinator, and only with --terminal set to a worker handle of this run.",
        "The coordinator would type into its own terminal, the operator's terminal, or another run.",
    ),
    "shell-chain": (
        "Bash chains, quotes, command substitution, backticks, bash -c, and the checked wrappers cannot hide a forbidden git, gh, or orca argv.",
        "A worker would push to main, merge, or start an Orca worker through a wrapper the segment splitter missed.",
    ),
    "shell-control": (
        "if, then, while, until, for, case, select, function definitions, subshells, and brace groups cannot hide a forbidden git, gh, orca, or gantry argv. A compound the guard cannot parse is denied.",
        "A worker would push to main, merge without the ship rules, start an Orca worker, or remove a protected worktree from inside a compound command.",
    ),
    "shell-expand": (
        "A command name or a governed subcommand position that contains $, a backtick, ${, $(, or eval is denied, and eval or exec with a dynamic argument is denied.",
        "A worker would push to main, force-push, or pass --admin to gh pr merge through a variable the guard sees as a literal.",
    ),
    "shell-file": (
        "source and . are denied for the coordinator and the worker. bash, sh, and zsh running a file are denied for the coordinator. A worker may run bash, sh, or zsh on a file; the pre-push hook is the backstop that still refuses a push to the default branch.",
        "A worker would source a file the guard never reads, or the coordinator would run a shell script.",
    ),
    "shell-benign": (
        "A worker may run git status, a non-default push, and a commit through the chain and control forms. The ship worker may run the guarded merge through those forms.",
        "A fix that denies every compound command would also deny ordinary worker git.",
    ),
    "activation-dormant": (
        "A valid registry that does not list the origin allows every tool call.",
        "An unregistered repo would still be policed.",
    ),
    "activation-registered": (
        "A registry entry for the origin applies the role rules.",
        "A registered repo would run with no role restrictions.",
    ),
    "activation-closed": (
        "When the origin is registered and the role cannot be read, Agent and Task are denied and every other tool is checked as a worker.",
        "An unreadable session would spawn a subagent or push to the default branch.",
    ),
    "activation-fallback": (
        "A missing or unreadable registry governs the repo when .git/gantry.json exists, and leaves it dormant when that file is absent.",
        "A corrupt registry would police a repo with no config, or would ignore a repo that has one.",
    ),
    "worker-gantry": (
        "A worker cannot run gantry or gantry_kit.py. The coordinator may run gantry overlap and nothing else under gantry.",
        "A worker would uninstall the kit or record an overlap verdict.",
    ),
    "pre-pr": (
        "gh pr create, gh pr new, and a POST to repos/<owner>/<repo>/pulls are denied until the worker's pre-pr overlap check is cleared, and the coordinator cannot open a PR.",
        "A worker would open a PR before the overlap gate, or the coordinator would open one.",
    ),
}


@dataclass
class Failure:
    prop: str
    where: str
    command: str
    detail: str
    shape: str = ""


@dataclass
class Case:
    prop: str
    role: str
    command: str
    shape: str
    expect_allow: bool = False
    files: tuple = ()
    on: str = "wt"
    backstop: bool = False


class _Unset:
    pass


UNSET = _Unset()

CHAIN = {
    "bare": "{c}",
    "and": "true && {c}",
    "seq": "{c}; true",
    "or": "false || {c}",
    "pipe": "{c} | true",
    "dollar": "echo $({c})",
    "double": 'echo "$({c})"',
    "backtick": "echo `{c}`",
    "double-backtick": 'echo "`{c}`"',
    "bash-c": "bash -c '{c}'",
    "bash-c-double": 'bash -c "{c}"',
    "env": "env {c}",
    "command": "command {c}",
    "timeout": "timeout 5 {c}",
    "nice": "nice {c}",
    "paren": "({c})",
    "cd": "cd /tmp && {c}",
    "newline": "true\n{c}",
    "redir-devnull": "{c} >/dev/null",
    "redir-prefix": ">/dev/null {c}",
    "nested-dollar": "echo $(echo $({c}))",
    "and-paren": "true && ({c})",
}

CONTROL = {
    "if": "if true; then {c}; fi",
    "brace": "{ {c}; }",
    "bang": "! {c}",
    "for": "for x in 1; do {c}; done",
    "while": "while true; do {c}; break; done",
    "until": "until false; do {c}; break; done",
    "case": "case a in a) {c};; esac",
    "select": "select x in a; do {c}; break; done </dev/null",
    "fn": "f() { {c}; }; f",
    "function": "function f { {c}; }; f",
    "bash-c-if": "bash -c 'if true; then {c}; fi'",
}

def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


def tool_set(names):
    return "{" + ", ".join(f'"{name}"' for name in names) + "}"


COORD_TOOLS_LINE = "COORD_TOOLS = " + tool_set(COORD_TOOLS)

MUTATIONS = {
    "coord-edit": (
        ["coordinator-no-edit"],
        [(COORD_TOOLS_LINE, "COORD_TOOLS = " + tool_set(COORD_TOOLS + EDIT_TOOLS))],
    ),
    "coord-subagent": (
        ["coordinator-no-subagent"],
        [
            (COORD_TOOLS_LINE, "COORD_TOOLS = " + tool_set(COORD_TOOLS + SUBAGENT_TOOLS)),
            (
                "        if role != \"worker\" and tool in SUBAGENT_TOOLS:\n            deny(SUBAGENT_MSG)\n",
                "        if False and role != \"worker\" and tool in SUBAGENT_TOOLS:\n            deny(SUBAGENT_MSG)\n",
            ),
        ],
    ),
    "coord-push": (
        ["coordinator-no-push"],
        [(
            '"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "merge-base", "cat-file", "describe", "shortlog", "blame", "grep"',
            '"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "merge-base", "cat-file", "describe", "shortlog", "blame", "grep", "push"',
        )],
    ),
    "coord-merge": (
        ["coordinator-no-merge"],
        [
            (
                'COORD_GH = {("pr", "view"), ("pr", "checks"), ("pr", "list"), ("auth", "status")}',
                'COORD_GH = {("pr", "view"), ("pr", "checks"), ("pr", "list"), ("auth", "status"), ("pr", "merge")}',
            ),
            (
                '    if pos[1:2] != ["merge"]:\n        return\n    need(ctx.role == "worker",',
                '    if pos[1:2] != ["merge"]:\n        return\n    return\n    need(ctx.role == "worker",',
            ),
        ],
    ),
    "coord-redirect": (
        ["coordinator-no-write"],
        [(
            'return "<" not in op and (target == "/dev/null" or op.endswith(">&") and target.isdigit())',
            "return True",
        )],
    ),
    "fetch-any": (
        ["coordinator-fetch-deny"],
        [(
            '"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "merge-base", "cat-file", "describe", "shortlog", "blame", "grep"',
            '"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "merge-base", "cat-file", "describe", "shortlog", "blame", "grep", "fetch", "pull", "merge", "rebase", "reset"',
        )],
    ),
    "push-default": (
        ["worker-push-argv"],
        [(
            'need(branch_name(repo, dst or src) != repo.default, f"never push to {repo.default}; push a task branch and open a PR")',
            'need(True, "push default")',
        )],
    ),
    "ship-title": (
        ["ship-merge-argv"],
        [(
            "need(ctx.task_title == SHIP_TASK_TITLE,",
            "need(True,",
        )],
    ),
    "worker-orca": (
        ["worker-orca-argv"],
        [(
            'only("send", "ask", "check")',
            "ANY",
        )],
    ),
    "cleanup-porcelain": (
        ["cleanup-argv"],
        [(
            'need(wt.git("status", "--porcelain") == "", f"{path} has uncommitted changes or is not a worktree; keep it")',
            'need(True, "porcelain")',
        )],
    ),
    "terminal-send": (
        ["terminal-send"],
        [(
            'def orca_terminal_send(ctx, opts):\n    handle = opts.get("--terminal")',
            'def orca_terminal_send(ctx, opts):\n    return\n    handle = opts.get("--terminal")',
        )],
    ),
    "drop-subst": (
        ["shell-chain"],
        [(
            "        segs += split_segments(body, depth + 1)\n",
            "        pass\n",
        )],
    ),
    "dormancy": (
        ["activation-dormant"],
        [(
            "    governed = kit.governing(project, cwd, warn=True)\n    if governed is None:\n        return True, \"\"\n",
            "    governed = kit.governing(project, cwd, warn=True)\n    if False and governed is None:\n        return True, \"\"\n",
        )],
    ),
    "fail-closed": (
        ["activation-closed"],
        [(
            "        if tool in SUBAGENT_TOOLS:\n            return False, SUBAGENT_MSG\n",
            "        if False and tool in SUBAGENT_TOOLS:\n            return False, SUBAGENT_MSG\n",
        )],
    ),
    "fallback": (
        ["activation-fallback"],
        [(
            "        return fallback_governed(common, key)\n",
            "        return None\n",
        )],
    ),
    "worker-gantry": (
        ["worker-gantry"],
        [(
            "def check_gantry(ctx, args):\n    need(ctx.role == \"coordinator\" and args[:1] == [\"overlap\"], \"only the coordinator runs gantry, and only gantry overlap\")\n",
            "def check_gantry(ctx, args):\n    return\n",
        )],
    ),
    "drop-compound": (
        ["shell-control"],
        [(
            "        pieces = expand_compound(argv)\n",
            "        pieces = [list(argv)]\n",
        )],
    ),
    "keep-expand": (
        ["shell-expand"],
        [(
            "def check_resolved(name, args):\n    if unresolved(name):\n",
            "def check_resolved(name, args):\n    return\n    if unresolved(name):\n",
        )],
    ),
    "allow-source": (
        ["shell-file"],
        [(
            "def deny_source(_ctx, _args):\n    deny(\"source and . run a file the guard does not read; run the commands directly\")\n",
            "def deny_source(_ctx, _args):\n    return\n",
        )],
    ),
    "deny-compound": (
        ["shell-benign"],
        [(
            "    SEGMENT_RULES.get(name, lambda ctx, args: None)(ctx, args)\n",
            "    SEGMENT_RULES.get(name, lambda ctx, args: deny(\"compound\"))(ctx, args)\n",
        )],
    ),
}


def python_ok(py):
    if py == "3.12.3":
        return True
    return py.startswith("3.11.")


def versions():
    bash = subprocess.check_output([BASH, "--version"], text=True).splitlines()[0]
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if not bash.startswith(BASH_PREFIX):
        die(f"bash oracle must be {BASH_PREFIX}, found {bash}")
    if not python_ok(py):
        die(f"python must be 3.12.3 or 3.11.x, found {py}")
    return bash, py


def git(*args, cwd):
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def make_fixture():
    root = tempfile.mkdtemp(prefix="vgfx-", dir="/tmp")
    if "gantry-guard" in root or any(ch.isspace() for ch in root):
        die(f"fixture path is not isolated from the guard's protected-token match: {root}")
    main = os.path.join(root, "main")
    wt = os.path.join(root, "wt")
    os.makedirs(main)
    git("init", "-q", "-b", "main", cwd=main)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    git("remote", "add", "origin", f"https://github.com/{FIXTURE_SLUG}.git", cwd=main)
    git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=main)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=main)
    git("config", "branch.main.remote", "origin", cwd=main)
    git("config", "branch.main.merge", "refs/heads/main", cwd=main)
    git("worktree", "add", "-q", "-b", "feat", wt, cwd=main)
    names = ("done", "dirty", "gone", "open", "live", "ahead")
    for branch in names:
        git("worktree", "add", "-q", "-b", branch, os.path.join(root, f"wt-{branch}"), cwd=main)
    open(os.path.join(root, "wt-dirty", "scratch.txt"), "w").close()
    ahead = os.path.join(root, "wt-ahead")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "ahead", cwd=ahead)
    paths = {name: os.path.realpath(os.path.join(root, f"wt-{name}")) for name in names}
    paths["main"] = os.path.realpath(main)
    paths["wt"] = os.path.realpath(wt)
    paths["home"] = os.path.join(root, "home")
    paths["tmp"] = os.path.join(root, "tmp")
    os.makedirs(paths["home"])
    os.makedirs(paths["tmp"])
    write_repo_config(paths["main"], "registered", True)
    write_registry(paths["home"], "registered")
    return root, paths


def auto_text(auto):
    return json.dumps({"autoMerge": auto}) + "\n"


def config_file(main):
    return os.path.join(main, CONFIG_PATH)


def write_repo_config(main, mode, auto):
    path = config_file(main)
    if mode in ("unreadable-bare", "missing-bare"):
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = auto_text(auto)
    if not os.path.exists(path) or open(path).read() != text:
        with open(path, "w") as fh:
            fh.write(text)


def write_registry(home, mode):
    folder = os.path.join(home, ".gantry")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "registry.json")
    if mode in ("missing-config", "missing-bare"):
        if os.path.exists(path):
            os.remove(path)
        return
    if mode in ("unreadable-config", "unreadable-bare"):
        with open(path, "w") as fh:
            fh.write("{not-json")
        return
    repos = {}
    if mode == "registered":
        repos[ORIGIN_KEY] = {"checkouts": []}
    with open(path, "w") as fh:
        json.dump({"version": 1, "repos": repos}, fh)
        fh.write("\n")


def write_clear(tmp):
    folder = os.path.join(tmp, "gantry-mailbox", "run_1")
    os.makedirs(folder, exist_ok=True)
    event = {"task": "task_impl", "checkpoint": "pre-pr", "kind": "classified", "verdict": "continue"}
    with open(os.path.join(folder, "overlap.jsonl"), "w") as fh:
        fh.write(json.dumps(event) + "\n")


def clear_overlap(tmp):
    path = os.path.join(tmp, "gantry-mailbox", "run_1", "overlap.jsonl")
    if os.path.exists(path):
        os.remove(path)


def load_guard(path):
    # The hook imports gantry_kit from its own directory. Drop a cached kit so a
    # mutated sibling is the one that loads.
    sys.modules.pop("gantry_kit", None)
    name = f"gantry_guard_{abs(hash(path)) % 10**8}_{os.getpid()}_{id(path)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.Repo.on_origin = lambda self, branch: branch in ON_ORIGIN
    return mod


def install_world(guard, paths):
    workers = [
        {"agentTerminalHandle": "term_worker", "taskId": "task_impl", "runId": "run_1", "terminalState": "active", "dispatchStatus": "running"},
        {"agentTerminalHandle": "term_ship", "taskId": "task_ship", "runId": "run_1", "terminalState": "active", "dispatchStatus": "running"},
        {"agentTerminalHandle": "term_clean", "taskId": "task_clean", "runId": "run_1", "terminalState": "retained", "dispatchStatus": "running"},
        {"agentTerminalHandle": "term_done", "taskId": "task_done", "runId": "run_1", "terminalState": "retained", "dispatchStatus": "completed", "worktreePath": paths["done"]},
        {"agentTerminalHandle": "term_busy", "taskId": "task_busy", "runId": "run_1", "terminalState": "active", "dispatchStatus": "running", "worktreePath": paths["live"]},
        {"agentTerminalHandle": "term_far", "taskId": "task_far", "runId": "run_2", "terminalState": "retained", "dispatchStatus": "completed"},
    ]
    tasks = {
        "run_1": [
            {"id": "task_impl", "task_title": "A: impl"},
            {"id": "task_ship", "task_title": SHIP_TASK},
            {"id": "task_clean", "task_title": CLEANUP_TASK},
            {"id": "task_done", "task_title": "C: impl"},
            {"id": "task_busy", "task_title": "B: impl"},
        ],
        "run_2": [{"id": "task_far", "task_title": "D: impl"}],
    }

    def fake_orca(*args):
        args = list(args)
        if args[:1] == ["run-current"]:
            return {"run": {"id": "run_1"}}
        run = args[args.index("--run") + 1] if "--run" in args else None
        if args[:1] == ["task-list"]:
            return {"tasks": tasks.get(run, [])}
        rows = [row for row in workers if run in (None, row["runId"])]
        return {"workers": rows, "page": {}}

    def orca_down(*args):
        raise RuntimeError("orca orchestration exited 1")

    guard.orca = fake_orca
    guard.merged_on_github = lambda slug, branch: slug == FIXTURE_SLUG and branch in MERGED
    guard._orca_down = orca_down
    return guard


def hook_decide(guard, payload):
    try:
        return guard.decide(payload)
    except Exception as exc:
        return False, f"guard failed closed ({type(exc).__name__}: {exc})"


HANDLES = {
    "coord": "term_main",
    "worker": "term_worker",
    "ship": "term_ship",
    "cleanup": "term_clean",
    "operator": "term_main",
}


def decide(guard, paths, role, tool, tool_input, on="wt", auto=True, agent_type=None, mark=None, orca="up", ancestor=UNSET, handle=None, mode="registered", cleared=False):
    saved = {key: os.environ.get(key) for key in ("ORCA_TERMINAL_HANDLE", MARK_ENV, "HOME", "CLAUDE_PROJECT_DIR", "TMPDIR")}
    previous = guard.orca
    patched = UNSET
    try:
        os.environ["HOME"] = paths["home"]
        os.environ["TMPDIR"] = paths["tmp"]
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        write_registry(paths["home"], mode)
        write_repo_config(paths["main"], mode, auto)
        if cleared:
            write_clear(paths["tmp"])
        else:
            clear_overlap(paths["tmp"])
        if on == "main-dirty":
            open(os.path.join(paths["main"], "dirty-scratch.txt"), "w").close()
            cwd = paths["main"]
        elif on == "main":
            cwd = paths["main"]
        elif on in paths:
            cwd = paths[on]
        else:
            cwd = paths["wt"]
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        os.environ.pop(MARK_ENV, None)
        use_handle = HANDLES[role] if handle is None else handle
        if use_handle:
            os.environ["ORCA_TERMINAL_HANDLE"] = use_handle
        if mark == "own" and use_handle:
            os.environ[MARK_ENV] = use_handle
        elif mark == "leaked":
            os.environ[MARK_ENV] = "term_main"
        elif mark == "literal":
            os.environ[MARK_ENV] = "1"
        if orca == "down":
            guard.orca = guard._orca_down
        if hasattr(guard, "ancestor_launched_coordinator"):
            patched = guard.ancestor_launched_coordinator
            value = False if ancestor is UNSET else ancestor
            guard.ancestor_launched_coordinator = lambda value=value: value
        payload = {
            "cwd": cwd,
            "permission_mode": "bypassPermissions",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
        }
        if agent_type is not None:
            payload["agent_type"] = agent_type
        elif role == "coord" and mark != "own":
            payload["agent_type"] = COORDINATOR_AGENT
        allowed, reason = hook_decide(guard, payload)
        return allowed, reason
    finally:
        if patched is not UNSET:
            guard.ancestor_launched_coordinator = patched
        guard.orca = previous
        scratch = os.path.join(paths["main"], "dirty-scratch.txt")
        if os.path.exists(scratch):
            os.remove(scratch)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_stubs(stub_dir):
    path = os.path.join(stub_dir, "record.py")
    with open(path, "w") as fh:
        fh.write(
            "#!/usr/bin/python3\n"
            "import json, os, sys\n"
            "with open(os.environ['GANTRY_ORACLE_LOG'], 'a') as fh:\n"
            "    fh.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n"
        )
    os.chmod(path, 0o755)
    for name in ("git", "gh", "orca"):
        dest = os.path.join(stub_dir, name)
        os.symlink(path, dest)


def oracle(cmd, stub_dir, files=()):
    work = tempfile.mkdtemp(prefix="oracle-")
    log = os.path.join(work, "log")
    for name, body in files:
        dest = os.path.join(work, name)
        folder = os.path.dirname(dest)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(dest, "w") as fh:
            fh.write(body)
        os.chmod(dest, 0o755)
    env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": work,
        "TERM": "dumb",
        "GANTRY_ORACLE_LOG": log,
        "LC_ALL": "C",
    }
    try:
        subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", cmd],
            cwd=work,
            env=env,
            timeout=4,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"oracle timeout: {cmd}") from exc
    argvs = []
    if os.path.exists(log):
        with open(log) as fh:
            argvs = [json.loads(line) for line in fh if line.strip()]
    created = [name for name in os.listdir(work) if name != "log"]
    shutil.rmtree(work, ignore_errors=True)
    return argvs, created


def sanity(stub_dir):
    checks = [
        ("git push origin main", True),
        ("echo '$(git push origin main)'", False),
        ("false || git push origin main", True),
        ("true || git push origin main", False),
        ("if true; then git push origin main; fi", True),
        ("x=push; git $x origin main", True),
        ("echo hi >/dev/null", False),
    ]
    for cmd, ran in checks:
        argvs, created = oracle(cmd, stub_dir)
        pushed = any(item[:2] == ["git", "push"] for item in argvs)
        if pushed != ran:
            die(f"oracle sanity failed for {cmd!r}: log={argvs!r} created={created!r}")
    argvs, created = oracle("echo hi > secret.txt", stub_dir)
    if created != ["secret.txt"]:
        die(f"oracle did not see a redirect create a file: {created!r}")


def git_sub(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "-c", "--config-env") or args[i].startswith("--config-env="):
            i += 1 if args[i].startswith("--config-env=") else 2
            continue
        i += 1
    return args[i] if i < len(args) else ""


def flag_is(args, name):
    return any(arg == name or arg.startswith(name + "=") for arg in args)


def forbidden(role, argv, paths, on="wt"):
    if not argv:
        return False
    cmd, args = argv[0], argv[1:]
    if cmd == "git":
        sub = git_sub(args)
        if sub == "push":
            if role == "coord":
                return True
            rest = args[args.index("push") + 1:]
            if any(arg in {"--force", "--mirror", "--no-verify", "--all", "--prune"} or arg.startswith("--force=") for arg in rest):
                return True
            if any(arg.startswith("+") for arg in rest):
                return True
            if any(arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:] for arg in rest):
                return True
            pos = [arg for arg in rest if not arg.startswith("-")]
            specs = pos[1:] if pos else []
            if not specs:
                return on == "main"
            for spec in specs:
                dest = spec[1:] if spec.startswith(":") else spec.partition(":")[2] or spec.partition(":")[0]
                dest = dest.removeprefix("refs/heads/")
                if dest == "main":
                    return True
            return False
        if sub == "worktree" and "remove" in args:
            if "--force" in args or "-f" in args:
                return True
            return os.path.realpath(args[-1]) in {
                paths["main"], paths["dirty"], paths["ahead"], paths["open"], paths["live"],
            }
        if role == "coord" and sub in {"commit", "reset", "rebase", "add", "checkout", "switch"}:
            return True
        return False
    if cmd == "gh" and args[:2] == ["pr", "merge"]:
        if role != "ship":
            return True
        if flag_is(args, "--admin") or flag_is(args, "--auto") or "--match-head-commit" not in args:
            return True
        return False
    if cmd == "orca":
        if any(token in args for token in ("run-create", "worker-start", "task-create", "task-update", "reset")):
            return role != "coord"
        if "terminal" in args and "send" in args:
            if role != "coord":
                return True
            if "--terminal" not in args:
                return True
            index = args.index("--terminal")
            handle = args[index + 1] if index + 1 < len(args) else ""
            return handle not in {"term_worker", "term_ship", "term_clean", "term_done"}
        if "worktree" in args and "rm" in args:
            if role != "cleanup":
                return True
            if "--force" in args:
                return True
            if "--worktree" not in args:
                return True
            index = args.index("--worktree")
            sel = args[index + 1] if index + 1 < len(args) else ""
            if not sel.startswith("path:"):
                return True
            return os.path.realpath(os.path.expanduser(sel[len("path:"):])) in {
                paths["main"], paths["dirty"], paths["ahead"], paths["open"], paths["live"],
            }
    return False


def fill(template, leaf):
    return template.replace("{c}", leaf)


def shell_cases(paths):
    push = [
        "git push origin main",
        "git push origin HEAD:main",
        "git push --force origin feat",
    ]
    orca = [
        "orca orchestration run-create --spec x",
        "orca orchestration worker-start --task t",
    ]
    merge_bad = [
        "gh pr merge 3 --squash",
        "gh pr merge 3 --admin --match-head-commit abc",
        "gh pr merge 3 --squash --match-head-commit abc --admin=true",
    ]
    merge_ok = ["gh pr merge 3 --squash --match-head-commit abc"]
    send = ["orca terminal send --terminal term_far --text hi --enter"]
    send_worker = ["orca terminal send --terminal term_worker --text hi --enter"]
    q = shlex.quote
    remove = [
        f"git worktree remove {q(paths['main'])}",
        f"git worktree remove {q(paths['dirty'])}",
        f"git worktree remove {q(paths['ahead'])}",
        f"git worktree remove --force {q(paths['done'])}",
        f"orca worktree rm --worktree path:{q(paths['main'])}",
    ]
    coord_push = ["git push -u origin feat", "git push origin main"]
    coord_other = ["git commit -m x", "git reset --hard origin/main"]
    cases = []

    def add(prop, role, shape, template, leaf, on="wt", expect_allow=False):
        cases.append(Case(prop, role, fill(template, leaf), shape, expect_allow=expect_allow, on=on))

    worker_leaves = push + orca + merge_bad + merge_ok + send_worker + remove
    for shape, template in CHAIN.items():
        for leaf in worker_leaves:
            add("shell-chain", "worker", shape, template, leaf)
        for leaf in merge_bad:
            add("shell-chain", "ship", shape, template, leaf)
        for leaf in coord_push:
            add("coordinator-no-push", "coord", shape, template, leaf)
        for leaf in merge_ok + merge_bad:
            add("coordinator-no-merge", "coord", shape, template, leaf)
        for leaf in coord_other:
            add("coordinator-no-write", "coord", shape, template, leaf)
        add("coordinator-no-write", "coord", shape, template, "echo hi > secret.txt")
        for leaf in send:
            add("terminal-send", "coord", shape, template, leaf)
    for shape, template in CONTROL.items():
        for role, leaves in (
            ("worker", worker_leaves),
            ("ship", merge_bad),
            ("cleanup", remove),
            ("coord", coord_push + merge_ok + send),
        ):
            for leaf in leaves:
                add("shell-control", role, shape, template, leaf)
        add("shell-control", "worker", shape, template, "git push", on="main")
    benign = [
        ("worker", "git status"),
        ("worker", "git push -u origin feat"),
        ("worker", "git commit -m x"),
        ("ship", "gh pr merge 3 --squash --match-head-commit abc"),
    ]
    for shape, template in {**CHAIN, **CONTROL}.items():
        for role, leaf in benign:
            add("shell-benign", role, shape, template, leaf, expect_allow=True)
    expand = [
        ("worker", "cmd=git; $cmd push origin main"),
        ("worker", "cmd=git; ${cmd} push origin main"),
        ("worker", "x=push; git $x origin main"),
        ("worker", "x=main; git push origin $x"),
        ("worker", "x=main; git push origin HEAD:$x"),
        ("worker", "d=--force; git push origin feat $d"),
        ("worker", "git() { command git push origin main; }; git status"),
        ("worker", "function git { command git push origin main; }; git status"),
        ("worker", "m=merge; gh pr $m 3 --squash"),
        ("ship", "f=--admin; gh pr merge 3 --squash --match-head-commit abc $f"),
        ("worker", "f=--admin; gh pr merge 3 --squash --match-head-commit abc $f"),
        ("worker", "cmd=git; exec $cmd push origin main"),
        ("worker", "exec git push origin $x"),
        ("worker", "printf main | xargs -I{} git push origin {}"),
        ("worker", "eval 'git push origin main'"),
        ("worker", "true && eval 'git push origin main'"),
    ]
    for role, command in expand:
        prop = "shell-chain" if "eval" in command or "xargs" in command else "shell-expand"
        cases.append(Case(prop, role, command, command))
    quoted = "echo '$(git push origin main)'"
    cases.append(Case("shell-chain", "worker", quoted, "single-quote", expect_allow=True))
    cases.append(Case("shell-chain", "coord", quoted, "single-quote", expect_allow=True))
    script = (("push.sh", "git push origin main\n"),)
    nested = (("scripts/x.sh", "git push origin main\n"),)
    harmless = (("scripts/x.sh", "echo ok\n"),)
    for role in ("worker", "coord"):
        for command in ("source push.sh", ". ./push.sh", "source ./push.sh"):
            cases.append(Case("shell-file", role, command, command, files=script))
    for shell in ("bash", "sh", "zsh"):
        cases.append(Case("shell-file", "coord", f"{shell} push.sh", shell, files=script))
        cases.append(Case("shell-file", "worker", f"{shell} push.sh", shell, files=script, backstop=True))
        cases.append(Case("shell-file", "worker", f"{shell} scripts/x.sh", f"{shell}-nested", files=nested, backstop=True))
    for command, files in (
        ("npm test", ()),
        ("python3 -m pytest -q", ()),
        ("bash scripts/x.sh", harmless),
        ("git add -A && git commit -m wip && git push -u origin feat", ()),
        ("git add file.txt && git commit -m wip", ()),
        ("true && git status && git push -u origin feat", ()),
        ("if true; then npm test; fi", ()),
        ("if true; then git add -A && git commit -m wip && git push -u origin feat; fi", ()),
    ):
        cases.append(Case("shell-benign", "worker", command, command, expect_allow=True, files=files))
    return cases


def check_shell(guard, paths, stub_dir, cases):
    failures = []
    cache = {}
    for case in cases:
        key = (case.command, case.files)
        if key not in cache:
            try:
                cache[key] = oracle(case.command, stub_dir, case.files)
            except RuntimeError as exc:
                failures.append(Failure(case.prop, case.role, case.command, str(exc)))
                continue
        argvs, created = cache[key]
        created = [name for name in created if name not in {pair[0] for pair in case.files}]
        allowed, reason = decide(guard, paths, case.role, "Bash", {"command": case.command}, on=case.on)
        if "failed closed" in (reason or ""):
            failures.append(Failure(case.prop, case.role, case.command, reason, case.shape))
            continue
        if case.backstop:
            if not allowed:
                failures.append(Failure(
                    case.prop, case.role, case.command,
                    f"decide denied ({reason}); a worker shell file stays allowed and pre-push blocks the default branch",
                    case.shape,
                ))
            continue
        bad = [argv for argv in argvs if forbidden(case.role, argv, paths, case.on)]
        wrote = case.role == "coord" and created and case.prop == "coordinator-no-write"
        if bad and allowed:
            failures.append(Failure(
                case.prop, case.role, case.command,
                f"bash ran {bad!r}; decide allowed",
                case.shape,
            ))
            continue
        if case.expect_allow and not allowed:
            failures.append(Failure(
                case.prop, case.role, case.command,
                f"decide denied ({reason}); bash ran {argvs!r}",
                case.shape,
            ))
            continue
        if wrote and allowed:
            failures.append(Failure(
                case.prop, case.role, case.command,
                f"bash created {created!r}; decide allowed",
                case.shape,
            ))
    return failures


def expect(guard, paths, prop, where, role, tool, tool_input, allow, **opts):
    allowed, reason = decide(guard, paths, role, tool, tool_input, **opts)
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    command = command or f"{tool} {tool_input}"
    if "failed closed" in (reason or ""):
        return [Failure(prop, where, command, reason)]
    if allowed != allow:
        return [Failure(prop, where, command, _fmt(allowed, reason) + f"; expected {'allow' if allow else 'deny'}")]
    return []


def _fmt(allowed, reason):
    state = "allowed" if allowed else "denied"
    return f"decide {state}" + (f" ({reason})" if reason else "")


def table_checks(guard, paths):
    failures = []
    edit_tools = [(tool, {"notebook_path": "n.ipynb"} if tool == "NotebookEdit" else {"file_path": "src/a.py"}) for tool in EDIT_TOOLS]
    edit_tools.append(("Write", {"file_path": CONFIG_PATH}))
    for tool, tool_input in edit_tools:
        failures += expect(guard, paths, "coordinator-no-edit", "coord", "coord", tool, tool_input, False)
    failures += expect(guard, paths, "coordinator-no-edit", "coord", "coord", "Read", {"file_path": "README.md"}, True)
    failures += expect(guard, paths, "coordinator-no-edit", "worker", "worker", "Write", {"file_path": "src/a.py"}, True)

    for tool in SUBAGENT_TOOLS:
        tool_input = {"prompt": "x", "subagent_type": "Explore"} if tool in {"Agent", "Task"} else {"command": "git status"}
        failures += expect(guard, paths, "coordinator-no-subagent", "coord", "coord", tool, tool_input, False, agent_type=COORDINATOR_AGENT)
    failures += expect(guard, paths, "coordinator-no-subagent", "worker", "worker", "Agent", {"prompt": "write slug.py", "subagent_type": "general-purpose"}, True)
    failures += expect(guard, paths, "coordinator-no-subagent", "operator", "operator", "Agent", {"prompt": "x", "subagent_type": "Explore"}, True)

    ident = "coordinator-identity"
    spawn = {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore"}
    failures += expect(guard, paths, ident, "mark", "operator", "Agent", spawn, False, mark="own", agent_type="")
    failures += expect(guard, paths, ident, "mark-task", "operator", "Task", {**spawn, "subagent_type": "general-purpose"}, False, mark="own", agent_type="")
    failures += expect(guard, paths, ident, "mark-push", "operator", "Bash", {"command": "git push -u origin feat"}, False, mark="own", agent_type="")
    failures += expect(guard, paths, ident, "mark-write", "operator", "Write", {"file_path": "src/a.py"}, False, mark="own", agent_type="")
    failures += expect(guard, paths, ident, "leaked", "worker", "Agent", spawn, True, mark="leaked", agent_type="")
    failures += expect(guard, paths, ident, "literal-1", "operator", "Agent", spawn, True, mark="literal", agent_type="")
    failures += ancestor_cases(guard, paths)

    writes = [
        ("echo hi > secret.txt", False),
        ("echo hi >/dev/null", True),
        ("echo hi >/dev/null 2>&1", True),
        ("cat <<EOF\nhi\nEOF", False),
        ("git commit -m x", False),
        ("rm -rf x", False),
        ("true && echo hi > secret.txt", False),
    ]
    for command, allow in writes:
        failures += expect(guard, paths, "coordinator-no-write", "coord", "coord", "Bash", {"command": command}, allow)

    deny_fetch = [
        ("wt", "git fetch"),
        ("wt", "git fetch https://github.com/someone/else.git"),
        ("wt", "git fetch origin feat"),
        ("wt", "git fetch --prune origin"),
        ("main", "git fetch origin +main:main"),
        ("main", "git fetch origin main:feat"),
        ("wt", "git fetch origin main:main"),
        ("main-dirty", "git fetch origin main:main"),
        ("main", "git pull"),
        ("main", "git pull --rebase"),
        ("main", "git pull --no-ff origin main"),
        ("main", "git pull --ff-only origin feat"),
        ("wt", "git pull --ff-only"),
        ("main-dirty", "git pull --ff-only"),
        ("main", "git merge --ff-only origin/feat"),
        ("main", "git merge origin/main"),
        ("main", "git rebase origin/main"),
        ("main", "git reset --hard origin/main"),
        ("wt", "git checkout main"),
        ("wt", "git switch main"),
        ("wt", "git fetch origin main:main"),
        ("main-dirty", "git fetch origin main:main"),
        ("main-dirty", "git pull --ff-only"),
        ("wt", "git pull --ff-only"),
    ]
    for on, command in deny_fetch:
        failures += expect(guard, paths, "coordinator-fetch-deny", on, "coord", "Bash", {"command": command}, False, on=on)
    allow_fetch = [
        ("wt", "git fetch origin"),
        ("wt", "git fetch origin main"),
        ("main-dirty", "git fetch origin main"),
        ("main", "git fetch origin main:main"),
        ("wt", f"git -C {shlex.quote(paths['main'])} fetch origin main:main"),
        ("main", "git pull --ff-only"),
        ("main", "git pull --ff-only origin main"),
        ("main", "git merge --ff-only origin/main"),
    ]
    for on, command in allow_fetch:
        failures += expect(guard, paths, "coordinator-fetch-allow", on, "coord", "Bash", {"command": command}, True, on=on)

    push_deny = [
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/main",
        "git push --force origin feat",
        "git push -f origin feat",
        "git push -fu origin feat",
        "git push origin +feat",
        "git push --mirror origin",
        "git push --no-verify origin feat",
        "git push --all origin",
        f"git -C {shlex.quote(paths['wt'])} push origin main",
    ]
    for command in push_deny:
        failures += expect(guard, paths, "worker-push-argv", "worker", "worker", "Bash", {"command": command}, False)
    failures += expect(guard, paths, "worker-push-argv", "worker-main", "worker", "Bash", {"command": "git push"}, False, on="main")
    failures += expect(guard, paths, "worker-push-argv", "worker", "worker", "Bash", {"command": "git push -u origin feat"}, True)
    failures += expect(
        guard, paths, "worker-push-argv", "worker", "worker", "Bash",
        {"command": 'git push --force-with-lease=refs/heads/feat:abc origin HEAD:refs/heads/feat'},
        True,
    )

    merge = "gh pr merge 3 --squash --match-head-commit abc"
    failures += expect(guard, paths, "ship-merge-argv", "worker", "worker", "Bash", {"command": merge}, False)
    failures += expect(guard, paths, "ship-merge-argv", "cleanup", "cleanup", "Bash", {"command": merge}, False)
    failures += expect(guard, paths, "ship-merge-argv", "coord", "coord", "Bash", {"command": merge}, False)
    failures += expect(guard, paths, "ship-merge-argv", "ship", "ship", "Bash", {"command": "gh pr merge 3 --squash"}, False)
    failures += expect(guard, paths, "ship-merge-argv", "ship", "ship", "Bash", {"command": "gh pr merge 3 --admin --match-head-commit abc"}, False)
    failures += expect(guard, paths, "ship-merge-argv", "ship", "ship", "Bash", {"command": "gh pr merge 3 --auto --match-head-commit abc"}, False)
    failures += expect(guard, paths, "ship-merge-argv", "ship-off", "ship", "Bash", {"command": merge}, False, auto=False)
    failures += expect(guard, paths, "ship-merge-argv", "ship", "ship", "Bash", {"command": merge}, True)
    failures += expect(guard, paths, "ship-merge-argv", "ship", "ship", "Bash", {"command": "gh pr merge 3 -s -d --match-head-commit abc"}, True)

    orca_deny = [
        "orca orchestration run-create --spec x",
        "orca orchestration worker-start --task t",
        "orca orchestration task-create --task-title t",
        f"orca orchestration task-update --task t --task-title {SHIP_TASK}",
        "orca orchestration reset",
    ]
    for command in orca_deny:
        failures += expect(guard, paths, "worker-orca-argv", "worker", "worker", "Bash", {"command": command}, False)
    failures += expect(guard, paths, "worker-orca-argv", "worker", "worker", "Bash", {"command": "orca orchestration send --to coordinator --body done"}, True)
    failures += expect(guard, paths, "worker-orca-argv", "worker", "worker", "Bash", {"command": "orca orchestration check"}, True)
    failures += expect(guard, paths, "worker-orca-argv", "coord", "coord", "Bash", {"command": "orca orchestration worker-start --task t"}, True)

    q = shlex.quote
    main, dirty, ahead, done, gone, live = (q(paths[name]) for name in ("main", "dirty", "ahead", "done", "gone", "live"))
    for command in (
        f"git worktree remove {main}",
        f"git worktree remove {dirty}",
        f"git worktree remove {ahead}",
        f"git worktree remove {q(paths['open'])}",
        f"git worktree remove {live}",
        f"git worktree remove --force {done}",
        f"orca worktree rm --worktree path:{main}",
        f"orca worktree rm --worktree path:{dirty}",
        f"orca worktree rm --worktree path:{done} --force",
        f"orca worktree rm --worktree path:{live}",
        "orca terminal close --terminal term_busy",
        "orca terminal close --terminal term_clean",
    ):
        failures += expect(guard, paths, "cleanup-argv", "cleanup", "cleanup", "Bash", {"command": command}, False)
    failures += expect(guard, paths, "cleanup-argv", "own-cwd", "cleanup", "Bash", {"command": f"git worktree remove {done}"}, False, on="done")
    failures += expect(guard, paths, "cleanup-argv", "own-cwd-orca", "cleanup", "Bash", {"command": f"orca worktree rm --worktree path:{done}"}, False, on="done")
    failures += expect(guard, paths, "cleanup-argv", "impl", "worker", "Bash", {"command": f"orca worktree rm --worktree path:{done}"}, False)
    failures += expect(guard, paths, "cleanup-argv", "cleanup", "cleanup", "Bash", {"command": f"git worktree remove {done}"}, True)
    failures += expect(guard, paths, "cleanup-argv", "cleanup", "cleanup", "Bash", {"command": f"git worktree remove {gone}"}, True)
    failures += expect(guard, paths, "cleanup-argv", "cleanup", "cleanup", "Bash", {"command": f"orca worktree rm --worktree path:{done}"}, True)
    failures += expect(guard, paths, "cleanup-argv", "cleanup", "cleanup", "Bash", {"command": "orca terminal close --terminal term_done"}, True)

    send_deny = [
        ("coord", "orca terminal send --text hi --enter"),
        ("coord", "orca terminal send --terminal term_main --text hi --enter"),
        ("coord", "orca terminal send --terminal term_far --text hi --enter"),
        ("worker", "orca terminal send --terminal term_worker --text hi --enter"),
        ("coord", "orca terminal create"),
        ("coord", "orca terminal close --terminal term_done"),
    ]
    for role, command in send_deny:
        failures += expect(guard, paths, "terminal-send", role, role, "Bash", {"command": command}, False)
    failures += expect(guard, paths, "terminal-send", "coord", "coord", "Bash", {"command": "orca terminal send --terminal term_worker --text hi --enter"}, True)
    failures += expect(guard, paths, "terminal-send", "coord", "coord", "Bash", {"command": "orca terminal send --terminal term_done --text hi --enter"}, True)
    failures += expect(guard, paths, "coordinator-no-push", "coord-c", "coord", "Bash", {"command": f"git -C {shlex.quote(paths['wt'])} push origin feat"}, False)
    failures += activation_checks(guard, paths)
    failures += gantry_checks(guard, paths)
    failures += pre_pr_checks(guard, paths)
    return failures


def activation_checks(guard, paths):
    failures = []
    dormant = [
        ("dormant-write", "coord", "Write", {"file_path": "src/a.py"}, COORDINATOR_AGENT),
        ("dormant-agent", "coord", "Agent", {"prompt": "x", "subagent_type": "Explore"}, COORDINATOR_AGENT),
        ("dormant-push", "worker", "Bash", {"command": "git push origin main"}, ""),
        ("dormant-gantry", "worker", "Bash", {"command": "gantry uninstall"}, ""),
        ("dormant-pr", "worker", "Bash", {"command": "gh pr create --title x --body y"}, ""),
    ]
    for where, role, tool, tool_input, agent in dormant:
        failures += expect(
            guard, paths, "activation-dormant", where, role, tool, tool_input, True,
            agent_type=agent, mode="unregistered",
        )
    failures += expect(guard, paths, "activation-registered", "registered-write", "coord", "Write", {"file_path": "src/a.py"}, False)
    failures += expect(guard, paths, "activation-registered", "registered-push", "worker", "Bash", {"command": "git push origin main"}, False)
    closed = [
        ("closed-agent", "Agent", {"prompt": "x", "subagent_type": "Explore"}, False),
        ("closed-task", "Task", {"prompt": "x", "subagent_type": "general-purpose"}, False),
        ("closed-push", "Bash", {"command": "git push origin main"}, False),
        ("closed-read", "Read", {"file_path": "README.md"}, True),
    ]
    for where, tool, tool_input, allow in closed:
        failures += expect(
            guard, paths, "activation-closed", where, "operator", tool, tool_input, allow,
            handle="", agent_type="", ancestor=None,
        )
    failures += expect(
        guard, paths, "activation-fallback", "unreadable-governed", "worker", "Bash",
        {"command": "git push origin main"}, False, mode="unreadable-config",
    )
    failures += expect(
        guard, paths, "activation-fallback", "unreadable-dormant", "worker", "Bash",
        {"command": "git push origin main"}, True, mode="unreadable-bare",
    )
    failures += expect(
        guard, paths, "activation-fallback", "missing-governed", "coord", "Write",
        {"file_path": "src/a.py"}, False, mode="missing-config",
    )
    failures += expect(
        guard, paths, "activation-fallback", "missing-dormant", "coord", "Write",
        {"file_path": "src/a.py"}, True, mode="missing-bare",
    )
    return failures


def gantry_checks(guard, paths):
    prop = "worker-gantry"
    failures = []
    denied = [
        ("worker", "gantry overlap --checkpoint scoped --intent x --files a.py"),
        ("worker", "gantry uninstall"),
        ("worker", "gantry overlap --record classified --checkpoint pre-pr --run run_1 --task task_impl --verdict continue"),
        ("worker", "python3 /opt/gantry_kit.py uninstall"),
        ("coord", "gantry uninstall"),
        ("coord", "python3 /opt/gantry_kit.py uninstall"),
    ]
    for role, command in denied:
        failures += expect(guard, paths, prop, role, role, "Bash", {"command": command}, False)
    failures += expect(
        guard, paths, prop, "coord-overlap", "coord", "Bash",
        {"command": "gantry overlap --checkpoint scoped --intent x --files a.py"}, True,
    )
    return failures


def pre_pr_checks(guard, paths):
    prop = "pre-pr"
    slug = FIXTURE_SLUG
    failures = []
    denied = [
        ("worker", "gh pr create --title x --body y"),
        ("worker", "gh pr new --title x --body y"),
        ("worker", f"gh api --method POST repos/{slug}/pulls"),
        ("worker", f"gh api --method POST repos/{slug}/pulls/"),
        ("worker", f"gh api -X POST repos/{slug}/pulls"),
        ("worker", f"gh api --method POST https://api.github.com/repos/{slug}/pulls"),
        ("coord", "gh pr create --title x --body y"),
        ("coord", "gh pr new --title x --body y"),
        ("coord", f"gh api --method POST repos/{slug}/pulls"),
        ("ship", "gh pr create --title x --body y"),
    ]
    for role, command in denied:
        failures += expect(guard, paths, prop, role, role, "Bash", {"command": command}, False)
    failures += expect(
        guard, paths, prop, "cleared", "worker", "Bash",
        {"command": "gh pr create --title x --body y"}, True, cleared=True,
    )
    failures += expect(
        guard, paths, prop, "cleared-new", "worker", "Bash",
        {"command": "gh pr new --title x --body y"}, True, cleared=True,
    )
    failures += expect(
        guard, paths, prop, "cleared-api", "worker", "Bash",
        {"command": f"gh api --method POST repos/{slug}/pulls/"}, True, cleared=True,
    )
    failures += expect(
        guard, paths, prop, "cleared-url", "worker", "Bash",
        {"command": f"gh api -X POST https://api.github.com/repos/{slug}/pulls"}, True, cleared=True,
    )
    return failures


def ancestor_cases(guard, paths):
    base = os.path.dirname(paths["main"])
    parent = os.path.join(base, "parent.py")
    with open(parent, "w") as fh:
        fh.write(
            "import subprocess, sys\n"
            "payload = sys.stdin.buffer.read()\n"
            "proc = subprocess.run([sys.executable, sys.argv[1]], input=payload, capture_output=True)\n"
            "sys.stderr.buffer.write(proc.stderr)\n"
            "raise SystemExit(proc.returncode)\n"
        )
    bindir = os.path.join(base, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(bindir, "orca"), "w") as fh:
        fh.write(
            "#!/usr/bin/python3\n"
            "import json, sys\n"
            "args = [a for a in sys.argv[1:] if a != '--json']\n"
            "if args[:2] == ['orchestration', 'run-current']:\n"
            "    result = {'run': {'id': 'run_1'}}\n"
            "elif len(args) > 1 and args[1] == 'task-list':\n"
            "    result = {'tasks': []}\n"
            "else:\n"
            "    result = {'workers': [], 'page': {}}\n"
            "print(json.dumps({'ok': True, 'result': result}))\n"
        )
    os.chmod(os.path.join(bindir, "orca"), 0o755)
    hook = guard.__file__
    spawn = {"description": "Explore the repo", "prompt": "find role_of", "subagent_type": "Explore", "run_in_background": True}

    write_registry(paths["home"], "registered")
    write_repo_config(paths["main"], "registered", True)

    def launch(tool, tool_input, argv, handle=None, down=False):
        env = {key: value for key, value in os.environ.items() if key not in {"ORCA_TERMINAL_HANDLE", MARK_ENV, "CLAUDE_PROJECT_DIR"}}
        env["HOME"] = paths["home"]
        env["TMPDIR"] = paths["tmp"]
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        if handle:
            env["ORCA_TERMINAL_HANDLE"] = handle
        if down:
            env["PATH"] = "/usr/bin:/bin"
        body = {
            "cwd": paths["wt"],
            "permission_mode": "bypassPermissions",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
        }
        proc = subprocess.run(
            [sys.executable, parent, hook, *argv],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stderr.strip()

    failures = []
    specs = [
        ("argv --agent", "Agent", spawn, ["--agent", COORDINATOR_AGENT], None, False, 2),
        ("argv --agent=", "Task", {**spawn, "subagent_type": "general-purpose"}, [f"--agent={COORDINATOR_AGENT}"], None, False, 2),
        ("argv --agent orca down", "Agent", spawn, ["--agent", COORDINATOR_AGENT], "term_main", True, 2),
        ("argv --agent push", "Bash", {"command": "git push -u origin feat"}, ["--agent", COORDINATOR_AGENT], "term_main", True, 2),
        ("argv plain", "Agent", spawn, [], "term_main", False, 0),
    ]
    for name, tool, tool_input, argv, handle, down, want in specs:
        code, reason = launch(tool, tool_input, argv, handle, down)
        if "failed closed" in reason:
            failures.append(Failure(
                "coordinator-identity", name,
                f"{tool} argv={argv!r} handle={handle!r} orca_down={down}",
                reason,
            ))
            continue
        if code != want:
            failures.append(Failure(
                "coordinator-identity", name,
                f"{tool} argv={argv!r} handle={handle!r} orca_down={down}",
                f"exit {code} {reason}; expected exit {want}",
            ))
    return failures


def mutate_sources(guard_text, kit_text, pairs):
    for old, new in pairs:
        guard_count = guard_text.count(old)
        kit_count = kit_text.count(old)
        if guard_count + kit_count != 1:
            die(f"mutation anchor found {guard_count} times in the guard and {kit_count} times in the kit:\n{old}")
        if guard_count:
            guard_text = guard_text.replace(old, new, 1)
        else:
            kit_text = kit_text.replace(old, new, 1)
    return guard_text, kit_text


def stage_sources(guard_text, kit_text):
    folder = tempfile.mkdtemp(prefix="vgfx-mut-", dir="/tmp")
    if "gantry-guard" in folder or any(ch.isspace() for ch in folder):
        die(f"mutation path is not isolated: {folder}")
    with open(os.path.join(folder, "gantry-guard.py"), "w") as fh:
        fh.write(guard_text)
    with open(os.path.join(folder, "gantry_kit.py"), "w") as fh:
        fh.write(kit_text)
    return folder


def summarize(failures, full, only=None):
    names = [name for name in PROPS if not only or name in only]
    by_prop = {name: [] for name in names}
    for item in failures:
        if item.prop in by_prop:
            by_prop[item.prop].append(item)
    lines = []
    failed = 0
    for name in names:
        statement, harm = PROPS[name]
        items = by_prop.get(name, [])
        if items:
            failed += 1
            lines.append(f"FAIL {name}  ({len(items)})")
        else:
            lines.append(f"PASS {name}")
        lines.append(f"  {statement}")
        lines.append(f"  Harm: {harm}")
        shown = items if full or len(items) <= 12 else _shapes(items)
        for item in shown:
            lines.append(f"  - [{item.where}] {item.command}")
            lines.append(f"    {item.detail}")
        if items and shown is not items:
            lines.append(f"  ... {len(items) - len(shown)} more; rerun with --full")
    return failed, "\n".join(lines)


def _shapes(items):
    picked = []
    seen = set()
    for item in items:
        key = item.shape or item.command
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
    return picked


def bind_names(guard):
    pairs = (
        (getattr(guard, "COORDINATOR_AGENT", None), COORDINATOR_AGENT, "coordinator agent"),
        (getattr(guard, "SHIP_TASK_TITLE", None), SHIP_TASK, "ship task"),
        (getattr(guard, "CLEANUP_TASK_TITLE", None), CLEANUP_TASK, "cleanup task"),
    )
    for got, want, label in pairs:
        if got != want:
            die(f"{label} is {got!r} in the hook and {want!r} in the model")
    text = open(guard.__file__).read()
    if CONFIG_PATH not in text:
        die(f"config path {CONFIG_PATH} is not in the hook")


def sync_status(guard_path):
    current = subprocess.run(["git", "hash-object", guard_path], cwd=ROOT, capture_output=True, text=True)
    if current.returncode != 0:
        return False, "guard blob was not readable"
    got = current.stdout.strip()
    blob = subprocess.run(
        ["git", "rev-parse", f"{SYNC_COMMIT}:template/kit/gantry-guard.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if blob.returncode == 0 and blob.stdout.strip() != SYNC_BLOB:
        return False, f"sync commit blob {blob.stdout.strip()} does not match pinned blob {SYNC_BLOB}"
    if got == SYNC_BLOB:
        return True, f"guard matches sync commit {SYNC_COMMIT}"
    return False, f"guard differs from sync commit {SYNC_COMMIT}"


def outcome(failures, only):
    by_prop = {}
    for item in failures:
        by_prop.setdefault(item.prop, []).append(item)
    names = list(only) if only else list(PROPS)
    return [name for name in names if by_prop.get(name)]


def run(guard_path, only=None, full=False, quiet=False):
    bash, py = versions()
    root, paths = make_fixture()
    stub_parent = tempfile.mkdtemp(prefix="vgfx-stubs-", dir="/tmp")
    if "gantry-guard" in stub_parent:
        die(f"stub path is not isolated: {stub_parent}")
    try:
        make_stubs(stub_parent)
        sanity(stub_parent)
        guard = install_world(load_guard(guard_path), paths)
        bind_names(guard)
        failures = table_checks(guard, paths)
        cases = shell_cases(paths)
        if only:
            cases = [case for case in cases if case.prop in only]
        failures += check_shell(guard, paths, stub_parent, cases)
        if only:
            failures = [item for item in failures if item.prop in only]
        harness = [item for item in failures if item.detail.startswith("oracle timeout") or "failed closed" in item.detail]
        if harness:
            print("\n".join(item.detail for item in harness), file=sys.stderr)
            return 2
        failed, report = summarize(failures, full, only)
        missed = outcome(failures, only)
        canonical = os.path.realpath(guard_path) == os.path.realpath(GUARD_PATH)
        sync_ok, sync_line = sync_status(guard_path)
        if not quiet:
            counted = len(only) if only else len(PROPS)
            print(f"bash: {bash}")
            print(f"python: {py}")
            print(f"guard: {guard_path}")
            print(sync_line)
            print(f"shell cases: {len(cases)}")
            print(report)
            print(f"{counted - failed}/{counted} properties hold")
            if canonical and not sync_ok:
                print("sync check failed for the kit guard")
        code = 1 if missed else 0
        if canonical and not sync_ok:
            code = 1
        return code
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(stub_parent, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Check gantry guard promises.")
    parser.add_argument("--guard", default=GUARD_PATH)
    parser.add_argument("--mutate", choices=sorted(MUTATIONS))
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    guard_path = args.guard
    only = None
    staged = None
    if args.mutate:
        props, pairs = MUTATIONS[args.mutate]
        only = set(props)
        base = run(guard_path, only, quiet=True)
        if base == 2:
            sys.exit(2)
        if base != 0:
            die(f"mutate {args.mutate}: the unmodified guard already fails this property")
        kit_source = os.path.join(os.path.dirname(os.path.abspath(guard_path)), "gantry_kit.py")
        if not os.path.isfile(kit_source):
            kit_source = KIT_PATH
        guard_text, kit_text = mutate_sources(open(guard_path).read(), open(kit_source).read(), pairs)
        staged = stage_sources(guard_text, kit_text)
        guard_path = os.path.join(staged, "gantry-guard.py")
    try:
        code = run(guard_path, only, args.full)
    finally:
        if staged:
            shutil.rmtree(staged, ignore_errors=True)
    if args.mutate:
        if code == 2:
            sys.exit(2)
        if code == 1:
            print(f"mutate {args.mutate}: the broken guard failed the property")
            sys.exit(0)
        print(f"mutate {args.mutate}: the broken guard still passed")
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
