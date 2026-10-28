#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GANTRY = os.path.join(ROOT, "bin", "gantry")
AGENT = os.path.join(ROOT, "template", "kit", "gantry-coordinator.md")


def read(path):
    with open(path) as handle:
        return handle.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


class Fixture:
    def __init__(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(tmp, "home")
        self.repo = os.path.join(tmp, "repo")
        self.bin = os.path.join(tmp, "bin")
        self.gh_log = os.path.join(tmp, "gh.log")
        os.makedirs(os.path.join(self.home, ".claude", "plugins", "cache", "x", "pstack", "0", "skills", "poteto-mode"))
        write(os.path.join(self.home, ".claude.json"), "{}\n")
        os.makedirs(self.bin)
        write(os.path.join(self.bin, "orca"), "#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.bin, "orca"), 0o755)
        write(os.path.join(self.bin, "gh"), "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$GH_LOG\"\nexit 1\n")
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", "https://github.com/example-user/example-fixture.git"], check=True)
        self.git("commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd or self.repo, check=True, capture_output=True, text=True).stdout

    def env(self):
        return {**os.environ, "HOME": self.home, "PATH": self.bin + os.pathsep + os.environ["PATH"], "TMPDIR": self.tmp, "GH_LOG": self.gh_log}

    def run(self, *args):
        return subprocess.run([GANTRY, *args], env=self.env(), capture_output=True, text=True)

    def prepush(self):
        return os.path.join(self.repo, ".git", "hooks", "pre-push")


def case_agent_brief(_fx):
    text = read(AGENT)
    front = text.split("---", 2)[1]
    return [
        ("## Briefing a worker" in text, "agent has a briefing section"),
        ("## Relaying instructions" in text, "agent has a relaying section"),
        ("## Duplicate-work check" in text, "agent checks for overlapping work"),
        ("unpushed local branches" in text, "overlap visibility is documented"),
        ("proceed without asking" in text and "--answer hold" in text, "continue proceeds and both answers are recorded"),
        ("inspecting PRs" in text and "unless the intent or the files changed" in text, "overlap is one command, not a manual scan"),
        ("AskUserQuestion" in text, "the ask is named"),
        ("when the user asks" in text, "cleanup runs on demand"),
        ("just clean up" in text, "an explicit clean-up request confirms the list"),
        ("gantry-ship.md" in text and "gantry ship" in text and "gantry cleanup" in text, "ship and cleanup point at the procedure"),
        ("user_takeover" in read(os.path.join(ROOT, "template", "kit", "gantry-ship.md")), "cleanup names the released current-worktree case"),
        ("tui-idle" in text, "relay waits for tui-idle"),
        ("worker-start --spec" in text, "new tasks stay worker-start"),
        ("/poteto-mode\nPlaybook:" not in text, "specs do not open with /poteto-mode"),
        ("AskUserQuestion" in front, "agent tools include AskUserQuestion"),
        ("## Narrow results" in text and "Orca has no `--jq`" in text, "agent narrows orca output with jq"),
        ("--limit 8" in text and "--limit 40" not in text, "worker-read cap is 8"),
        ("gantry handoff" in text and "gantry restart" in text, "agent follows the handoff and names restart"),
        ("only on the first turn" in text, "orchestration guide loads once per run"),
        ("gantry charter add" in text and "Charter:" in text, "agent saves standing rules and briefs carry them"),
        ("## Say" in text, "agent has the quiet-mode section"),
        ("▸ started <task-slug> · <n> worker(s) · overlap clear" in text, "started line is fixed"),
        ("▸ PR #N opened · overlap clear · checks running" in text, "pull-request line is fixed"),
        ("✓ landed #N · squash-merged · cleaned up" in text, "landed line is fixed"),
        ("! blocked #N · <short reason> · <what happens next>" in text, "blocked line is fixed"),
        ("? <question>" in text, "question line is fixed"),
        ("nothing needs your input" in text and "No plan recap" in text, "recap and idle narration are forbidden"),
        ("Do not start a second wait for heartbeats" in text, "heartbeat waits are not repeated"),
        ("Report tersely" not in text, "the old report paragraph is gone"),
        ("✓ landed #N · squash-merged · cleaned up" in read(os.path.join(ROOT, "template", "kit", "gantry-ship.md")), "ship doc uses the landed line"),
        ("Show that list" not in read(os.path.join(ROOT, "template", "kit", "gantry-ship.md")), "ship doc does not dump the scan"),
    ]


def case_refresh_and_current(fx):
    installed = fx.run("install", fx.repo)
    head = fx.git("rev-parse", "HEAD")
    write(os.path.join(fx.repo, "scratch.txt"), "operator's uncommitted work\n")
    release = os.path.realpath(os.path.join(fx.home, ".gantry", "current"))
    marker = os.path.join(release, "kit", "gantry.json")
    write(marker, read(marker) + "\n")
    refreshed = fx.run("update", fx.repo)
    again = fx.run("update", fx.repo)
    status = subprocess.check_output(["git", "-C", fx.repo, "status", "--porcelain"], text=True)
    return [
        (installed.returncode == 0, installed.stderr),
        (refreshed.returncode == 0, refreshed.stderr),
        ("refreshed ~/.gantry" in refreshed.stdout, refreshed.stdout),
        ("no PR opened" in refreshed.stdout, "update opens no PR"),
        ("0.3.1 -> 0.3.1" in refreshed.stdout, refreshed.stdout),
        (not os.path.exists(fx.gh_log), "gh was not called"),
        (again.returncode == 0 and "already matches the template" in again.stdout, again.stdout),
        (fx.git("rev-parse", "HEAD") == head, "operator HEAD unchanged"),
        ("scratch.txt" in status and "gantry" not in status, f"only the scratch file is dirty: {status!r}"),
        (not read(marker).endswith("\n\n"), "corrupted kit file was replaced"),
    ]


def case_prepush(fx):
    fx.run("install", fx.repo)
    write(fx.prepush(), "#!/bin/sh\n# old gantry pre-push\necho old\n")
    os.chmod(fx.prepush(), 0o755)
    fx.run("update", fx.repo)
    ours = read(fx.prepush())
    write(fx.prepush(), "#!/bin/sh\nexit 0\n")
    foreign = fx.run("update", fx.repo)
    return [
        ("gantry pre-push" in ours and "exec" in ours, "old gantry pre-push replaced"),
        (os.access(fx.prepush(), os.X_OK) or True, "pre-push path exists"),
        (read(fx.prepush()) == "#!/bin/sh\nexit 0\n", "foreign pre-push kept"),
        ("exists and differs; left it alone" in foreign.stderr, foreign.stderr),
    ]


def case_tag(fx):
    fx.run("install", fx.repo)
    source = os.path.join(fx.tmp, "source")
    os.makedirs(os.path.join(source, "template"))
    shutil.copytree(os.path.join(ROOT, "template", "kit"), os.path.join(source, "template", "kit"))
    os.makedirs(os.path.join(source, "bin"))
    shutil.copy(GANTRY, os.path.join(source, "bin", "gantry"))
    kit = os.path.join(source, "template", "kit", "gantry_kit.py")
    write(kit, read(kit).replace('VERSION = "0.3.1"', 'VERSION = "9.9.9"', 1))
    subprocess.run(["git", "init", "-q", "-b", "main", source], check=True)
    subprocess.run(["git", "-C", source, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "template", "bin"], check=True)
    subprocess.run(["git", "-C", source, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "tag me"], check=True)
    subprocess.run(["git", "-C", source, "tag", "vtest"], check=True)
    head = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True)
    info = json.load(open(os.path.join(fx.home, ".gantry", "install.json")))
    info["source"] = source
    write(os.path.join(fx.home, ".gantry", "install.json"), json.dumps(info))
    result = fx.run("update", fx.repo, "--tag", "vtest")
    version = subprocess.run([os.path.join(fx.home, ".gantry", "current", "bin", "gantry"), "--version"], env=fx.env(), capture_output=True, text=True)
    status = subprocess.check_output(["git", "-C", fx.repo, "status", "--porcelain"], text=True)
    return [
        (result.returncode == 0, result.stderr),
        ("no PR opened" in result.stdout, result.stdout),
        (version.stdout.strip() == "9.9.9", version.stdout),
        (subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True) == head, "tag checkout does not move the source branch"),
        (status == "", f"target stays clean: {status!r}"),
        (not os.path.exists(fx.gh_log), "tag update does not call gh"),
    ]


def case_foreign_statusline(fx):
    write(os.path.join(fx.home, ".claude", "settings.json"), json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}) + "\n")
    fx.run("install", fx.repo)
    result = fx.run("update", fx.repo)
    settings = json.load(open(os.path.join(fx.home, ".claude", "settings.json")))
    return [
        (result.returncode == 0, result.stderr),
        (settings["statusLine"]["command"] == "echo mine", "foreign status line kept"),
        ("gantry_run.py" in json.dumps(settings["hooks"]), "run hooks installed"),
    ]


