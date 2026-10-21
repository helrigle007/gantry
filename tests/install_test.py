#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GANTRY = os.path.join(ROOT, "bin", "gantry")
HEADER = "your Claude Code hooks block commands pstack needs"
SUGGESTION = "push (--force|-f)([^-]|$)"

DANGEROUS = """#!/bin/bash
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tool_input"]["command"])')
DANGEROUS_PATTERNS=(
  "git clean -f"
  "push --force"
  "reset --hard"
)
for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qE "$pattern"; then
    echo "BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this." >&2
    exit 2
  fi
done
exit 0
"""

DENY_MERGE = """#!/usr/bin/env python3
import json, sys
cmd = json.load(sys.stdin)["tool_input"]["command"]
if "gh pr merge" in cmd:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
          "permissionDecision": "deny", "permissionDecisionReason": "merges are manual here"}}))
"""

BLOCK_ALL = "#!/bin/sh\necho 'nothing gets through' >&2\nexit 2\n"
EXIT_ONE = "#!/bin/sh\necho 'hook crashed' >&2\nexit 1\n"
ORCA_ONLY = '#!/bin/sh\n[ -n "$ORCA_AGENT_HOOK_PORT" ] && { echo "orca session hook" >&2; exit 2; }\nexit 0\n'
MARKER = '#!/bin/sh\ncat > /dev/null\necho ran >> "$HOME/probe-ran"\nexit 0\n'
ORCA_TOUCH = """#!/bin/sh
echo orca >> "$ORCA_LOG"
exit 0
"""


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def entry(matcher, command):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def status(repo):
    return subprocess.check_output(["git", "-C", repo, "status", "--porcelain"], text=True)


def registry(home):
    path = os.path.join(home, ".gantry", "registry.json")
    if not os.path.exists(path):
        return None
    return json.load(open(path))


