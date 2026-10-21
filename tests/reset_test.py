#!/usr/bin/env python3
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "template", "kit", "gantry_run.py")
LAUNCHER = os.path.join(ROOT, "bin", "gantry")
sys.path.insert(0, os.path.dirname(RUN))
import gantry_run as run

FAKE_ORCA = r"""#!/usr/bin/env python3
import json, os, sys
fixture = json.load(open(os.environ["GANTRY_ORCA_FIXTURE"]))
args = [arg for arg in sys.argv[1:] if arg != "--json"]
key = None
if args[:2] == ["orchestration", "run-current"]:
    key = "run-current"
elif args[:2] == ["orchestration", "task-list"]:
    key = "task-list"
elif args[:2] == ["orchestration", "worker-list"]:
    key = "worker-list"
elif args[:2] == ["orchestration", "gate-list"]:
    key = "gate-list"
if key is None:
    json.dump({"ok": False, "error": "unknown " + " ".join(args)}, sys.stdout)
    sys.exit(0)
json.dump({"ok": True, "result": fixture[key]}, sys.stdout)
"""

FAKE_CLAUDE = r"""#!/bin/sh
printf '%s\n' "$@" > "$CLAUDE_ARGV"
printf '%s' "$GANTRY_COORDINATOR_HANDLE" > "$CLAUDE_ARGV.handle"
"""


def write_exec(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def task(task_id, title, status, deps, result=""):
    return {
        "id": task_id,
        "task_title": title,
        "status": status,
        "deps": deps,
        "result": result,
    }


def worker(dispatch, task_id, status, heartbeat=""):
    row = {
        "dispatchId": dispatch,
        "taskId": task_id,
        "dispatchStatus": status,
        "workerState": "running" if status == "dispatched" else "succeeded",
        "terminalState": "attached" if status == "dispatched" else "released",
    }
    if heartbeat:
        row["dispatch"] = {"lastHeartbeatAt": heartbeat}
    return row


def fixture(tasks, workers, gates):
    return {
        "run-current": {"run": {"id": "run_fixture", "objective": "add slugify"}},
        "task-list": {"tasks": tasks, "page": {}},
        "worker-list": {"workers": workers, "page": {}},
        "gate-list": {"runId": "run_fixture", "gates": gates, "count": len(gates)},
    }


SETTLED = task(
    "task_a",
    "slugify",
    "completed",
    [],
    json.dumps({"body": "done pr=https://github.com/example-org/example-repo/pull/7 branch=feat/slug"}),
)
READY = task("task_b", "words", "pending", '["task_a"]')
CHARTER = [
    "gantry charter. Standing rules from the user. Follow every one, and copy the lines a task needs onto its brief's Charter line.",
    "every repo:",
    "- g-071c6d Never force-push a shared branch.",
    "https://github.com/example-org/example-repo:",
    "- r-ad322a Hey, always check the linked ticket",
]


def env_for(tmp, repo, fixture_path, mode="compact", threshold=300000):
    bindir = os.path.join(tmp, "bin")
    home = os.path.join(tmp, "home")
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(home, exist_ok=True)
    write_exec(os.path.join(bindir, "orca"), FAKE_ORCA)
    os.makedirs(repo, exist_ok=True)
    if not os.path.isdir(os.path.join(repo, ".git")):
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, capture_output=True)
    url = "https://github.com/example-org/example-repo.git"
    if subprocess.run(["git", "-C", repo, "config", "--get", "remote.origin.url"], capture_output=True).returncode != 0:
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", url], check=True, capture_output=True)
    common = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip()
    with open(os.path.join(common, "gantry.json"), "w") as handle:
        json.dump({"autoMerge": True, "contextReset": {"mode": mode, "thresholdTokens": threshold}}, handle)
    saved = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        import gantry_kit
        gantry_kit.register(gantry_kit.normalize_origin(url), repo)
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
    env = os.environ.copy()
    env["HOME"] = home
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["TMPDIR"] = tmp
    env["CLAUDE_PROJECT_DIR"] = repo
    env["GANTRY_ORCA_FIXTURE"] = fixture_path
    return env