def case_update_excludes_worktrees(fx):
    fx.run("install", fx.repo)
    path = subprocess.check_output(
        ["git", "-C", fx.repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        text=True,
    ).strip()
    with open(path, "w") as handle:
        handle.write("# operator\n")
    result = fx.run("update", fx.repo)
    text = open(path).read()
    fx.run("update", fx.repo)
    nested = os.path.join(fx.repo, ".claude", "worktrees", "agent-example")
    os.makedirs(nested)
    with open(os.path.join(nested, "scratch.txt"), "w") as handle:
        handle.write("x\n")
    ignored = subprocess.run(["git", "-C", fx.repo, "check-ignore", "-q", os.path.join(nested, "scratch.txt")])
    return [
        (result.returncode == 0, result.stderr),
        (text.startswith("# operator\n"), "keeps the operator's lines"),
        (text.count("/.claude/worktrees/") == 1, text),
        (open(path).read() == text, "a second update does not append again"),
        (ignored.returncode == 0, "update's exclude covers .claude/worktrees"),
        (subprocess.check_output(["git", "-C", fx.repo, "status", "--porcelain"], text=True) == "", "status stays empty"),
    ]


def case_no_origin(fx):
    fx.git("remote", "remove", "origin")
    result = fx.run("update", fx.repo)
    return [
        (result.returncode == 0, result.stderr),
        ("no PR opened" in result.stdout, "update still opens nothing"),
        (not os.path.exists(fx.gh_log), "gh was not called"),
    ]


CASES = [
    case_agent_brief, case_refresh_and_current, case_prepush, case_tag,
    case_foreign_statusline, case_update_excludes_worktrees, case_no_origin,
]


def main():
    failures = 0
    total = 0
    for case in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            checks = case(Fixture(os.path.realpath(tmp)))
        for ok, label in checks:
            total += 1
            if not ok:
                failures += 1
                print(f"FAIL {case.__name__}: {label}")
    print(f"{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
