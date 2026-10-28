#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "context_attribution.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import context_attribution as ca


def assistant(rid, ctx, content, out=10):
    return {
        "type": "assistant",
        "requestId": rid,
        "message": {
            "usage": {
                "input_tokens": ctx,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": out,
            },
            "content": content,
        },
    }


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def test_bash_families_do_not_double_count_orca():
    families = ca.bash_families("orca orchestration worker-read --dispatch ctx_1 --limit 40")
    assert families == ["worker-read"], families
    assert ca.bash_families("gantry wait --run run_1") == ["gantry wait"]
    assert ca.bash_families("~/.gantry/current/kit/gantry-mailbox wait --timeout-ms 180000") == ["gantry-mailbox wait"]
    assert ca.bash_families("orca status --json") == ["orca other"]
    assert "briefs/specs" in ca.bash_families("orca orchestration task-create --spec 'Playbook: feature'")


def test_growth_scales_to_usage_and_links_task_output():
    lines = [
        assistant("r1", 1000, [tool_use("t1", "Bash", {"command": "~/.gantry/current/kit/gantry-mailbox wait --timeout-ms 180000"})]),
        tool_result("t1", "Command running in background with ID: bab. Output is being written to: /tmp/tasks/bab.output"),
        {"type": "assistant", "requestId": "r2", "message": {
            "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5},
            "content": [tool_use("t2", "Read", {"file_path": "/tmp/tasks/bab.output"})],
        }},
        tool_result("t2", '{"type":"heartbeat","from_handle":"term_abcdefghijklmnopqrstuvwxyz","payload":"{\\"taskId\\":\\"task_1\\"}"}'),
        assistant("r3", 1600, [{"type": "text", "text": "next"}]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        with open(path, "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        result = ca.analyze(path)
    by = {row["family"]: row for row in result["families"]}
    assert result["positive_growth"] == 600, result["positive_growth"]
    assert result["compactions"] == 0
    assert by["gantry-mailbox wait"]["tokens"] > by.get("Read other", {}).get("tokens", 0)
    assert sum(row["tokens"] for row in result["families"]) == 600


def test_compaction_is_not_negative_attribution():
    lines = [
        assistant("r1", 50000, [{"type": "text", "text": "a" * 400}]),
        assistant("r2", 80000, [{"type": "text", "text": "b" * 400}]),
        assistant("r3", 10000, [{"type": "text", "text": "c"}]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        with open(path, "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        result = ca.analyze(path)
    assert result["compactions"] == 1
    assert result["positive_growth"] == 30000
    assert result["peak"] == 80000
    assert any(wave["end"] == 80000 and wave["added"] == 30000 for wave in result["waves"])


def test_task_echo_is_separate_from_the_spec_and_replay_shrinks_it():
    command = "orca orchestration task-create --spec 'Playbook: feature " + ("rule " * 80) + "'"
    echo = json.dumps({"ok": True, "result": {"task": {"id": "task_a", "spec": "x" * 4000, "status": "pending"}}})
    lines = [
        assistant("r1", 1000, [tool_use("t1", "Bash", {"command": command})]),
        tool_result("t1", echo),
        assistant("r2", 5000, [{"type": "text", "text": "next"}]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        with open(path, "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        measured = ca.analyze(path)
        sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
        from gantry_run import replay_shrink
        projected = ca.analyze(path, replay_shrink())
    measured_by = {row["family"]: row for row in measured["families"]}
    projected_by = {row["family"]: row for row in projected["families"]}
    assert measured_by["briefs/specs"]["tokens"] > 0
    assert measured_by["task echo"]["tokens"] > measured_by["briefs/specs"]["tokens"]
    assert projected["positive_growth"] < measured["positive_growth"]
    assert projected_by["task echo"]["tokens"] < measured_by["task echo"]["tokens"]
    assert projected["peak"] == measured["peak"]


def test_cli_json(tmp_path=None):
    lines = [assistant("r1", 100, [{"type": "text", "text": "hi"}]), assistant("r2", 140, [{"type": "text", "text": "yo"}])]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.jsonl")
        out = os.path.join(tmp, "out.json")
        with open(path, "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        proc = subprocess.run([sys.executable, SCRIPT, "--json", out, path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "assistant prose" in proc.stdout
        data = json.load(open(out))
        assert data[0]["positive_growth"] == 40


if __name__ == "__main__":
    test_bash_families_do_not_double_count_orca()
    test_growth_scales_to_usage_and_links_task_output()
    test_compaction_is_not_negative_attribution()
    test_task_echo_is_separate_from_the_spec_and_replay_shrinks_it()
    test_cli_json()
    print("5/5 passed")
