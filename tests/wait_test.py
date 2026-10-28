#!/usr/bin/env python3
"""gantry wait. orca is faked. Heartbeats and mailbox lines are scripted."""
import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
import gantry_kit as kit

WALL = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
STALE = "2026-10-28T11:00:00Z"
FRESH = "2026-10-28T12:00:00Z"
FORBIDDEN = ("worker-stop", "worker-start", "worker-abandon", "worker-release")


def message(kind, mid, dispatch, task, body="", **payload):
    body_payload = {"taskId": task, "dispatchId": dispatch}
    body_payload.update(payload)
    return {
        "id": mid,
        "type": kind,
        "subject": kind,
        "body": body,
        "payload": body_payload,
    }


class World:
    def __init__(self, tmp):
        self.tmp = tmp
        self.mono = 0.0
        self.wall = WALL
        self.workers = []
        self.shows = {}
        self.script = []
        self.trace = []
        self.run_id = "run_1"

    def worker(self, dispatch, task, status="dispatched"):
        self.workers.append({
            "dispatchId": dispatch,
            "taskId": task,
            "dispatchStatus": status,
            "workerState": "running",
        })
        self.shows.setdefault(dispatch, {"id": dispatch, "status": status, "taskId": task})

    def at(self, when, kind, payload):
        self.script.append((when, kind, payload))
        self.script.sort(key=lambda item: item[0])

    def apply(self):
        while self.script and self.script[0][0] <= self.mono:
            _at, kind, payload = self.script.pop(0)
            if kind == "journal":
                self.append(payload)
            elif kind == "show":
                current = dict(self.shows.get(payload["id"]) or {})
                current.update(payload)
                self.shows[payload["id"]] = current

    def append(self, row):
        directory = os.path.join(self.tmp, "gantry-mailbox", self.run_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "journal.jsonl"), "a") as handle:
            handle.write(json.dumps(row) + "\n")

    def pause(self, seconds):
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)
        self.apply()

    def invoke(self, argv, cwd=None, timeout=30):
        self.trace.append(list(argv))
        if argv and argv[0] == "orca":
            return self.orca(argv)
        return 1, "", "unexpected " + " ".join(argv)

    def orca(self, argv):
        if argv[1:3] == ["orchestration", "run-current"]:
            return 0, json.dumps({"ok": True, "result": {"run": {"id": self.run_id}}}), ""
        if argv[1:3] == ["orchestration", "worker-list"]:
            return 0, json.dumps({"ok": True, "result": {"workers": self.workers, "page": {}}}), ""
        if argv[1:3] == ["orchestration", "worker-show"]:
            dispatch = argv[argv.index("--dispatch") + 1]
            shown = dict(self.shows.get(dispatch) or {"id": dispatch, "status": "dispatched"})
            return 0, json.dumps({"ok": True, "result": {"dispatch": shown}}), ""
        if argv[1:3] == ["orchestration", "worker-read"]:
            return 0, json.dumps({"ok": True, "result": {"text": "preview that must not leak"}}), ""
        return 1, json.dumps({"ok": False, "error": "unexpected orca"}), ""

    def run(self, argv):
        self.apply()
        saved = (kit.invoke, kit.pause, kit.monotonic_now, kit.wall_now, kit.start_mailbox, kit.WAIT_POLL_S)
        saved_tmp = os.environ.get("TMPDIR")
        kit.invoke = self.invoke
        kit.pause = self.pause
        kit.monotonic_now = lambda: self.mono
        kit.wall_now = lambda: self.wall
        kit.start_mailbox = lambda run: None
        os.environ["TMPDIR"] = self.tmp
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = kit.dispatch(["wait", *argv])
        finally:
            kit.invoke, kit.pause, kit.monotonic_now, kit.wall_now, kit.start_mailbox, kit.WAIT_POLL_S = saved
            if saved_tmp is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved_tmp
        raw = [line for line in out.getvalue().splitlines() if line.strip()]
        lines = [json.loads(line) for line in raw]
        return code, lines, raw, err.getvalue()


def world():
    tmp = tempfile.TemporaryDirectory()
    return tmp, World(tmp.name)


def calls(fx, name):
    return [argv for argv in fx.trace if name in argv]


def assert_left_running(fx):
    blob = " ".join(" ".join(argv) for argv in fx.trace)
    for name in FORBIDDEN:
        assert name not in blob, blob