def save_fixture(tmp, data):
    path = os.path.join(tmp, "fixture.json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path


def hook(repo, env, payload):
    return subprocess.run(
        [sys.executable, RUN],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=repo,
        env=env,
    )


def test_projection_keeps_what_the_coordinator_acts_on():
    projected = run.project_message({
        "id": "msg_1",
        "type": "worker_done",
        "subject": "done",
        "body": "three sentences\npr=https://github.com/example-org/example-repo/pull/7 branch=feat/slug",
        "from_handle": "term_secret",
        "sender_pane_key": "pane",
        "delivery_contract": "current_delivery",
        "payload": json.dumps({"taskId": "task_a", "dispatchId": "ctx_1", "phase": "done", "outcome": "failed", "options": ["land", "hold"]}),
    })
    assert projected["outcome"] == "failed"
    assert "land" in projected["options"]
    assert projected["id"] == "msg_1"
    assert projected["task"] == "task_a"
    assert projected["dispatch"] == "ctx_1"
    assert "pr=" in projected["body"]
    assert "from_handle" not in projected
    assert "pane" not in json.dumps(projected)
    assert run.autocompact_tokens({"mode": "compact", "threshold": 300000}) == 300000
    assert run.autocompact_tokens({"mode": "compact", "threshold": 50}) == 100000
    assert run.autocompact_tokens({"mode": "compact", "threshold": 5000000}) == 1000000
    assert run.autocompact_tokens({"mode": "restart", "threshold": 300000}) is None
    with tempfile.TemporaryDirectory() as tmp:
        saved_dir = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            missing = run.load_config(tmp)
        finally:
            os.chdir(cwd)
            if saved_dir is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = saved_dir
    assert missing["mode"] == "compact"
    assert missing["threshold"] == 300000
    assert run.autocompact_tokens(missing) == 300000


def test_reset_matches_the_unreset_next_step():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        path = save_fixture(tmp, fixture([SETTLED, READY], [], []))
        env = env_for(tmp, repo, path)
        saved = os.environ.copy()
        os.environ.update(env)
        try:
            state, handoff = run.refresh(repo)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        direct = state["next"]
        assert direct == "Start ready tasks: task_b.", direct
        state_path = os.path.join(tmp, "gantry-mailbox", "run_fixture", "state.json")
        with open(state_path, "w") as handle:
            handle.write("{}\n")
        payload = {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "sess",
            "transcript_path": "",
            "cwd": repo,
            "agent_type": "gantry-coordinator",
        }
        proc = hook(repo, env, payload)
        assert proc.returncode == 0, proc.stderr
        injected = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert f"Next: {direct}" in injected
        assert "run run_fixture" in injected
        rebuilt = json.load(open(state_path))
        assert run.next_step(rebuilt) == direct
        plain = hook(repo, env, {**payload, "source": "startup"})
        assert plain.stdout == ""
        restarted = hook(repo, {**env, "GANTRY_RESTART": "1", "GANTRY_COORDINATOR": "1"}, {**payload, "source": "startup", "agent_type": ""})
        assert f"Next: {direct}" in json.loads(restarted.stdout)["hookSpecificOutput"]["additionalContext"]
        worker = hook(repo, env, {**payload, "source": "compact", "agent_type": "claude"})
        assert worker.stdout == ""
        cleared = hook(repo, env, {**payload, "source": "clear"})
        assert cleared.stdout == ""
        journal = os.path.join(tmp, "gantry-mailbox", "run_fixture", "journal.jsonl")
        with open(journal, "w") as handle:
            handle.write(json.dumps({
                "id": "msg_9", "type": "question", "subject": "which name",
                "body": "slug or slugify", "payload": {"taskId": "task_b", "dispatchId": "ctx_9"},
            }) + "\n")
        os.environ.update(env)
        try:
            again, _handoff = run.refresh(repo)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        assert again["next"] == direct
        assert any(message["type"] == "question" for message in again["messages"])


def test_inflight_worker_and_open_gate_outrank_ready_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        live = worker("ctx_live", "task_b", "dispatched", "2026-10-21T00:00:00Z")
        path = save_fixture(tmp, fixture([SETTLED, READY], [live], []))
        env = env_for(tmp, repo, path)
        saved = os.environ.copy()
        os.environ.update(env)
        try:
            state, _handoff = run.refresh(repo)
            assert state["next"] == "Workers in flight: ctx_live. Keep one mailbox wait running. Do not start a duplicate."
            assert state["workers"][0]["heartbeat"] == "2026-10-21T00:00:00Z"
            path = save_fixture(tmp, fixture(
                [SETTLED, READY], [],
                [{"id": "gate_1", "taskId": "task_b", "status": "pending", "question": "Which name?"}],
            ))
            env["GANTRY_ORCA_FIXTURE"] = path
            os.environ["GANTRY_ORCA_FIXTURE"] = path
            gated, _handoff = run.refresh(repo)
            assert gated["next"].startswith("Open gate gate_1 on task_b")
            path = save_fixture(tmp, fixture([SETTLED], [], []))
            os.environ["GANTRY_ORCA_FIXTURE"] = path
            shipped, _handoff = run.refresh(repo)
            assert shipped["next"] == "Every implementation task has settled. Dispatch the ship worker."
            landing = task("task_land", "gantry-ship", "pending", [])
            path = save_fixture(tmp, fixture([SETTLED, landing], [], []))
            os.environ["GANTRY_ORCA_FIXTURE"] = path
            held, _handoff = run.refresh(repo)
            assert held["next"] == "Start the ship task: task_land."
            blocked = task("task_b", "words", "pending", ["task_failed"])
            failed = task("task_failed", "slugify", "failed", [])
            path = save_fixture(tmp, fixture([failed, blocked], [], []))
            os.environ["GANTRY_ORCA_FIXTURE"] = path
            waiting, _handoff = run.refresh(repo)
            assert "task_b" not in waiting["next"]
            bare = task("task_a", "slugify", "completed", [])
            path = save_fixture(tmp, fixture([bare], [], []))
            os.environ["GANTRY_ORCA_FIXTURE"] = path
            journal = os.path.join(tmp, "gantry-mailbox", "run_fixture", "journal.jsonl")
            os.makedirs(os.path.dirname(journal), exist_ok=True)
            with open(journal, "w") as handle:
                handle.write(json.dumps({
                    "id": "msg_done", "type": "worker_done", "subject": "done",
                    "body": "Finished the helper. Tests passed. Ready to ship. task=task_a dispatch=ctx_a pr=https://github.com/example-org/example-repo/pull/9 branch=feat/slug",
                    "payload": {"taskId": "task_a", "dispatchId": "ctx_a", "outcome": "succeeded"},
                }) + "\n")
            filled, _handoff = run.refresh(repo)
            assert filled["tasks"][0]["pr"] == "https://github.com/example-org/example-repo/pull/9"
            assert "Dispatch the ship worker." in filled["next"]
        finally:
            os.environ.clear()
            os.environ.update(saved)


def test_hooks_stay_quiet_until_restart_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        transcript = os.path.join(tmp, "session.jsonl")
        with open(transcript, "w") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "message": {"usage": {
                    "input_tokens": 5000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 1,
                }},
            }) + "\n")
        env = env_for(tmp, repo, os.path.join(tmp, "missing.json"), mode="compact", threshold=1000)
        broken = env.copy()
        broken["PATH"] = "/usr/bin"
        pre = hook(repo, broken, {"hook_event_name": "PreCompact", "trigger": "auto", "cwd": repo, "session_id": "s"})
        assert pre.returncode == 0 and pre.stdout == ""
        quiet = hook(repo, env, {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "status",
            "cwd": repo,
            "session_id": "s",
            "transcript_path": transcript,
            "agent_type": "gantry-coordinator",
        })
        assert quiet.returncode == 0 and quiet.stdout == ""
        status = hook(repo, env, {"cwd": repo, "context_window": {"total_input_tokens": 5000}})
        assert status.stdout.strip() == "gantry 5k/1k compact COMPACT"
        env = env_for(tmp, repo, os.path.join(tmp, "missing.json"), mode="restart", threshold=1000)
        first = hook(repo, env, {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "go",
            "cwd": repo,
            "session_id": "s",
            "transcript_path": transcript,
            "agent_type": "gantry-coordinator",
        })
        assert first.returncode == 0
        notice = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "gantry restart" in notice
        second = hook(repo, env, {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "go",
            "cwd": repo,
            "session_id": "s",
            "transcript_path": transcript,
            "agent_type": "gantry-coordinator",
        })
        assert second.stdout == ""
        restart_status = hook(repo, env, {"cwd": repo, "context_window": {"total_input_tokens": 5000}})
        assert restart_status.stdout.strip() == "gantry 5k/1k restart RESTART"


