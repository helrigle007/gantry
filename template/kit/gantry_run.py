#!/usr/bin/env python3
"""Run summary, mailbox projection, and the coordinator context hooks.

Orca is the source of truth. This writes a state file and a short handoff
derived from `orca orchestration` plus the mailbox journal. The coordinator
does not write either file.

  gantry_run.py              hook or status line, JSON on stdin
  gantry_run.py write        refresh state for the bound Run and print the handoff

Defaults live in gantry.json in the git common dir under contextReset:
mode "compact" and thresholdTokens 300000. A missing or invalid value uses those.
"""

import sys
sys.dont_write_bytecode = True

import json
import os
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gantry_kit as kit

SETTLED = {"completed", "failed"}
RESOLVED_GATES = {"resolved", "cancelled", "closed", "answered"}
QUIET_MESSAGES = {"heartbeat", "status"}
HANDOFF_CAP = 9000
DEFAULT_MODE = "compact"
DEFAULT_THRESHOLD = 300000
AUTOCOMPACT_MIN = 100000
AUTOCOMPACT_MAX = 1000000
SESSION_CONTEXT = {
    "resume": "handoff",
    "compact": "handoff",
    "fork": "handoff",
    "startup": "charter",
    "clear": "charter",
}


def state_dir(run):
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox", run)
    os.makedirs(path, exist_ok=True)
    return path


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def project_message(message):
    payload = message.get("payload")
    if isinstance(payload, str) and payload:
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    body = message.get("body") or ""
    if not isinstance(body, str):
        body = str(body)
    body = " ".join(body.split())
    projected = {
        "gantry_mailbox": "msg",
        "id": message.get("id") or "",
        "type": message.get("type") or "",
        "task": payload.get("taskId") or payload.get("task") or "",
        "dispatch": payload.get("dispatchId") or payload.get("dispatch") or "",
        "subject": message.get("subject") or "",
        "body": body,
    }
    phase = payload.get("phase") or ""
    if phase:
        projected["phase"] = phase
    outcome = payload.get("outcome") or ""
    if outcome:
        projected["outcome"] = outcome
    options = payload.get("options")
    if options:
        projected["options"] = options if isinstance(options, str) else json.dumps(options, separators=(",", ":"))
    return {key: value for key, value in projected.items() if value != "" or key in ("id", "type", "gantry_mailbox")}


def project_pr(result_text):
    pr = branch = outcome = ""
    if not result_text:
        return pr, branch, outcome
    data = result_text
    if isinstance(result_text, str):
        try:
            data = json.loads(result_text)
        except ValueError:
            data = result_text
    blob = data if isinstance(data, str) else json.dumps(data)
    if isinstance(data, dict):
        outcome = data.get("outcome") or ""
        blob = data.get("body") or blob
    for token in str(blob).replace(",", " ").split():
        if token.startswith("pr=") and token != "pr=none":
            pr = token[3:]
        elif token.startswith("branch=") and token != "branch=none":
            branch = token[7:]
    return pr, branch, outcome


def project_task(task):
    deps = task.get("deps") or []
    if isinstance(deps, str):
        try:
            deps = json.loads(deps)
        except ValueError:
            deps = []
    pr, branch, outcome = project_pr(task.get("result"))
    return {
        "id": task.get("id") or "",
        "title": task.get("task_title") or task.get("display_name") or "",
        "status": task.get("status") or "",
        "deps": [dep for dep in deps if isinstance(dep, str)],
        "pr": pr,
        "branch": branch,
        "outcome": outcome,
    }


def project_worker(worker):
    heartbeat = ""

    def walk(node):
        nonlocal heartbeat
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "lastHeartbeatAt" and value and not heartbeat:
                    heartbeat = value
                elif isinstance(value, dict):
                    walk(value)

    walk(worker)
    return {
        "dispatch": worker.get("dispatchId") or "",
        "task": worker.get("taskId") or "",
        "status": worker.get("dispatchStatus") or "",
        "state": worker.get("workerState") or "",
        "terminal": worker.get("terminalState") or "",
        "handle": worker.get("agentTerminalHandle") or "",
        "retained": worker.get("retainedReason") or "",
        "heartbeat": heartbeat,
    }