class Fixture:
    def __init__(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(tmp, "home")
        self.repo = os.path.join(tmp, "repo")
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(os.path.join(self.home, ".claude", "plugins", "cache", "x", "pstack", "0", "skills", "poteto-mode"))
        os.makedirs(os.path.join(self.home, ".claude", "hooks"))
        with open(os.path.join(self.home, ".claude.json"), "w") as handle:
            handle.write("{}\n")
        os.makedirs(self.bin)
        self.script(os.path.join(self.bin, "orca"), "#!/bin/sh\nexit 0\n")
        os.makedirs(self.repo)
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "remote", "add", "origin", "https://github.com/example-user/example-fixture.git", cwd=self.repo)

    def script(self, path, body):
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        return path

    def hook(self, name, body):
        return self.script(os.path.join(self.home, ".claude", "hooks", name), body)

    def settings(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump(payload, handle)
        return path

    @property
    def home_settings(self):
        return os.path.join(self.home, ".claude", "settings.json")

    @property
    def local_settings(self):
        return os.path.join(self.repo, ".claude", "settings.local.json")

    def env(self, **extra):
        return {"HOME": self.home, "PATH": self.bin + os.pathsep + os.environ["PATH"], "TMPDIR": self.tmp, **extra}

    def run(self, *args, **extra):
        return subprocess.run([GANTRY, *args], env=self.env(**extra), capture_output=True, text=True)

    def install(self, **extra):
        return self.run("install", self.repo, **extra)


def dangerous_in_home(fx):
    cmd = fx.hook("block-force-push.sh", DANGEROUS)
    fx.settings(fx.home_settings, {"hooks": {"PreToolUse": [entry("Bash", cmd)]}})
    return cmd


def case_dangerous_hook(fx):
    cmd = dangerous_in_home(fx)
    result = fx.install()
    err = result.stderr
    return [
        (result.returncode == 0, "install still exits 0"),
        (HEADER in err, "warning header printed"),
        (fx.home_settings in err, "names the settings file"),
        (cmd in err, "names the hook command"),
        ("blocks lease push: git push --force-with-lease=" in err, "flags the lease push"),
        ("blocks worktree resync: git reset --hard origin/feat" in err, "flags the reset"),
        ("'push --force'" in err, "quotes the hook's pattern"),
        (SUGGESTION in err, "prints the lease suggestion"),
        ("gh pr merge" not in err, "does not flag the guarded merge"),
        (status(fx.repo) == "", "git status stays empty"),
    ]


def case_json_deny(fx):
    cmd = fx.hook("deny-merge.py", DENY_MERGE)
    fx.settings(fx.local_settings, {"hooks": {"PreToolUse": [entry("", cmd)]}})
    err = fx.install().stderr
    return [
        (fx.local_settings in err, "names settings.local.json"),
        ("blocks guarded merge: gh pr merge 1 --squash --match-head-commit 0123abc" in err, "flags the merge"),
        ("merges are manual here" in err, "quotes the JSON reason"),
        ("lease push" not in err, "does not flag commands the hook allows"),
    ]


def case_edit_matcher(fx):
    dangerous_in_home(fx)
    other = fx.hook("block-all.sh", BLOCK_ALL)
    current = json.load(open(fx.home_settings))
    current["hooks"]["PreToolUse"].append(entry("Edit", other))
    fx.settings(fx.home_settings, current)
    err = fx.install().stderr
    return [(HEADER in err, "probe ran"), (other not in err, "Edit-only hook not flagged")]


def case_exit_one(fx):
    dangerous_in_home(fx)
    other = fx.hook("crash.sh", EXIT_ONE)
    current = json.load(open(fx.home_settings))
    current["hooks"]["PreToolUse"].append(entry("Bash", other))
    fx.settings(fx.home_settings, current)
    err = fx.install().stderr
    return [(HEADER in err, "probe ran"), (other not in err, "exit-1 hook not flagged")]


def case_minimal_env(fx):
    dangerous_in_home(fx)
    other = fx.hook("orca-only.sh", ORCA_ONLY)
    current = json.load(open(fx.home_settings))
    current["hooks"]["PreToolUse"].append(entry("*", other))
    fx.settings(fx.home_settings, current)
    err = fx.install(ORCA_AGENT_HOOK_PORT="4242").stderr
    return [(HEADER in err, "probe ran"), (other not in err, "session-bound hook sees no ORCA_* vars")]


def case_clean_home(fx):
    result = fx.install()
    trust = json.load(open(os.path.join(fx.home, ".claude.json")))
    settings = open(fx.home_settings).read()
    return [
        (result.returncode == 0, "install exits 0"),
        (result.stderr == "", "stderr empty"),
        ("gantry installed in " in result.stdout, "names the checkout"),
        ("Nothing in the target repo was committed." in result.stdout, "says it committed nothing"),
        (status(fx.repo) == "", f"status empty: {status(fx.repo)!r}"),
        (not os.path.isdir(os.path.join(fx.repo, ".claude")), "no .claude directory in the repo"),
        (trust["projects"][os.path.realpath(fx.repo)]["hasTrustDialogAccepted"] is True, "trusts the main checkout"),
        ("gantry-guard.py" in settings and "exit 2" in settings, "user hook fails closed"),
        ("0.1.0" in fx.run("--version").stdout, "version is 0.1.0"),
    ]


def case_allowing_hook_silent(fx):
    fx.settings(fx.home_settings, {"hooks": {"PreToolUse": [entry("Bash", fx.hook("marker.sh", MARKER))]}})
    result = fx.install()
    return [
        (os.path.exists(os.path.join(fx.home, "probe-ran")), "probe ran the hook"),
        (result.stderr == "", "stderr empty"),
        ("Nothing in the target repo was committed." in result.stdout, "committed nothing"),
    ]


def case_hooks_kept(fx):
    blocker = dangerous_in_home(fx)
    merge = fx.hook("deny-merge.py", DENY_MERGE)
    fx.settings(fx.local_settings, {"hooks": {"PreToolUse": [entry("Bash", merge)]}})
    watched = [blocker, merge, fx.local_settings]

    def snap():
        return [(open(path, "rb").read(), os.stat(path).st_mtime_ns) for path in watched]

    before = snap()
    fx.settings(fx.home_settings, {"hooks": {"PreToolUse": [entry("Bash", "echo mine"), entry("Bash", blocker)]}})
    err = fx.install().stderr
    settings = json.load(open(fx.home_settings))
    commands = []
    for group in settings["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            commands.append(hook["command"])
    return [
        (HEADER in err, "probe ran"),
        (snap() == before, "foreign hook files unchanged"),
        ("echo mine" in commands, "foreign command kept"),
        (sum("gantry-guard.py" in command for command in commands) == 1, f"one guard command: {commands}"),
    ]


def case_per_command_merge(fx):
    fx.settings(fx.home_settings, {"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [
            {"type": "command", "command": "echo mine"},
            {"type": "command", "command": "/old/.gantry/current/kit/gantry-guard.py"},
        ],
    }]}})
    fx.install()
    settings = json.load(open(fx.home_settings))
    commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
    return [
        ("echo mine" in commands, "foreign command in a shared group stays"),
        (sum("gantry-guard.py" in command for command in commands) == 1, "old guard command replaced once"),
    ]