def git_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main", path], check=True, capture_output=True)


def launch(repo, env, *args):
    return subprocess.run([LAUNCHER, *args], cwd=repo, env=env, capture_output=True, text=True)


def test_launcher_compact_and_restart():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        git_repo(repo)
        argv_path = os.path.join(tmp, "claude-argv")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        write_exec(os.path.join(bindir, "claude"), FAKE_CLAUDE)
        path = save_fixture(tmp, fixture([SETTLED, READY], [], []))
        env = env_for(tmp, repo, path, mode="compact", threshold=300000)
        os.makedirs(os.path.join(env["HOME"], ".claude", "agents"))
        with open(os.path.join(env["HOME"], ".claude", "agents", "gantry-coordinator.md"), "w") as handle:
            handle.write("---\nname: gantry-coordinator\n---\n")
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        env["ORCA_TERMINAL_HANDLE"] = "term_coordinator"
        env["CLAUDE_ARGV"] = argv_path
        proc = launch(repo, env, "add slugify")
        assert proc.returncode == 0, proc.stderr
        args = open(argv_path).read().splitlines()
        assert args[0] == "add slugify"
        assert args[args.index("--autocompact") + 1] == "300000"
        assert args.index("add slugify") < args.index("--disallowedTools")
        denied = args[args.index("--disallowedTools") + 1]
        assert "Task" in denied.split(",") and "MultiEdit" not in denied
        assert open(argv_path + ".handle").read() == "term_coordinator"
        assert "--resume" not in args
        env = env_for(tmp, repo, path, mode="restart", threshold=300000)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        env["ORCA_TERMINAL_HANDLE"] = "term_coordinator"
        env["CLAUDE_ARGV"] = argv_path
        proc = launch(repo, env, "restart")
        assert proc.returncode == 0, proc.stderr
        args = open(argv_path).read().splitlines()
        assert "Do not create a new Run" in args[0]
        assert "--autocompact" not in args
        denied = args[args.index("--disallowedTools") + 1]
        assert "Task" in denied.split(",") and "MultiEdit" not in denied
        assert open(argv_path + ".handle").read() == "term_coordinator"
        assert "--resume" not in args
        handoff = os.path.join(tmp, "gantry-mailbox", "run_fixture", "handoff.md")
        assert "Next: Start ready tasks: task_b." in open(handoff).read()