def project_gate(gate):
    return {
        "id": gate.get("id") or "",
        "task": gate.get("taskId") or gate.get("task_id") or gate.get("task") or "",
        "status": gate.get("status") or gate.get("state") or "pending",
        "question": " ".join(str(gate.get("question") or "").split()),
        "resolution": " ".join(str(gate.get("resolution") or "").split()),
    }


def _actionable(state, skip_titles):
    by_id = {task["id"]: task for task in state["tasks"]}
    active = {worker["task"] for worker in state["workers"] if worker["status"] not in SETTLED}
    ready = []
    for task in state["tasks"]:
        if task["status"] in SETTLED or task["id"] in active:
            continue
        if task["title"] in skip_titles:
            continue
        if all(by_id.get(dep, {}).get("status") == "completed" for dep in task["deps"]):
            ready.append(task)
    return ready


def ready_tasks(state):
    return _actionable(state, ("gantry-ship", "gantry-cleanup"))


def held_landing(state):
    titles = {task["id"]: task["title"] for task in state["tasks"]}
    for gate in state["gates"]:
        if titles.get(gate.get("task")) != "gantry-ship":
            continue
        if gate.get("status") not in RESOLVED_GATES:
            continue
        resolution = (gate.get("resolution") or "").lower()
        if resolution and "land" not in resolution:
            return gate
    return None


def apply_journal_prs(state):
    by_id = {task["id"]: task for task in state["tasks"]}
    for message in state["messages"]:
        if message.get("type") != "worker_done":
            continue
        pr, branch, outcome = project_pr(message.get("body") or "")
        task = by_id.get(message.get("task") or "")
        if not task:
            continue
        if pr and not task["pr"]:
            task["pr"] = pr
        if branch and not task["branch"]:
            task["branch"] = branch
        if outcome and not task["outcome"]:
            task["outcome"] = outcome


def next_step(state):
    gates = [gate for gate in state["gates"] if gate.get("status") not in RESOLVED_GATES]
    if gates:
        gate = gates[0]
        return (
            f"Open gate {gate['id']} on {gate['task']}: {gate['question']}. "
            "Keep one gantry wait running and do not start a duplicate worker."
        )
    held = held_landing(state)
    if held:
        return (
            f"Landing gate {held['id']} resolved {held['resolution']}. "
            "Report that and do not start the ship task."
        )
    active = [worker for worker in state["workers"] if worker["status"] not in SETTLED]
    if active:
        ids = ", ".join(worker["dispatch"] for worker in active)
        return f"Workers in flight: {ids}. Keep one gantry wait running. Do not start a duplicate."
    ready = ready_tasks(state)
    if ready:
        ids = ", ".join(task["id"] for task in ready)
        return f"Start ready tasks: {ids}."
    ships = _actionable(state, ("gantry-cleanup",))
    ships = [task for task in ships if task["title"] == "gantry-ship"]
    if ships:
        return f"Start the ship task: {ships[0]['id']}."
    titled = {task["title"] for task in state["tasks"]}
    settled_prs = [task for task in state["tasks"] if task["pr"] and task["status"] in SETTLED]
    if settled_prs and "gantry-ship" not in titled and not active:
        if not state.get("autoMerge"):
            return (
                "Every implementation task has settled and autoMerge is false. "
                "Dispatch the ship worker to babysit. Do not run gantry ship."
            )
        return (
            "Every implementation task has settled. "
            "Read gantry-ship.md and run gantry ship for each independent PR. "
            "A stacked PR still needs the ship worker."
        )
    if any(task["title"] == "gantry-ship" and task["status"] in SETTLED for task in state["tasks"]):
        if not state.get("autoMerge"):
            return "Ship worker has settled and autoMerge is false. Create the landing task and its gate."
        return "Ship worker has settled. Report what landed. Clean up with gantry cleanup when the user asks."
    return "No ready task and no worker in flight. Report status to the user."