def case_idempotent(fx):
    first = fx.install()
    settings = open(fx.home_settings, "rb").read()
    reg = registry(fx.home)
    config = open(os.path.join(fx.repo, ".git", "gantry.json")).read()
    open(os.path.join(fx.repo, ".git", "gantry.json"), "w").write('{"autoMerge": false}\n')
    second = fx.install()
    return [
        (first.returncode == 0 and second.returncode == 0, "both installs exit 0"),
        (open(fx.home_settings, "rb").read() == settings, "settings bytes unchanged"),
        (registry(fx.home) == reg, "registry unchanged"),
        (open(os.path.join(fx.repo, ".git", "gantry.json")).read() == '{"autoMerge": false}\n', "config not rewritten"),
        (status(fx.repo) == "", "status still empty"),
        (len(reg["repos"]) == 1, "one registry key"),
    ]


def case_status_clean_and_worktree(fx):
    sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init", cwd=fx.repo)
    fx.script(os.path.join(fx.bin, "orca"), ORCA_TOUCH.replace("$ORCA_LOG", json.dumps(os.path.join(fx.tmp, "orca.log"))[1:-1] if False else "#!/bin/sh\necho orca >> \"$ORCA_LOG\"\nexit 0\n"))
    result = fx.install()
    wt = os.path.join(fx.tmp, "wt")
    sh("git", "worktree", "add", "-q", "-b", "feat", wt, cwd=fx.repo)
    hook = os.path.join(fx.home, ".gantry", "current", "kit", "gantry-guard.py")
    env = fx.env(ORCA_LOG=os.path.join(fx.tmp, "orca.log"), CLAUDE_PROJECT_DIR=wt, ORCA_TERMINAL_HANDLE="term_worker")
    denied = subprocess.run([sys.executable, hook], input=json.dumps({
        "cwd": wt, "tool_name": "Bash", "tool_input": {"command": "rm -rf x"},
        "agent_type": "gantry-coordinator", "permission_mode": "bypassPermissions",
    }), text=True, capture_output=True, env=env)
    worker = subprocess.run([sys.executable, hook], input=json.dumps({
        "cwd": wt, "tool_name": "Bash", "tool_input": {"command": "git push origin main"},
        "permission_mode": "bypassPermissions",
    }), text=True, capture_output=True, env=env)
    prepush = os.path.join(fx.repo, ".git", "hooks", "pre-push")
    pushed = subprocess.run([prepush], input="abc def refs/heads/main " + ("0" * 40) + "\n", text=True, capture_output=True, env=env, cwd=wt)
    return [
        (result.returncode == 0, result.stderr),
        (status(fx.repo) == "", f"status after install: {status(fx.repo)!r}"),
        (status(wt) == "", "worktree status empty"),
        (denied.returncode == 2 and "gantry guard:" in denied.stderr, f"worktree coordinator denied: {denied.returncode} {denied.stderr}"),
        (worker.returncode == 2, f"worktree worker denied: {worker.returncode} {worker.stderr}"),
        (pushed.returncode == 1 and "gantry pre-push" in pushed.stderr, f"pre-push denies main: {pushed.returncode} {pushed.stderr}"),
    ]