def add_rules(repo, env):
    for args in (["--global", "Never force-push a shared branch."], ["Hey, always check the linked ticket"]):
        proc = launch(repo, env, "charter", "add", *args)
        assert proc.returncode == 0, proc.stderr


def session_start(source):
    return {
        "hook_event_name": "SessionStart",
        "source": source,
        "session_id": "sess",
        "transcript_path": "",
        "agent_type": "gantry-coordinator",
    }


def test_handoff_keeps_the_charter_past_the_cap():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        filler = [task(f"task_fill_{n:03d}", "filler " + "x" * 80, "completed", []) for n in range(200)]
        path = save_fixture(tmp, fixture([SETTLED, READY, *filler], [], []))
        env = env_for(tmp, repo, path)
        add_rules(repo, env)
        saved = os.environ.copy()
        os.environ.update(env)
        try:
            _state, handoff = run.refresh(repo)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        lines = handoff.splitlines()
        assert len(handoff) <= run.HANDOFF_CAP, len(handoff)
        assert "task_fill_199" not in handoff
        assert lines[-1] == "Next: Start ready tasks: task_b.", lines[-1]
        assert lines[-1 - len(CHARTER):-1] == CHARTER, lines[-8:]
        assert handoff.count(CHARTER[0]) == 1


def test_plain_startup_and_clear_inject_only_the_charter():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        path = save_fixture(tmp, fixture([SETTLED, READY], [], []))
        env = env_for(tmp, repo, path)
        add_rules(repo, env)
        stale = os.path.join(tmp, "gantry-mailbox", "run_other")
        os.makedirs(stale)
        with open(os.path.join(stale, "handoff.md"), "w") as handle:
            handle.write("gantry handoff.\nrun run_other\nNext: Start ready tasks: task_z.\n")
        for source in ("startup", "clear"):
            proc = hook(repo, env, {**session_start(source), "cwd": repo})
            assert proc.returncode == 0, proc.stderr
            injected = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            assert injected == "\n".join(CHARTER), injected
            assert "run run_fixture" not in injected
        worker = hook(repo, env, {**session_start("startup"), "cwd": repo, "agent_type": "claude"})
        assert worker.stdout == ""


def test_failed_compact_falls_back_to_the_charter():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        env = env_for(tmp, repo, os.path.join(tmp, "missing.json"))
        add_rules(repo, env)
        proc = hook(repo, env, {**session_start("compact"), "cwd": repo})
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] == "\n".join(CHARTER), proc.stdout


if __name__ == "__main__":
    test_projection_keeps_what_the_coordinator_acts_on()
    test_reset_matches_the_unreset_next_step()
    test_inflight_worker_and_open_gate_outrank_ready_tasks()
    test_hooks_stay_quiet_until_restart_threshold()
    test_launcher_compact_and_restart()
    test_handoff_keeps_the_charter_past_the_cap()
    test_plain_startup_and_clear_inject_only_the_charter()
    test_failed_compact_falls_back_to_the_charter()
    print("8/8 passed")