def render_handoff(state):
    lines = [
        "gantry handoff. Orca is the source of truth. This file is a summary, regenerated from Orca and the mailbox journal.",
        f"run {state['run']}",
        f"objective {state.get('objective') or ''}",
        f"autoMerge {str(state.get('autoMerge')).lower()}",
        f"contextReset {state['mode']} thresholdTokens {state['threshold']}",
        "gates:",
    ]
    if not state["gates"]:
        lines.append("- none")
    for gate in state["gates"]:
        lines.append(
            f"- {gate['id']} task={gate['task']} status={gate['status']} "
            f"resolution={gate['resolution'] or 'none'} question={gate['question']}"
        )
    lines.append("workers:")
    for worker in state["workers"]:
        lines.append(
            f"- {worker['dispatch']} task={worker['task']} status={worker['status']} "
            f"state={worker['state']} terminal={worker['terminal']} handle={worker['handle'] or 'none'} "
            f"retained={worker['retained'] or 'none'} heartbeat={worker['heartbeat'] or 'none'}"
        )
    lines.append("tasks:")
    for task in state["tasks"]:
        deps = ",".join(task["deps"]) or "none"
        lines.append(
            f"- {task['id']} status={task['status']} title={task['title']} deps={deps} "
            f"pr={task['pr'] or 'none'} branch={task['branch'] or 'none'} outcome={task['outcome'] or 'none'}"
        )
    lines.append("recent:")
    if not state["messages"]:
        lines.append("- none")
    for message in state["messages"]:
        lines.append(
            f"- {message.get('type')} id={message.get('id')} task={message.get('task', '')} "
            f"dispatch={message.get('dispatch', '')} subject={message.get('subject', '')} "
            f"body={message.get('body', '')[:240]}"
        )
    body = "\n".join(lines)
    nxt = "Next: " + state["next"]
    room = HANDOFF_CAP - len(nxt) - 2
    if room < 0:
        return nxt[:HANDOFF_CAP].rstrip() + "\n"
    charter = []
    for line in state["charter"]:
        if len(line) + 1 > room:
            break
        charter.append(line)
        room -= len(line) + 1
    if len(body) > room:
        body = body[:room].rstrip()
    return "\n".join([body, *charter, nxt]) + "\n"


def autocompact_tokens(config):
    if config.get("mode") != "compact":
        return None
    return min(AUTOCOMPACT_MAX, max(AUTOCOMPACT_MIN, int(config["threshold"])))


def orca(*args):
    proc = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True, timeout=25)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = {}
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or proc.stderr.strip() or f"exit {proc.returncode}")[:400])
    return data.get("result") or {}


def load_config(cwd):
    data = {}
    governed = kit.governing(os.environ.get("CLAUDE_PROJECT_DIR"), cwd or os.getcwd())
    if governed is not None and isinstance(governed.config, dict):
        data = governed.config
    reset = data.get("contextReset") or {}
    mode = reset.get("mode") or DEFAULT_MODE
    if mode not in ("compact", "restart"):
        mode = DEFAULT_MODE
    try:
        threshold = int(reset.get("thresholdTokens") or DEFAULT_THRESHOLD)
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    return {
        "mode": mode,
        "threshold": threshold,
        "autoMerge": data.get("autoMerge", True),
    }


def journal_messages(run):
    path = os.path.join(state_dir(run), "journal.jsonl")
    rows = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    interesting = [row for row in rows if row.get("type") not in QUIET_MESSAGES]
    return [project_message(row) for row in interesting[-12:]]


def collect_pages(run, sub, key):
    rows = []
    cursor = None
    while True:
        args = ["orchestration", sub, "--run", run]
        if cursor:
            args.extend(["--cursor", cursor])
        result = orca(*args)
        rows.extend(result.get(key) or [])
        cursor = (result.get("page") or {}).get("nextCursor")
        if not cursor:
            return rows, result


def build_state(cwd):
    config = load_config(cwd)
    current = orca("orchestration", "run-current")
    run_obj = current.get("run") or {}
    run = run_obj.get("id") if isinstance(run_obj, dict) else None
    if not run:
        raise RuntimeError("this terminal has no bound Run")
    tasks, _task_page = collect_pages(run, "task-list", "tasks")
    workers, _worker_page = collect_pages(run, "worker-list", "workers")
    gates_result = orca("orchestration", "gate-list", "--run", run)
    state = {
        "run": run,
        "objective": run_obj.get("objective") or "",
        "autoMerge": config["autoMerge"],
        "mode": config["mode"],
        "threshold": config["threshold"],
        "tasks": [project_task(task) for task in tasks],
        "workers": [project_worker(worker) for worker in workers],
        "gates": [project_gate(gate) for gate in (gates_result.get("gates") or [])],
        "messages": journal_messages(run),
    }
    apply_journal_prs(state)
    state["next"] = next_step(state)
    state["overlap"] = overlap_events(run)
    state["charter"] = kit.charter_block(os.environ.get("CLAUDE_PROJECT_DIR"), cwd)
    return state


