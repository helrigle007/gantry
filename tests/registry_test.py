#!/usr/bin/env python3
"""A missing or unreadable registry falls back to .git/gantry.json per repo."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "template", "kit")
GANTRY = os.path.join(ROOT, "bin", "gantry")
sys.path.insert(0, KIT)
import gantry_kit

WARNING = "gantry: registry is unreadable; run gantry doctor"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def load_guard():
    path = os.path.join(KIT, "gantry-guard.py")
    spec = importlib.util.spec_from_file_location("gantry_guard_registry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(tmp):
    home = os.path.join(tmp, "home")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(os.path.join(home, ".claude", "plugins", "cache", "x", "pstack", "0", "skills", "poteto-mode"))
    with open(os.path.join(home, ".claude.json"), "w") as handle:
        handle.write("{}\n")
    os.makedirs(bindir)
    orca = os.path.join(bindir, "orca")
    with open(orca, "w") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(orca, 0o755)
    env = os.environ.copy()
    env["HOME"] = home
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["TMPDIR"] = tmp
    env.pop("CLAUDE_PROJECT_DIR", None)
    return home, env


def init_repo(path, origin):
    os.makedirs(path)
    git("init", "-q", "-b", "main", cwd=path)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    git("remote", "add", "origin", origin, cwd=path)


def run(args, env, cwd):
    return subprocess.run([GANTRY, *args], cwd=cwd, env=env, capture_output=True, text=True)


def hook_write(guard, repo, env):
    payload = {
        "cwd": repo,
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "x.txt", "content": "x"},
        "agent_type": "gantry-coordinator",
    }
    return subprocess.run([sys.executable, guard], input=json.dumps(payload), text=True, capture_output=True, env=env, cwd=repo)


def test_corrupt_fallback_and_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        home, env = prepare(tmp)
        repo = os.path.join(tmp, "repo")
        other = os.path.join(tmp, "other")
        init_repo(repo, "https://github.com/example-org/example-repo.git")
        init_repo(other, "https://github.com/example-org/example-other.git")
        installed = run(["install", repo], env, repo)
        assert installed.returncode == 0, installed.stderr
        registry = os.path.join(home, ".gantry", "registry.json")
        with open(registry, "w") as handle:
            handle.write("{")
        guard = load_guard()
        os.environ["HOME"] = home
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

        def decide(path):
            return guard.decide({
                "cwd": path,
                "permission_mode": "bypassPermissions",
                "tool_name": "Write",
                "tool_input": {"file_path": "x.txt", "content": "x"},
                "agent_type": "gantry-coordinator",
            })

        allowed, reason = decide(repo)
        assert allowed is False and "never writes code" in reason, (allowed, reason)
        hook = hook_write(os.path.join(KIT, "gantry-guard.py"), repo, env)
        assert hook.returncode == 2 and WARNING in hook.stderr, hook.stderr
        bare = decide(other)
        assert bare[0] is True and bare[1] == "", bare
        other_hook = hook_write(os.path.join(KIT, "gantry-guard.py"), other, env)
        assert other_hook.returncode == 0, other_hook.stderr
        assert WARNING in other_hook.stderr, other_hook.stderr
        wt = os.path.join(tmp, "wt")
        git("worktree", "add", "-q", "-b", "feat", wt, cwd=repo)
        worktree = decide(wt)
        assert worktree[0] is False and "never writes code" in worktree[1], worktree
        saved_bytes = open(registry).read()
        os.remove(registry)
        missing_repo = decide(repo)
        assert missing_repo[0] is False and "never writes code" in missing_repo[1], missing_repo
        missing_other = decide(other)
        assert missing_other[0] is True and missing_other[1] == "", missing_other
        with open(registry, "w") as handle:
            handle.write(saved_bytes)
        doctor = run(["doctor", repo], env, repo)
        assert doctor.returncode != 0, doctor.stdout
        assert "unreadable" in doctor.stdout or "unreadable" in doctor.stderr, doctor.stdout + doctor.stderr
        prepush = os.path.join(KIT, "pre-push")
        denied = subprocess.run([prepush], input="a b refs/heads/main " + ("0" * 40) + "\n", text=True, capture_output=True, env=env, cwd=wt)
        assert denied.returncode != 0 and "refusing to push" in denied.stderr and WARNING in denied.stderr, denied.stderr
        allowed_push = subprocess.run([prepush], input="", text=True, capture_output=True, env=env, cwd=other)
        assert allowed_push.returncode == 0 and WARNING in allowed_push.stderr, allowed_push.stderr
        saved = gantry_kit.fallback_governed
        gantry_kit.fallback_governed = lambda common, key: None
        try:
            mutated, mutated_reason = decide(repo)
        finally:
            gantry_kit.fallback_governed = saved
        original_assertion = mutated is False and "never writes code" in mutated_reason
        assert original_assertion is False
        assert mutated is True, mutated_reason
        print("mutation: fallback forced dormant, so test (1) failed and the write was allowed")


def test_install_refuses_without_repair_and_repair_rebuilds():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        home, env = prepare(tmp)
        repo = os.path.join(tmp, "repo")
        init_repo(repo, "https://github.com/example-org/example-repo.git")
        assert run(["install", repo], env, repo).returncode == 0
        registry = os.path.join(home, ".gantry", "registry.json")
        with open(registry, "w") as handle:
            handle.write("{")
        before = open(registry, "rb").read()
        refused = run(["install", repo], env, repo)
        assert refused.returncode != 0 and "--repair" in refused.stderr, refused.stderr
        assert open(registry, "rb").read() == before
        repaired = run(["install", "--repair", repo], env, repo)
        assert repaired.returncode == 0, repaired.stderr
        assert "started empty" in repaired.stdout, repaired.stdout
        data = json.load(open(registry))
        assert "https://github.com/example-org/example-repo" in data["repos"], data
        with open(registry, "w") as handle:
            handle.write("not json " + repo)
        found = run(["install", "--repair", repo], env, repo)
        assert found.returncode == 0, found.stderr
        assert "repaired from" in found.stdout, found.stdout
        again = json.load(open(registry))
        assert os.path.realpath(repo) in again["repos"]["https://github.com/example-org/example-repo"]["checkouts"]


if __name__ == "__main__":
    test_corrupt_fallback_and_mutation()
    test_install_refuses_without_repair_and_repair_rebuilds()
    print("2/2 passed")