def case_unregistered_is_dormant(fx):
    fx.script(os.path.join(fx.bin, "orca"), "#!/bin/sh\necho called >> \"$ORCA_LOG\"\nexit 0\n")
    fx.install()
    other = os.path.join(fx.tmp, "other")
    os.makedirs(other)
    sh("git", "init", "-q", "-b", "main", cwd=other)
    sh("git", "remote", "add", "origin", "https://github.com/example-org/unregistered.git", cwd=other)
    log = os.path.join(fx.tmp, "orca.log")
    env = fx.env(ORCA_LOG=log, CLAUDE_PROJECT_DIR=other, ORCA_TERMINAL_HANDLE="term_worker")
    hook = os.path.join(fx.home, ".gantry", "current", "kit", "gantry-guard.py")
    allowed = subprocess.run([sys.executable, hook], input=json.dumps({
        "cwd": other, "tool_name": "Bash", "tool_input": {"command": "rm -rf x"},
        "agent_type": "gantry-coordinator", "permission_mode": "bypassPermissions",
    }), text=True, capture_output=True, env=env)
    run_hook = os.path.join(fx.home, ".gantry", "current", "kit", "gantry_run.py")
    quiet = subprocess.run([sys.executable, run_hook], input=json.dumps({
        "cwd": other, "context_window": {"total_input_tokens": 5000},
    }), text=True, capture_output=True, env=env)
    prepush = os.path.join(fx.home, ".gantry", "current", "kit", "pre-push")
    pushed = subprocess.run([prepush], input="", text=True, capture_output=True, env=env, cwd=other)
    return [
        (allowed.returncode == 0 and allowed.stderr == "" and allowed.stdout == "", f"unregistered allows: {allowed.returncode} {allowed.stderr}"),
        (quiet.returncode == 0 and quiet.stdout == "", f"run hook silent: {quiet.stdout!r} {quiet.stderr}"),
        (pushed.returncode == 0, f"pre-push allows unregistered: {pushed.returncode} {pushed.stderr}"),
        (not os.path.exists(log), "unregistered path does not call orca"),
    ]


def case_uninstall_restores(fx):
    prior = {
        "hooks": {"PreToolUse": [entry("Bash", "echo mine")]},
        "statusLine": {"type": "command", "command": "echo mine"},
    }
    fx.settings(fx.home_settings, prior)
    before = open(fx.home_settings, "rb").read()
    fx.install()
    removed = fx.run("uninstall", "--all")
    again = fx.run("uninstall", "--all")
    return [
        (removed.returncode == 0, removed.stderr),
        (open(fx.home_settings, "rb").read() == before, "prior settings bytes restored"),
        (not os.path.exists(os.path.join(fx.home, ".claude", "agents", "gantry-coordinator.md")), "agent removed"),
        (not os.path.exists(os.path.join(fx.home, ".gantry")), "kit removed after settings restore"),
        (not os.path.exists(os.path.join(fx.repo, ".git", "hooks", "pre-push")), "pre-push stub removed with --all"),
        (again.returncode == 0, "second --all exits 0"),
        (status(fx.repo) == "", "uninstall leaves the repo clean"),
    ]


def case_uninstall_one_repo(fx):
    fx.install()
    other = os.path.join(fx.tmp, "other")
    os.makedirs(other)
    sh("git", "init", "-q", "-b", "main", cwd=other)
    sh("git", "remote", "add", "origin", "https://github.com/example-org/example-repo.git", cwd=other)
    second = fx.run("install", other)
    gone = fx.run("uninstall", fx.repo)
    data = registry(fx.home)
    settings = open(fx.home_settings).read()
    return [
        (second.returncode == 0 and gone.returncode == 0, gone.stderr),
        ("https://github.com/example-user/example-fixture" not in data["repos"], "fixture origin unregistered"),
        ("https://github.com/example-org/example-repo" in data["repos"], "other origin stays"),
        ("gantry-guard.py" in settings, "user hooks stay while another repo is registered"),
        (not os.path.exists(os.path.join(fx.repo, ".git", "hooks", "pre-push")), "our pre-push stub removed"),
    ]


def set_origin(fx, url):
    sh("git", "remote", "set-url", "origin", url, cwd=fx.repo)