def overlap_events(run):
    path = os.path.join(state_dir(run), "overlap.jsonl")
    rows = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return rows


def refresh(cwd):
    state = build_state(cwd)
    directory = state_dir(state["run"])
    write_atomic(os.path.join(directory, "state.json"), json.dumps(state, indent=2) + "\n")
    handoff = render_handoff(state)
    write_atomic(os.path.join(directory, "handoff.md"), handoff)
    return state, handoff


def latest_context(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as handle:
        handle.seek(max(0, size - 400_000))
        tail = handle.read().decode("utf-8", "ignore")
    last = None
    for line in tail.splitlines():
        if "cache_read_input_tokens" not in line and "input_tokens" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        usage = ((rec.get("message") or {}).get("usage") or {}) if isinstance(rec, dict) else {}
        if "input_tokens" not in usage:
            continue
        ctx = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        if ctx:
            last = ctx
    return last


def context_from_status(data):
    window = data.get("context_window") or {}
    total = window.get("total_input_tokens")
    if isinstance(total, int) and total:
        return total
    usage = window.get("current_usage") or {}
    if not usage:
        return None
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    ) or None


def flag_path(session_id):
    directory = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-context")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, session_id or "unknown")


def emit_status(data):
    config = load_config(data.get("cwd") or "")
    tokens = context_from_status(data)
    if not tokens:
        print("gantry …")
        return
    mark = " RESTART" if tokens >= config["threshold"] and config["mode"] == "restart" else ""
    if tokens >= config["threshold"] and config["mode"] == "compact":
        mark = " COMPACT"
    print(f"gantry {tokens // 1000}k/{config['threshold'] // 1000}k {config['mode']}{mark}")


def for_coordinator(data):
    agent = data.get("agent_type") or ""
    if agent:
        return agent == "gantry-coordinator"
    return os.environ.get("GANTRY_COORDINATOR") == "1"


def latest_handoff():
    root = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox")
    newest = ""
    newest_mtime = -1
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return ""
    for name in names:
        path = os.path.join(root, name, "handoff.md")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= newest_mtime:
            newest_mtime = mtime
            newest = path
    if not newest:
        return ""
    with open(newest) as handle:
        return "stale handoff, Orca refresh failed.\n" + handle.read()


def emit_threshold(data):
    if not for_coordinator(data):
        return
    config = load_config(data.get("cwd") or "")
    if config["mode"] != "restart":
        return
    tokens = latest_context(data.get("transcript_path"))
    if not tokens:
        return
    path = flag_path(data.get("session_id"))
    if tokens < config["threshold"]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    if os.path.exists(path):
        return
    write_atomic(path, str(tokens))
    notice = (
        f"gantry context reset: {tokens} tokens is past thresholdTokens {config['threshold']} "
        "in .git/gantry.json. "
        "Mode is restart. Stop starting workers. Tell the operator to exit and run `gantry restart` "
        "from the main checkout. The new session reloads the handoff from Orca."
    )
    json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": notice}}, sys.stdout)
    sys.stdout.write("\n")


def session_context(source, restarted):
    if source == "startup" and restarted:
        return "handoff"
    return SESSION_CONTEXT.get(source)


def emit_session(data):
    kind = session_context(data.get("source") or "", os.environ.get("GANTRY_RESTART") == "1")
    if kind is None or not for_coordinator(data):
        return
    cwd = data.get("cwd") or ""
    text = ""
    if kind == "handoff":
        try:
            _state, text = refresh(cwd)
        except Exception:
            text = latest_handoff()
    if not text.strip():
        text = "\n".join(kit.charter_block(os.environ.get("CLAUDE_PROJECT_DIR"), cwd))
    if not text.strip():
        return
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text.strip()}},
        sys.stdout,
    )
    sys.stdout.write("\n")