def test_heartbeats_then_a_message():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 1
        fx.worker("ctx_a", "task_a")
        fx.at(1, "journal", message("heartbeat", "msg_h", "ctx_a", "task_a", phase="implementing"))
        fx.at(1, "show", {"id": "ctx_a", "lastHeartbeatAt": FRESH, "status": "dispatched", "taskId": "task_a"})
        fx.at(2, "journal", message(
            "worker_done", "msg_done", "ctx_a", "task_a",
            "task=task_a dispatch=ctx_a pr=https://github.com/example-org/example-repo/pull/9 branch=feat/slug evidence=ok",
            outcome="succeeded",
        ))
        code, lines, raw, err = fx.run(["--run", "run_1"])
        assert err == "", err
        assert code == 0, lines
        assert len(lines) == 1, lines
        assert raw[0] == json.dumps(lines[0], separators=(",", ":"))
        assert lines[0]["status"] == "worker_done"
        assert lines[0]["id"] == "msg_done"
        assert lines[0]["task"] == "task_a"
        assert lines[0]["dispatch"] == "ctx_a"
        assert lines[0]["outcome"] == "succeeded"
        assert lines[0]["pr"].endswith("/pull/9")
        assert fx.mono == 2, fx.mono
        assert_left_running(fx)
        assert calls(fx, "worker-read") == []
    finally:
        tmp.cleanup()


def test_missing_heartbeat_is_a_failure():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 60
        fx.worker("ctx_a", "task_slug", status="ready")
        fx.shows["ctx_a"] = {"id": "ctx_a", "status": "ready", "taskId": "task_slug"}
        code, lines, raw, err = fx.run(["--run", "run_1", "--dispatch", "ctx_a"])
        assert err == "", err
        assert code == 0, lines
        assert len(lines) == 1, lines
        assert lines[0]["status"] == "failure"
        assert lines[0]["reason"] == "no heartbeat in 180s"
        assert lines[0]["task"] == "task_slug"
        assert lines[0]["dispatch"] == "ctx_a"
        assert lines[0]["action"] == "left running"
        assert "preview" not in raw[0]
        assert fx.mono == 180, fx.mono
        reads = calls(fx, "worker-read")
        assert len(reads) == 1, reads
        assert "--limit" in reads[0] and "8" in reads[0]
        assert_left_running(fx)
        code, lines, _raw, err = fx.run(["--run", "run_1", "--dispatch", "ctx_a"])
        assert err == "" and code == 0, lines
        assert lines[0]["status"] == "timeout"
        assert lines[0]["reason"] == "no message"
        assert len(calls(fx, "worker-read")) == 1
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_stall_after_heartbeat_is_a_failure():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 1
        fx.worker("ctx_b", "task_b")
        fx.shows["ctx_b"] = {
            "id": "ctx_b",
            "status": "dispatched",
            "taskId": "task_b",
            "lastHeartbeatAt": STALE,
        }
        code, lines, raw, err = fx.run(["--run", "run_1"])
        assert err == "", err
        assert code == 0, lines
        assert len(lines) == 1 and lines[0]["status"] == "failure", lines
        assert lines[0]["reason"] == "stalled after heartbeat"
        assert lines[0]["dispatch"] == "ctx_b"
        assert lines[0]["task"] == "task_b"
        assert lines[0]["action"] == "left running"
        assert "preview" not in raw[0]
        assert fx.mono == 0, fx.mono
        assert calls(fx, "worker-read") == []
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_question_before_the_heartbeat():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 60
        fx.worker("ctx_a", "task_a", status="ready")
        fx.shows["ctx_a"] = {"id": "ctx_a", "status": "ready", "taskId": "task_a"}
        fx.at(0, "journal", message(
            "question", "msg_q", "ctx_a", "task_a", "Use the short name?",
            options=["short", "long"],
        ))
        code, lines, raw, err = fx.run(["--run", "run_1", "--timeout-ms", "200000"])
        assert err == "", err
        assert code == 0, lines
        assert len(lines) == 1, lines
        assert lines[0]["status"] == "question"
        assert lines[0]["id"] == "msg_q"
        assert lines[0]["body"] == "Use the short name?"
        assert json.loads(lines[0]["options"]) == ["short", "long"]
        assert fx.mono == 0, fx.mono
        assert fx.trace == [], fx.trace
        assert "preview" not in raw[0]
    finally:
        tmp.cleanup()