def case_origin_shapes(fx):
    checks = []
    samples = [
        "https://github.com/example-org/example-repo.git",
        "https://github.com/octocat/hello",
        "git@github.com:example-org/example-repo.git",
        "ssh://git@github.com/octocat/hello.git",
        "https://ghe.example.com/acme/widgets.git",
        "git@ghe.example.com:acme/widgets.git",
        "ssh://git@ghe.example.com:2222/acme/widgets.git",
    ]
    for url in samples:
        set_origin(fx, url)
        result = fx.install()
        checks.append((result.returncode == 0, f"{url} accepted: {result.stderr}"))
    set_origin(fx, "https://github.com/Example-Org/Example-Repo.git")
    fx.install()
    set_origin(fx, "git@github.com:example-org/example-repo.git")
    fx.install()
    keys = sorted(registry(fx.home)["repos"])
    checks.append((keys.count("https://github.com/example-org/example-repo") == 1, f"ssh and https share one key: {keys}"))
    config = json.load(open(os.path.join(fx.repo, ".git", "gantry.json")))
    checks.append((config.get("autoMerge") is True and config.get("forbidden") == [] and "owners" not in config, "default config"))
    return checks


def case_origin_missing(fx):
    sh("git", "remote", "remove", "origin", cwd=fx.repo)
    result = fx.install()
    return [
        (result.returncode != 0, "no origin fails"),
        ("no origin remote" in result.stderr, result.stderr),
        (status(fx.repo) == "", "writes nothing in the worktree"),
        (registry(fx.home) is None, "does not register"),
    ]


def case_origin_unparseable(fx):
    set_origin(fx, "/tmp/not-a-remote")
    result = fx.install()
    set_origin(fx, "https://github.com/only-one")
    second = fx.install()
    return [
        (result.returncode != 0 and "not a parseable git remote" in result.stderr, result.stderr),
        (second.returncode != 0 and "not a parseable git remote" in second.stderr, second.stderr),
        (registry(fx.home) is None, "unparseable origin is not registered"),
    ]


def case_exclude_untouched(fx):
    path = subprocess.check_output(["git", "-C", fx.repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], text=True).strip()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("# mine\n*.log")
    fx.install()
    return [(open(path).read() == "# mine\n*.log", "exclude file unchanged"), ("gantry" not in open(path).read(), "no gantry exclude line")]


def case_foreign_status_and_prepush(fx):
    fx.settings(fx.home_settings, {"statusLine": {"type": "command", "command": "echo mine"}})
    foreign = os.path.join(fx.repo, ".git", "hooks", "pre-push")
    os.makedirs(os.path.dirname(foreign), exist_ok=True)
    with open(foreign, "w") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    result = fx.install()
    settings = json.load(open(fx.home_settings))
    return [
        (result.returncode == 0, result.stderr),
        (settings["statusLine"]["command"] == "echo mine", "foreign status line kept"),
        ("left it alone" in result.stderr, "foreign pre-push reported"),
        (open(foreign).read() == "#!/bin/sh\nexit 0\n", "foreign pre-push kept"),
    ]


def case_doctor(fx):
    fx.install()
    result = fx.run("doctor", fx.repo)
    text = result.stdout
    return [
        (result.returncode == 0, result.stderr),
        ("version: 0.1.0" in text, text),
        ("registered: https://github.com/example-user/example-fixture" in text, text),
        ("hooks: gantry guard present" in text, text),
        ("pre-push: gantry stub" in text, text),
    ]


CASES = [
    case_dangerous_hook, case_json_deny, case_edit_matcher, case_exit_one, case_minimal_env,
    case_clean_home, case_allowing_hook_silent, case_hooks_kept, case_per_command_merge,
    case_idempotent, case_status_clean_and_worktree, case_unregistered_is_dormant,
    case_uninstall_restores, case_uninstall_one_repo, case_origin_shapes, case_origin_missing,
    case_origin_unparseable, case_exclude_untouched, case_foreign_status_and_prepush, case_doctor,
]


def main():
    failures = 0
    total = 0
    for case in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            checks = case(Fixture(tmp))
        for ok, label in checks:
            total += 1
            if not ok:
                failures += 1
                print(f"FAIL {case.__name__}: {label}")
    print(f"{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