def on_hook(data):
    try:
        governed = kit.governing(
            os.environ.get("CLAUDE_PROJECT_DIR"), data.get("cwd") or os.getcwd(), warn=True
        )
    except kit.RegistryError:
        kit.warn_registry()
        return 0
    if governed is None:
        return 0
    event = data.get("hook_event_name")
    if event == "PreCompact":
        try:
            refresh(data.get("cwd") or "")
        except Exception:
            return 0
        return 0
    if event == "SessionStart":
        emit_session(data)
        return 0
    if event == "UserPromptSubmit":
        emit_threshold(data)
        return 0
    if not event and "context_window" in data:
        emit_status(data)
        return 0
    return 0


def replay_shrink():
    message = {
        "id": "msg_7fa48b2cc850",
        "run_id": "run_238bfca86963",
        "from_handle": "term_21de7688-5a27-4064-85b9-66997ffb9037",
        "to_handle": "run:run_238bfca86963",
        "subject": "alive",
        "body": "",
        "type": "heartbeat",
        "priority": "normal",
        "thread_id": None,
        "payload": json.dumps({"taskId": "task_a50c2e7d46e7", "dispatchId": "ctx_21fc8b50d75e", "phase": "investigating"}),
        "read": 0,
        "sequence": 1,
        "created_at": "2026-10-15T17:54:30Z",
        "delivered_at": None,
        "sender_pane_key": "ad98544a-a906-4e14-a631-0c57f6779f2f:1fa59877-b017-493c-a302-c2a5c9282d23",
        "delivery_contract": "current_delivery",
    }
    worker = {
        "dispatchId": "ctx_8bbc849e55b1",
        "taskId": "task_b7432f0c1fd7",
        "runId": "run_238bfca86963",
        "workerState": "succeeded",
        "dispatchStatus": "completed",
        "agentTerminalHandle": "term_" + "a" * 32,
        "terminalState": "released",
        "resource": {"worktreeId": "x" * 180, "endpointIncarnation": "y" * 180, "archive": {"source": "transcript", "status": "captured"}},
        "projection": {"workspace": {"id": "z" * 180}, "stage": {"detail": "w" * 800}},
        "dispatch": {"lastHeartbeatAt": "2026-10-19T15:30:00Z", "processIncarnation": "p" * 160},
    }
    show = {"ok": True, "result": {"dispatch": {"id": worker["dispatchId"], "status": "dispatched", "lastHeartbeatAt": "2026-10-15T18:07:16Z", "taskId": worker["taskId"], "hostScope": {"kind": "local"}, "assigneePaneKey": "k" * 80}, "worker": worker}}
    narrow_show = {"dispatch": show["result"]["dispatch"]["id"], "status": show["result"]["dispatch"]["status"], "heartbeat": show["result"]["dispatch"]["lastHeartbeatAt"], "task": show["result"]["dispatch"]["taskId"]}
    listing = {"ok": True, "result": {"workers": [worker, worker, worker]}}
    narrow_list = [project_worker(row) for row in listing["result"]["workers"]]
    pr = {"body": "x" * 6000, "title": "Copy image", "state": "OPEN", "number": 884, "mergeStateStatus": "CLEAN", "headRefName": "ship", "url": "https://github.com/example-org/example-repo/pull/884", "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}]}
    narrow_pr = {key: pr[key] for key in ("number", "title", "state", "mergeStateStatus", "headRefName", "url")}
    spec = "Playbook: feature\n" + ("gantry worker rules. " * 40)
    echo = {"ok": True, "result": {"task": {"id": "task_a", "task_title": "feature", "spec": spec, "status": "pending"}}}
    narrow_echo = {"task": "task_a", "title": "feature", "status": "pending"}

    def ratio(before, after):
        return len(json.dumps(after, separators=(",", ":"))) / len(json.dumps(before))

    return {
        "gantry-mailbox wait": ratio(message, project_message(message)),
        "worker-show": ratio(show, narrow_show),
        "worker-list": ratio(listing, narrow_list),
        "worker-read": 8 / 40,
        "gh pr view": ratio(pr, narrow_pr),
        "task echo": ratio(echo, narrow_echo),
        "Read skill/guide": "once",
        "orca skills get": "once",
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["write"]:
        _state, handoff = refresh(os.getcwd())
        sys.stdout.write(handoff)
        return 0
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        data = json.loads(raw)
    except ValueError:
        return 0
    return on_hook(data)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except Exception as exc:
        print(f"gantry_run: {exc}", file=sys.stderr)
        sys.exit(1 if sys.argv[1:2] == ["write"] else 0)