def test_pre_pr_before_the_heartbeat():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 30
        fx.worker("ctx_a", "task_a", status="ready")
        fx.at(0, "journal", message(
            "pre-pr", "msg_pr", "ctx_a", "task_a", "ready to open",
            intent="add a helper", files=["src/app.py", "tests/test_app.py"],
        ))
        code, lines, _raw, err = fx.run(["--run", "run_1"])
        assert err == "" and code == 0, lines
        assert lines[0]["status"] == "pre-pr"
        assert lines[0]["intent"] == "add a helper"
        assert lines[0]["files"] == "src/app.py,tests/test_app.py"
        assert lines[0]["dispatch"] == "ctx_a"
        assert fx.mono == 0
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_open_question_is_not_a_stall():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 1
        fx.worker("ctx_a", "task_a")
        fx.shows["ctx_a"]["lastHeartbeatAt"] = STALE
        fx.append(message("question", "msg_q", "ctx_a", "task_a", "Which name?"))
        cursor = os.path.join(fx.tmp, "gantry-mailbox", "run_1", "cursor")
        os.makedirs(os.path.dirname(cursor), exist_ok=True)
        with open(cursor, "w") as handle:
            handle.write("1")
        code, lines, _raw, err = fx.run(["--run", "run_1", "--timeout-ms", "1000"])
        assert err == "" and code == 0, lines
        assert lines[0]["status"] == "timeout", lines
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_multiple_workers():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 1
        fx.worker("ctx_a", "task_a")
        fx.worker("ctx_b", "task_b")
        for dispatch, task in (("ctx_a", "task_a"), ("ctx_b", "task_b")):
            fx.at(1, "journal", message("heartbeat", "msg_" + dispatch, dispatch, task))
            fx.at(1, "show", {"id": dispatch, "lastHeartbeatAt": FRESH, "status": "dispatched", "taskId": task})
        fx.at(2, "journal", message(
            "worker_done", "msg_done", "ctx_a", "task_a",
            "task=task_a dispatch=ctx_a pr=none branch=feat/a evidence=ok",
            outcome="succeeded",
        ))
        code, lines, _raw, err = fx.run(["--run", "run_1"])
        assert err == "" and code == 0, lines
        assert len(lines) == 1 and lines[0]["status"] == "worker_done", lines
        assert lines[0]["dispatch"] == "ctx_a"
        assert fx.mono == 2, fx.mono
        shown = [argv[argv.index("--dispatch") + 1] for argv in calls(fx, "worker-show")]
        assert "ctx_a" in shown and "ctx_b" in shown, shown
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_one_missing_heartbeat_among_workers():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 60
        fx.worker("ctx_a", "task_a")
        fx.worker("ctx_b", "task_b", status="ready")
        fx.at(1, "journal", message("heartbeat", "msg_h", "ctx_a", "task_a"))
        fx.at(1, "show", {"id": "ctx_a", "lastHeartbeatAt": FRESH, "status": "dispatched", "taskId": "task_a"})
        code, lines, _raw, err = fx.run(["--run", "run_1"])
        assert err == "" and code == 0, lines
        assert lines[0]["status"] == "failure", lines
        assert lines[0]["dispatch"] == "ctx_b"
        assert lines[0]["task"] == "task_b"
        assert lines[0]["reason"] == "no heartbeat in 180s"
        assert lines[0]["action"] == "left running"
        reads = calls(fx, "worker-read")
        assert len(reads) == 1 and "ctx_b" in reads[0], reads
        assert_left_running(fx)
    finally:
        tmp.cleanup()


def test_timeout_cap_and_unknown_flag():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            kit.dispatch(["wait", "--timeout-ms", "200001"])
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("long timeout was accepted")
    assert "200000" in err.getvalue()
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        try:
            kit.dispatch(["wait", "--force"])
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("force was accepted")
    assert "gantry wait accepts only" in err.getvalue()


def test_escalation_returns():
    tmp, fx = world()
    try:
        kit.WAIT_POLL_S = 30
        fx.worker("ctx_a", "task_a")
        fx.at(0, "journal", message("escalation", "msg_e", "ctx_a", "task_a", "plan contradicts the charter"))
        code, lines, _raw, err = fx.run(["--run", "run_1"])
        assert err == "" and code == 0, lines
        assert lines[0]["status"] == "escalation"
        assert lines[0]["id"] == "msg_e"
        assert fx.mono == 0
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    test_heartbeats_then_a_message()
    test_missing_heartbeat_is_a_failure()
    test_stall_after_heartbeat_is_a_failure()
    test_question_before_the_heartbeat()
    test_pre_pr_before_the_heartbeat()
    test_open_question_is_not_a_stall()
    test_multiple_workers()
    test_one_missing_heartbeat_among_workers()
    test_timeout_cap_and_unknown_flag()
    test_escalation_returns()
    print("10/10 passed")
