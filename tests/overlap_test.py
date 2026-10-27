#!/usr/bin/env python3
"""Duplicate-work check. The script ranks, labels, and records the verdict.

The current task's own branch, worker, and pull request are left out first.
"""
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

INTENT = "Fix the crash in app"
PLANNED = ["src/app.py", "tests/test_app.py"]
PRS = [
    {
        "number": 1,
        "title": "Fix the crash in app",
        "body": "The crash is in the request path.",
        "headRefName": "fix-crash",
        "url": "https://github.com/example-org/example-repo/pull/1",
        "files": [{"path": "src/app.py"}, {"path": "tests/test_app.py"}],
    },
    {
        "number": 2,
        "title": "Rename the banner",
        "body": "Cosmetic copy only.",
        "headRefName": "banner",
        "url": "https://github.com/example-org/example-repo/pull/2",
        "files": [{"path": "src/app.py"}],
    },
    {
        "number": 3,
        "title": "Update readme",
        "body": "Docs only.",
        "headRefName": "docs-readme",
        "url": "https://github.com/example-org/example-repo/pull/3",
        "files": [{"path": "README.md"}],
    },
]
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    print(open(os.environ["GH_PRS"]).read())
elif args[:2] == ["issue", "list"]:
    print("[]")
else:
    sys.stderr.write("unexpected gh " + " ".join(args) + "\\n")
    sys.exit(1)
"""
FAKE_ORCA = """#!/usr/bin/env python3
import json, os, sys
fixture = json.load(open(os.environ["GANTRY_ORCA_FIXTURE"]))
args = [arg for arg in sys.argv[1:] if arg != "--json"]
key = "run-current" if args[:2] == ["orchestration", "run-current"] else None
key = "task-list" if args[:2] == ["orchestration", "task-list"] else key
key = "worker-list" if args[:2] == ["orchestration", "worker-list"] else key
key = "gate-list" if args[:2] == ["orchestration", "gate-list"] else key
if key is None:
    print(json.dumps({"ok": False, "error": "unknown"}))
else:
    print(json.dumps({"ok": True, "result": fixture[key]}))
"""


def write_exec(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", repo, *args], check=True, capture_output=True)


def load_guard():
    spec = importlib.util.spec_from_file_location("gantry_guard", os.path.join(KIT, "gantry-guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def decide(guard, repo, command):
    return guard.decide({
        "cwd": repo,
        "permission_mode": "bypassPermissions",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })


def test_rank_classify_and_log():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        bare = os.path.join(tmp, "bare.git")
        repo = os.path.join(tmp, "repo")
        home = os.path.join(tmp, "home")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(home)
        git(tmp, "init", "-q", "--bare", "-b", "main", bare)
        git(tmp, "clone", "-q", bare, repo)
        git(repo, "remote", "set-url", "origin", "https://github.com/example-org/example-repo.git")
        git(repo, "config", f"url.{bare}/.insteadOf", "https://github.com/example-org/example-repo.git")
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(repo, "push", "-q", "origin", "HEAD:main")
        for branch in ("fix-crash", "banner", "docs-readme", "extra-topic"):
            git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        prs = os.path.join(tmp, "prs.json")
        with open(prs, "w") as handle:
            json.dump(PRS, handle)
        write_exec(os.path.join(bindir, "gh"), FAKE_GH)
        write_exec(os.path.join(bindir, "orca"), "#!/bin/sh\nexit 1\n")
        env = os.environ.copy()
        env.update({"HOME": home, "TMPDIR": tmp, "PATH": bindir + os.pathsep + env["PATH"], "GH_PRS": prs})
        proc = subprocess.run(
            [os.path.join(ROOT, "bin", "gantry"), "overlap", "--checkpoint", "pre-pr", "--intent", INTENT,
             "--files", "src/app.py,tests/test_app.py", "--run", "run_1", "--task", "task_impl"],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        prs_out = [item for item in data["candidates"] if item["kind"] == "pr"]
        assert [item["id"] for item in prs_out] == ["1", "2", "3"], prs_out
        assert [item["score"] for item in prs_out] == [2, 1, 0]
        labels = [item["label"] for item in prs_out]
        actions = [gantry_kit.ACTIONS[label] for label in labels]
        assert labels == ["duplicate", "related", "unrelated"], labels
        assert labels == [gantry_kit.classify(INTENT, PLANNED, item) for item in prs_out]
        assert actions == ["stop", "flag", "ignore"], actions
        assert data["verdict"] == "stop"
        assert data["verdict"] == gantry_kit.verdict_of([item["label"] for item in data["candidates"]])
        assert any(item["kind"] == "branch" and item["id"] == "extra-topic" for item in data["candidates"])
        assert "unpushed" in data["sees"]
        prev_tmp = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = tmp
        events = gantry_kit.read_overlap("run_1")
        assert events and events[0]["kind"] == "gathered" and events[0]["checkpoint"] == "pre-pr"
        assert events[0]["candidates"][0]["label"] == "duplicate"
        classified = events[-1]
        assert classified["kind"] == "classified" and classified["verdict"] == "stop"
        assert classified["checkpoint"] == "pre-pr" and classified["task"] == "task_impl"
        assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is False
        recorded = subprocess.run(
            [os.path.join(ROOT, "bin", "gantry"), "overlap", "--record", "classified", "--checkpoint", "pre-pr",
             "--run", "run_1", "--task", "task_impl", "--verdict", "stop"],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert recorded.returncode == 0, recorded.stderr
        assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is False
        subprocess.run(
            [os.path.join(ROOT, "bin", "gantry"), "overlap", "--record", "answered", "--checkpoint", "pre-pr",
             "--run", "run_1", "--task", "task_impl", "--answer", "proceed"],
            cwd=repo, env=env, check=True, capture_output=True, text=True,
        )
        assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is True
        fixture = {
            "run-current": {"run": {"id": "run_1", "objective": INTENT}},
            "task-list": {"tasks": [], "page": {}},
            "worker-list": {"workers": [], "page": {}},
            "gate-list": {"gates": [], "count": 0},
        }
        path = os.path.join(tmp, "fixture.json")
        with open(path, "w") as handle:
            json.dump(fixture, handle)
        write_exec(os.path.join(bindir, "orca"), FAKE_ORCA)
        env["GANTRY_ORCA_FIXTURE"] = path
        env["CLAUDE_PROJECT_DIR"] = repo
        saved = os.environ.copy()
        os.environ.update(env)
        try:
            sys.path.insert(0, KIT)
            import gantry_run
            state, _handoff = gantry_run.refresh(repo)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        assert any(event["kind"] == "answered" for event in state["overlap"])
        disk = json.load(open(os.path.join(tmp, "gantry-mailbox", "run_1", "state.json")))
        assert any(event["checkpoint"] == "pre-pr" for event in disk["overlap"])
        if prev_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev_tmp


def test_gate_and_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        repo = os.path.join(tmp, "repo")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        os.makedirs(repo)
        git(tmp, "init", "-q", "-b", "main", repo)
        git(repo, "remote", "add", "origin", "https://github.com/example-org/example-repo.git")
        saved_home = os.environ.get("HOME")
        saved_tmp = os.environ.get("TMPDIR")
        os.environ["HOME"] = home
        os.environ["TMPDIR"] = tmp
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            gantry_kit.register(gantry_kit.normalize_origin("https://github.com/example-org/example-repo.git"), repo)
            guard = load_guard()

            def fake_orca(*args):
                if args[0] == "worker-list":
                    return {"workers": [{"agentTerminalHandle": "term_worker", "taskId": "task_impl", "runId": "run_1", "terminalState": "active", "dispatchStatus": "running"}], "page": {}}
                if args[0] == "task-list":
                    return {"tasks": [{"id": "task_impl", "task_title": "impl"}]}
                return {"run": {"id": "run_1"}}

            guard.orca = fake_orca
            os.environ["ORCA_TERMINAL_HANDLE"] = "term_worker"
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is False and "pre-pr" in reason, (allowed, reason)
            gantry_kit.record_overlap("run_1", {"kind": "classified", "checkpoint": "pre-pr", "task": "task_impl", "run": "run_1", "verdict": "continue"})
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is True, reason
            os.remove(gantry_kit.overlap_path("run_1"))
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is False and "pre-pr" in reason
            saved = guard.pr_create_allowed
            guard.pr_create_allowed = lambda ctx: True
            try:
                mutated, mutated_reason = decide(guard, repo, "gh pr create --title x --body y")
            finally:
                guard.pr_create_allowed = saved
            original_assertion = mutated is False and "pre-pr" in mutated_reason
            assert original_assertion is False
            assert mutated is True, mutated_reason
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            if saved_tmp is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved_tmp
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)


def test_auto_record_and_answered_gate():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        bare = os.path.join(tmp, "bare.git")
        repo = os.path.join(tmp, "repo")
        home = os.path.join(tmp, "home")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(home)
        git(tmp, "init", "-q", "--bare", "-b", "main", bare)
        git(tmp, "clone", "-q", bare, repo)
        git(repo, "remote", "set-url", "origin", "https://github.com/example-org/example-repo.git")
        git(repo, "config", f"url.{bare}/.insteadOf", "https://github.com/example-org/example-repo.git")
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(repo, "push", "-q", "origin", "HEAD:main")
        for branch in ("fix-crash", "banner", "docs-readme", "extra-topic"):
            git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        prs = os.path.join(tmp, "prs.json")
        with open(prs, "w") as handle:
            json.dump(PRS, handle)
        write_exec(os.path.join(bindir, "gh"), FAKE_GH)
        write_exec(os.path.join(bindir, "orca"), "#!/bin/sh\nexit 1\n")
        env = os.environ.copy()
        env.update({"HOME": home, "TMPDIR": tmp, "PATH": bindir + os.pathsep + env["PATH"], "GH_PRS": prs})
        saved_home = os.environ.get("HOME")
        saved_tmp = os.environ.get("TMPDIR")
        os.environ["HOME"] = home
        os.environ["TMPDIR"] = tmp
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            gantry_kit.register(gantry_kit.normalize_origin("https://github.com/example-org/example-repo.git"), repo)
            guard = load_guard()

            def fake_orca(*args):
                if args[0] == "worker-list":
                    return {"workers": [{"agentTerminalHandle": "term_worker", "taskId": "task_impl", "runId": "run_1", "terminalState": "active", "dispatchStatus": "running"}], "page": {}}
                if args[0] == "task-list":
                    return {"tasks": [{"id": "task_impl", "task_title": "impl"}]}
                return {"run": {"id": "run_1"}}

            guard.orca = fake_orca
            os.environ["ORCA_TERMINAL_HANDLE"] = "term_worker"

            def overlap(*args):
                proc = subprocess.run(
                    [os.path.join(ROOT, "bin", "gantry"), "overlap", *args],
                    cwd=repo, env=env, capture_output=True, text=True,
                )
                assert proc.returncode == 0, proc.stderr
                return json.loads(proc.stdout)

            continued = overlap(
                "--checkpoint", "pre-pr", "--intent", "Add a word-count helper",
                "--files", "src/wordcount.py,tests/test_wordcount.py", "--run", "run_1", "--task", "task_impl",
            )
            assert continued["verdict"] == "continue", continued["verdict"]
            assert continued["candidates"]
            assert {item["label"] for item in continued["candidates"]} == {"unrelated"}
            assert all(item["label"] == gantry_kit.classify(continued["intent"], continued["files"], item) for item in continued["candidates"])
            assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is True
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is True, reason

            stopped = overlap(
                "--checkpoint", "pre-pr", "--intent", INTENT,
                "--files", "src/app.py,tests/test_app.py", "--run", "run_1", "--task", "task_impl",
            )
            assert [item["label"] for item in stopped["candidates"] if item["kind"] == "pr"] == ["duplicate", "related", "unrelated"]
            assert stopped["verdict"] == "stop"
            assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is False
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is False and "pre-pr" in reason, (allowed, reason)
            held = subprocess.run(
                [os.path.join(ROOT, "bin", "gantry"), "overlap", "--record", "answered", "--checkpoint", "pre-pr",
                 "--run", "run_1", "--task", "task_impl", "--answer", "hold"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            assert held.returncode == 0, held.stderr
            assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is False
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is False and "pre-pr" in reason, (allowed, reason)
            proceeded = subprocess.run(
                [os.path.join(ROOT, "bin", "gantry"), "overlap", "--record", "answered", "--checkpoint", "pre-pr",
                 "--run", "run_1", "--task", "task_impl", "--answer", "proceed"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            assert proceeded.returncode == 0, proceeded.stderr
            assert gantry_kit.cleared("run_1", "task_impl", "pre-pr") is True
            allowed, reason = decide(guard, repo, "gh pr create --title x --body y")
            assert allowed is True, reason
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            if saved_tmp is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved_tmp
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)


WORD = "example-user/wordcount-7"
OTHER = "example-user/other-count"
INTENT_WORD = "Add src/wordcount_7.py plus a test"
FILES_WORD = "src/wordcount_7.py,tests/test_wordcount_7.py"
WORD_FILES = {
    "src/wordcount_7.py": "def count(text):\n    return len(text.split())\n",
    "tests/test_wordcount_7.py": "def test_count():\n    assert True\n",
}
OTHER_FILES = {"src/wordcount_7.py": "def count(text):\n    return 0\n"}


def pr_row(number, title, branch, files, body=""):
    return {
        "number": number,
        "title": title,
        "body": body,
        "headRefName": branch,
        "url": f"https://github.com/example-org/example-repo/pull/{number}",
        "files": [{"path": path} for path in files],
    }


def seed_repo(tmp, branches):
    bare = os.path.join(tmp, "bare.git")
    repo = os.path.join(tmp, "repo")
    git(tmp, "init", "-q", "--bare", "-b", "main", bare)
    git(tmp, "clone", "-q", bare, repo)
    git(repo, "remote", "set-url", "origin", "https://github.com/example-org/example-repo.git")
    git(repo, "config", f"url.{bare}/.insteadOf", "https://github.com/example-org/example-repo.git")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    git(repo, "push", "-q", "origin", "HEAD:main")
    for name, files in branches:
        git(repo, "checkout", "-q", "-B", name)
        for path, content in files.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(content)
            git(repo, "add", "--", path)
        git(repo, "commit", "-q", "-m", "add " + name.split("/")[-1])
        git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
        git(repo, "checkout", "-q", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "remote", "set-head", "origin", "main")
    return repo


def write_json(path, data):
    with open(path, "w") as handle:
        json.dump(data, handle)


def overlap_env(tmp, repo, prs, fixture):
    home = os.path.join(tmp, "home")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(home, exist_ok=True)
    prs_path = os.path.join(tmp, "prs.json")
    fixture_path = os.path.join(tmp, "fixture.json")
    write_json(prs_path, prs)
    write_json(fixture_path, fixture)
    write_exec(os.path.join(bindir, "gh"), FAKE_GH)
    write_exec(os.path.join(bindir, "orca"), FAKE_ORCA)
    env = os.environ.copy()
    env.update({
        "HOME": home,
        "TMPDIR": tmp,
        "PATH": bindir + os.pathsep + env["PATH"],
        "GH_PRS": prs_path,
        "GANTRY_ORCA_FIXTURE": fixture_path,
    })
    return env, prs_path, fixture_path


def run_overlap(repo, env, *args):
    proc = subprocess.run(
        [os.path.join(ROOT, "bin", "gantry"), "overlap", *args],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout)


def ids_of(data, kind):
    return [item["id"] for item in data["candidates"] if item["kind"] == kind]


def test_current_task_is_excluded():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        repo = seed_repo(tmp, [(WORD, WORD_FILES)])
        wt = os.path.join(tmp, "wt")
        git(repo, "worktree", "add", "-q", wt, WORD)
        worker = {
            "dispatchId": "ctx_word",
            "taskId": "task_word",
            "runId": "run_word",
            "dispatchStatus": "running",
            "workerState": "running",
            "terminalState": "active",
            "agentTerminalHandle": "term_word",
            "worktreePath": wt,
            "task_title": INTENT_WORD,
            "files": ["src/wordcount_7.py", "tests/test_wordcount_7.py"],
        }
        task = {"id": "task_word", "task_title": "wordcount", "status": "dispatched", "result": ""}
        fixture = {
            "task-list": {"tasks": [task], "page": {}},
            "worker-list": {"workers": [worker], "page": {}},
        }
        env, prs_path, fixture_path = overlap_env(tmp, repo, [], fixture)
        base = ["--intent", INTENT_WORD, "--files", FILES_WORD, "--run", "run_word", "--task", "task_word"]

        own_only = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert own_only["verdict"] == "continue", own_only
        assert WORD not in ids_of(own_only, "branch")
        assert "task_word" not in ids_of(own_only, "worker")
        assert all(item.get("branch") != WORD for item in own_only["candidates"])
        prev_tmp = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = tmp
        try:
            assert gantry_kit.cleared("run_word", "task_word", "pre-pr") is True
        finally:
            if prev_tmp is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = prev_tmp

        bare = run_overlap(repo, env, "--checkpoint", "pre-pr", "--intent", INTENT_WORD, "--files", FILES_WORD)
        branch = next(item for item in bare["candidates"] if item["kind"] == "branch" and item["id"] == WORD)
        assert branch["label"] == "related", branch
        assert branch["label"] == gantry_kit.classify(INTENT_WORD, WORD_FILES.keys(), branch)
        assert any(item["kind"] == "worker" and item["id"] == "task_word" and item["label"] == "duplicate" for item in bare["candidates"])
        assert bare["verdict"] == "stop"

        write_exec(os.path.join(tmp, "bin", "orca"), "#!/bin/sh\nexit 1\n")
        unknown = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert any(item["kind"] == "branch" and item["id"] == WORD and item["label"] == "related" for item in unknown["candidates"])
        assert unknown["verdict"] == "stop"
        write_exec(os.path.join(tmp, "bin", "orca"), FAKE_ORCA)

        write_json(prs_path, [
            pr_row(7, "Add a word count helper", WORD, list(WORD_FILES), "Helper and test."),
            pr_row(3, "Update readme", "docs-readme", ["README.md"], "Docs only."),
        ])
        own_pr = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert own_pr["verdict"] == "continue", own_pr
        assert "7" not in ids_of(own_pr, "pr")
        assert WORD not in ids_of(own_pr, "branch")
        assert ids_of(own_pr, "pr") == ["3"]
        assert any(item["id"] == "3" and item["label"] == "unrelated" for item in own_pr["candidates"])

        write_json(prs_path, [
            pr_row(8, INTENT_WORD, OTHER, ["src/wordcount_7.py"], "Same words, different task."),
        ])
        titled = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        titled_pr = next(item for item in titled["candidates"] if item["kind"] == "pr" and item["id"] == "8")
        assert titled_pr["label"] == "duplicate"
        assert titled_pr["label"] == gantry_kit.classify(INTENT_WORD, list(WORD_FILES), titled_pr)
        assert WORD not in ids_of(titled, "branch")
        assert "task_word" not in ids_of(titled, "worker")
        assert titled["verdict"] == "stop"

        write_json(prs_path, [])
        task["result"] = "Pushed the helper. task=task_word dispatch=ctx_word pr=none branch=" + WORD
        worker.pop("worktreePath")
        fixture["worker-list"]["workers"] = []
        write_json(fixture_path, fixture)
        recorded = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert recorded["verdict"] == "continue", recorded
        assert WORD not in ids_of(recorded, "branch")

        fixture["worker-list"]["workers"] = [{
            "dispatchId": "ctx_word",
            "taskId": "task_word",
            "dispatchStatus": "running",
            "dispatch": {"id": "ctx_word", "branch": WORD},
            "task_title": "unrelated title",
            "files": ["src/wordcount_7.py", "tests/test_wordcount_7.py"],
        }]
        task["result"] = ""
        write_json(fixture_path, fixture)
        named = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert named["verdict"] == "continue", named
        assert WORD not in ids_of(named, "branch")
        assert "task_word" not in ids_of(named, "worker")

        fixture["worker-list"]["workers"] = [{
            "dispatchId": "ctx_word",
            "taskId": "task_word",
            "dispatchStatus": "running",
            "resource": {"worktreeId": "repo-1::" + wt},
            "task_title": INTENT_WORD,
            "files": ["src/wordcount_7.py", "tests/test_wordcount_7.py"],
        }]
        write_json(fixture_path, fixture)
        via_id = run_overlap(repo, env, "--checkpoint", "pre-pr", *base)
        assert via_id["verdict"] == "continue", via_id
        assert WORD not in ids_of(via_id, "branch")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        repo = seed_repo(tmp, [(WORD, WORD_FILES), (OTHER, OTHER_FILES)])
        wt = os.path.join(tmp, "wt")
        other_wt = os.path.join(tmp, "wt-other")
        git(repo, "worktree", "add", "-q", wt, WORD)
        git(repo, "worktree", "add", "-q", other_wt, OTHER)
        fixture = {
            "task-list": {"tasks": [
                {"id": "task_word", "task_title": "wordcount", "status": "dispatched", "result": ""},
                {"id": "task_other", "task_title": INTENT_WORD, "status": "dispatched", "result": ""},
            ], "page": {}},
            "worker-list": {"workers": [
                {
                    "dispatchId": "ctx_word",
                    "taskId": "task_word",
                    "dispatchStatus": "running",
                    "worktreePath": wt,
                    "task_title": INTENT_WORD,
                    "files": list(WORD_FILES),
                },
                {
                    "dispatchId": "ctx_other",
                    "taskId": "task_other",
                    "dispatchStatus": "running",
                    "worktreePath": other_wt,
                    "task_title": INTENT_WORD,
                    "files": ["src/wordcount_7.py"],
                },
            ], "page": {}},
        }
        env, _prs_path, _fixture_path = overlap_env(tmp, repo, [], fixture)
        mixed = run_overlap(
            repo, env, "--checkpoint", "pre-pr", "--intent", INTENT_WORD, "--files", FILES_WORD,
            "--run", "run_word", "--task", "task_word",
        )
        assert WORD not in ids_of(mixed, "branch")
        assert "task_word" not in ids_of(mixed, "worker")
        foreign = next(item for item in mixed["candidates"] if item["kind"] == "branch" and item["id"] == OTHER)
        assert foreign["label"] == "related", foreign
        assert foreign["label"] == gantry_kit.classify(INTENT_WORD, list(WORD_FILES), foreign)
        other_worker = next(item for item in mixed["candidates"] if item["kind"] == "worker" and item["id"] == "task_other")
        assert other_worker["label"] == "duplicate"
        assert mixed["verdict"] == "stop"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        repo = seed_repo(tmp, [(OTHER, OTHER_FILES)])
        fixture = {
            "task-list": {"tasks": [
                {"id": "task_word", "task_title": "wordcount", "status": "ready", "result": ""},
            ], "page": {}},
            "worker-list": {"workers": [], "page": {}},
        }
        env, _prs_path, _fixture_path = overlap_env(tmp, repo, [], fixture)
        scoped = run_overlap(
            repo, env, "--checkpoint", "scoped", "--intent", INTENT_WORD, "--files", FILES_WORD,
            "--run", "run_word", "--task", "task_word",
        )
        foreign = next(item for item in scoped["candidates"] if item["kind"] == "branch" and item["id"] == OTHER)
        assert foreign["label"] == "related", foreign
        assert scoped["verdict"] == "stop"


if __name__ == "__main__":
    test_rank_classify_and_log()
    test_gate_and_mutation()
    test_auto_record_and_answered_gate()
    test_current_task_is_excluded()
    print("4/4 passed")
