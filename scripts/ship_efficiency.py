#!/usr/bin/env python3
"""Compare the ship and cleanup span in Claude Code session transcripts.

A span starts at PR-ready and ends at cleanup done. PR-ready is an executed
command: an implementation worker's `worker_done` whose body carries a pull
URL, or the coordinator's `worker-start --task-title gantry-ship`. Cleanup
done is an executed `git worktree remove`, `orca worktree rm`, or a command
that marks the task done (`task-update --status completed`, or a `worker_done`
with `--outcome succeeded` and no pull URL).

Matching uses argv, after quotes are removed from the split. Text inside a
spec, and the coordinator prompt, do not open or close a span. The prompt
names these commands.

For each closed span this prints:

  turns           coordinator assistant requests on the span
  tokens          context added on the coordinator span, from
                  context_attribution.py --replay
  workers         ship and cleanup worker-start commands on the span
  total           coordinator tokens plus replay growth of ship and cleanup
                  worker sessions, when those files are in the same run
  wall seconds    timestamp of PR-ready to timestamp of cleanup done

Implementation-worker files can supply the PR-ready line. Their tokens are
not added. Unclosed spans are listed and left out of the means. A `worker_done`
with `--outcome failed`, or a body that says "not landed", counts as a failure
whether or not the span closes.

A directory of jsonl files is one run. A subdirectory of jsonl files is
another run. Pass several runs as several directories. The file named on a
span is the coordinator session in that run. An implementation worker can
open the span; the line still names the coordinator.

Claude Code Agent-tool subagents are transcripts named agent-*.jsonl, or any
session whose records set isSidechain or agentId. They are not the
coordinator and they are not ship or cleanup workers. Their commands do not
open or close a span, and their tokens are not added.

A single-quoted spec may contain an apostrophe as '\\''. That is one shell
word. A semicolon or a pipe inside that word is not a command separator.

  python3 scripts/ship_efficiency.py --before <dir-or-jsonl>...
  python3 scripts/ship_efficiency.py --before <before>... --after <after>...
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import context_attribution as ca

PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")
TASK_BODY = re.compile(r"(?:^|[\s])task=([A-Za-z0-9_.:-]+)")
NOT_LANDED = re.compile(r"not landed", re.I)
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DONE_STATUS = {"completed", "done", "succeeded"}
COORD_TOKENS = {
    "worker-start", "task-create", "worker-release", "worker-list",
    "run-list", "gate-create", "gate-list", "worker-show", "worker-read",
}
END_KINDS = {"remove", "task_done"}


def split_segments(command):
    parts = []
    buf = []
    in_single = False
    in_double = False
    i = 0
    n = len(command or "")

    def flush():
        text = "".join(buf).strip()
        buf.clear()
        if text:
            parts.append(text)

    while i < n:
        c = command[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        # Outside quotes, '\'' is how a single-quoted word holds an apostrophe.
        # The backslash is not a separator, and the escaped character is not either.
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(command[i + 1])
            i += 2
            continue
        if c in ";\n":
            flush()
            i += 1
            continue
        if c == "|" and i + 1 < n and command[i + 1] == "|":
            flush()
            i += 2
            continue
        if c == "&" and i + 1 < n and command[i + 1] == "&":
            flush()
            i += 2
            continue
        if c == "|":
            flush()
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return parts


def argv_of(segment):
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return []
    while argv and ENV_ASSIGN.match(argv[0]):
        argv = argv[1:]
    return argv


def argvs(command):
    found = []
    for segment in split_segments(command):
        argv = argv_of(segment)
        if argv:
            found.append(argv)
    return found


def option(argv, name):
    values = []
    flag = f"--{name}"
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == flag and i + 1 < len(argv):
            values.append(argv[i + 1])
            i += 2
            continue
        if token.startswith(flag + "="):
            values.append(token.split("=", 1)[1])
        i += 1
    return values


def basename(token):
    return os.path.basename(token or "")


def git_rest(argv):
    if basename(argv[0]) != "git":
        return None, []
    args = argv[1:]
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token, args[i + 1:]
    return None, []


def is_worktree_remove(argv):
    sub, rest = git_rest(argv)
    if sub == "worktree" and rest[:1] == ["remove"]:
        return True
    return (
        len(argv) >= 3
        and basename(argv[0]) == "orca"
        and argv[1] == "worktree"
        and argv[2] == "rm"
    )


def is_terminal_close(argv):
    return (
        len(argv) >= 3
        and basename(argv[0]) == "orca"
        and argv[1] == "terminal"
        and argv[2] == "close"
    )


def is_gh(argv, action):
    if basename(argv[0]) != "gh":
        return False
    args = [token for token in argv[1:] if not token.startswith("-")]
    return args[:2] == ["pr", action]


def is_worker_done(argv):
    if "send" not in argv:
        return False
    return "worker_done" in option(argv, "type")


def body_of(argv):
    return "\n".join(option(argv, "body"))


def task_of(argv):
    for name in ("task-id", "task"):
        values = option(argv, name)
        if values:
            return values[-1]
    match = TASK_BODY.search(body_of(argv))
    if match and match.group(1) not in ("none",):
        return match.group(1)
    return None


def prs_of(text):
    return PR_URL.findall(text or "")


def is_mailbox(argv):
    return any(basename(token) == "gantry-mailbox" for token in argv)


def is_coordinator_argv(argv):
    if is_mailbox(argv):
        return True
    return any(token in COORD_TOKENS for token in argv)


def role_of(commands):
    merge = False
    checks = False
    remove = False
    close = False
    coordinator = False
    for command in commands:
        for argv in argvs(command):
            if is_coordinator_argv(argv):
                coordinator = True
            if is_gh(argv, "merge"):
                merge = True
            if is_gh(argv, "checks"):
                checks = True
            if is_worktree_remove(argv):
                remove = True
            if is_terminal_close(argv):
                close = True
    if coordinator:
        return "coordinator"
    if merge or checks:
        return "ship"
    if remove or close:
        return "cleanup"
    return "implementation"


def events_of(command, role):
    found = []
    for argv in argvs(command):
        if "worker-start" in argv:
            title = (option(argv, "task-title") or [None])[-1]
            spec = "\n".join(option(argv, "spec"))
            prs = prs_of(spec)
            task = (option(argv, "task") or [None])[-1]
            if title == "gantry-ship":
                found.append({"kind": "ship_dispatch", "prs": prs, "task": task})
            elif title == "gantry-cleanup":
                found.append({"kind": "cleanup_dispatch", "prs": prs, "task": task})
        if is_worktree_remove(argv):
            found.append({"kind": "remove", "prs": [], "task": None})
        if is_worker_done(argv):
            body = body_of(argv)
            prs = prs_of(body)
            outcome = (option(argv, "outcome") or [None])[-1]
            task = task_of(argv)
            if role == "implementation" and prs:
                found.append({"kind": "pr_ready", "prs": prs, "task": task})
            if outcome == "failed" or NOT_LANDED.search(body or ""):
                found.append({
                    "kind": "failure",
                    "prs": prs,
                    "task": task,
                    "pr": prs[0] if prs else None,
                })
            if outcome == "succeeded" and not prs:
                found.append({"kind": "task_done", "prs": [], "task": task})
        statuses = option(argv, "status") + option(argv, "state")
        if "task-update" in argv and any(status in DONE_STATUS for status in statuses):
            found.append({"kind": "task_done", "prs": [], "task": task_of(argv)})
    return found


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parts(record):
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        return [part for part in content if isinstance(part, dict)]
    return []


def bash_command(part):
    if part.get("type") != "tool_use" or part.get("name") != "Bash":
        return None
    raw = part.get("input")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("command"), str):
        return raw["command"]
    return None


def tool_result(part):
    if part.get("type") != "tool_result":
        return None, None
    return part.get("tool_use_id"), ca.text_of(part.get("content"))


def request_key(record, index):
    return record.get("requestId") or record.get("uuid") or f"event-{index}"


def load_records(path):
    records = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def commands_of(records):
    found = []
    for record in records:
        if record.get("type") != "assistant":
            continue
        for part in parts(record):
            command = bash_command(part)
            if command:
                found.append(command)
    return found


def task_from_result(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("task"), str) and data["task"]:
        return data["task"]
    return None


def is_subagent_session(path, records):
    """Claude Code Agent-tool sidechain. Not a gantry coordinator or worker."""
    if os.path.basename(path).startswith("agent-"):
        return True
    for record in records:
        if record.get("isSidechain") is True or record.get("agentId"):
            return True
    return False


def file_events(path):
    records = load_records(path)
    base = os.path.basename(path)
    if is_subagent_session(path, records):
        return {
            "path": path,
            "file": base,
            "role": "subagent",
            "subagent": True,
            "records": records,
            "events": [],
        }
    commands = commands_of(records)
    role = role_of(commands)
    events = []
    pending = {}
    base = os.path.basename(path)
    for index, record in enumerate(records):
        kind = record.get("type")
        stamp = parse_time(record.get("timestamp"))
        if kind == "assistant":
            request = request_key(record, index)
            for part in parts(record):
                command = bash_command(part)
                if not command:
                    continue
                for event in events_of(command, role):
                    event = dict(event)
                    event.update({
                        "file": base,
                        "path": path,
                        "role": role,
                        "time": stamp,
                        "index": index,
                        "request": request,
                    })
                    events.append(event)
                    if event["kind"] == "ship_dispatch" and part.get("id") and not event.get("task"):
                        pending[part["id"]] = event
        elif kind == "user":
            for part in parts(record):
                tool_id, text = tool_result(part)
                event = pending.get(tool_id)
                if event is None or event.get("task"):
                    continue
                task = task_from_result(text)
                if task:
                    event["task"] = task
    return {
        "path": path,
        "file": base,
        "role": role,
        "subagent": False,
        "records": records,
        "events": events,
    }


def span_open(task, pr, event, spans, open_spans):
    span = {
        "task": task or pr or f"ship-{len(spans) + 1}",
        "pr": pr,
        "file": event["file"],
        "start_time": event["time"],
        "end_time": None,
        "closed": False,
        "failed": False,
    }
    spans.append(span)
    open_spans.append(span)
    return span


def match_open(open_spans, task, pr):
    if pr:
        for span in open_spans:
            if span["pr"] == pr:
                return span
    if task:
        for span in open_spans:
            if span["task"] == task:
                return span
    return None


def known_pr(spans, pr):
    return any(span["pr"] == pr for span in spans)


def build_spans(sessions):
    events = []
    for session in sessions:
        events.extend(session["events"])
    events.sort(key=lambda event: (
        event["time"] is None,
        event["time"] or datetime.min,
        event["file"],
        event["index"],
    ))
    spans = []
    open_spans = []
    for event in events:
        kind = event["kind"]
        prs = event.get("prs") or []
        pr = prs[0] if prs else event.get("pr")
        task = event.get("task")
        if kind == "pr_ready":
            if known_pr(spans, pr) or match_open(open_spans, task, pr):
                continue
            span_open(task, pr, event, spans, open_spans)
        elif kind == "ship_dispatch":
            # A dispatch joins PRs that are already open. It opens a span only
            # for a PR that has none, or when it names no PR and nothing is open.
            unmatched = [item for item in prs if match_open(open_spans, None, item) is None]
            if unmatched:
                for item in unmatched:
                    span_open(task, item, event, spans, open_spans)
            elif not prs and not open_spans:
                span_open(task, None, event, spans, open_spans)
        elif kind == "failure":
            target = match_open(open_spans, task, pr)
            if target is None and open_spans:
                target = open_spans[0]
            if target is not None:
                target["failed"] = True
        elif kind in END_KINDS:
            target = match_open(open_spans, task, pr)
            if target is None and open_spans:
                target = open_spans[0]
            if target is None:
                continue
            target["closed"] = True
            target["end_time"] = event["time"]
            open_spans.remove(target)
    return spans, events


def replay_shrink():
    sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
    from gantry_run import replay_shrink as shrink
    return shrink()


def replay_growth(records, shrink):
    if not records:
        return 0
    handle = tempfile.NamedTemporaryFile("w", prefix="ship-span-", suffix=".jsonl", delete=False)
    try:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.close()
        return ca.analyze(handle.name, shrink)["positive_growth"]
    finally:
        os.remove(handle.name)


def in_window(records, start, end):
    if start is None or end is None:
        return []
    chosen = []
    for record in records:
        stamp = parse_time(record.get("timestamp"))
        if stamp is not None and start <= stamp <= end:
            chosen.append(record)
    return chosen


def turns_of(records):
    seen = []
    for index, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        key = request_key(record, index)
        if key not in seen:
            seen.append(key)
    return len(seen)


def count_workers(events, start, end):
    if start is None or end is None:
        return None
    total = 0
    for event in events:
        if event["kind"] not in ("ship_dispatch", "cleanup_dispatch"):
            continue
        if event["time"] is not None and start <= event["time"] <= end:
            total += 1
    return total


def measure(span, sessions, events, shrink):
    row = {
        "file": span["file"],
        "task": span["task"],
        "pr": span["pr"],
        "closed": span["closed"],
        "failed": span["failed"],
        "turns": None,
        "tokens": None,
        "workers": None,
        "total_tokens": None,
        "wall_seconds": None,
    }
    if not span["closed"]:
        return row
    start = span["start_time"]
    end = span["end_time"]
    coordinator = []
    for session in sessions:
        if session["role"] != "coordinator":
            continue
        coordinator.extend(in_window(session["records"], start, end))
    coordinator.sort(key=lambda record: parse_time(record.get("timestamp")) or datetime.min)
    row["turns"] = turns_of(coordinator)
    row["tokens"] = replay_growth(coordinator, shrink)
    row["workers"] = count_workers(events, start, end)
    extra = 0
    for session in sessions:
        if session["role"] not in ("ship", "cleanup"):
            continue
        extra += replay_growth(in_window(session["records"], start, end), shrink)
    row["total_tokens"] = row["tokens"] + extra
    if start and end:
        row["wall_seconds"] = (end - start).total_seconds()
    return row


def coordinator_file(span, sessions):
    """The coordinator session that owns this span, when the run has one."""
    coords = [session for session in sessions if session["role"] == "coordinator" and not session.get("subagent")]
    if not coords:
        return None
    if len(coords) == 1:
        return coords[0]["file"]
    start = span.get("start_time")
    end = span.get("end_time") or start

    def score(session):
        total = 0
        for event in session["events"]:
            stamp = event.get("time")
            if stamp is None or start is None:
                continue
            if end is not None and start <= stamp <= end:
                total += 3
            elif stamp >= start:
                total += 1
        return total

    best = max(coords, key=score)
    if score(best) == 0:
        return coords[0]["file"]
    return best["file"]


def rows_of(paths, shrink):
    sessions = [file_events(path) for path in paths]
    spans, events = build_spans(sessions)
    for span in spans:
        named = coordinator_file(span, sessions)
        if named:
            span["file"] = named
    return [measure(span, sessions, events, shrink) for span in spans]


def file_has_end(path):
    session = file_events(path)
    return any(event["kind"] in END_KINDS for event in session["events"])


def session_meta(path):
    """First stamped record, cwd, session id, and whether this file is a subagent.

    Claude Code writes records with no timestamp (last-prompt, mode, and the
    like) before the first stamped line. The session start is that first stamp.
    """
    started = None
    cwd = None
    session = None
    subagent = os.path.basename(path).startswith("agent-")
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if started is None:
                started = parse_time(record.get("timestamp"))
            if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
            if session is None and isinstance(record.get("sessionId"), str) and record["sessionId"]:
                session = record["sessionId"]
            if record.get("isSidechain") is True or record.get("agentId"):
                subagent = True
    return {
        "path": path,
        "started": started,
        "cwd": cwd,
        "session": session,
        "subagent": subagent,
    }


def _token_forms(token):
    if not token:
        return []
    forms = [token]
    if "_" in token:
        swapped = token.replace("_", "-")
        if swapped not in forms:
            forms.append(swapped)
    return forms


def _common_dir(path, cache):
    if not path or not os.path.isdir(path):
        return None
    key = os.path.realpath(path)
    if key not in cache:
        proc = subprocess.run(
            ["git", "-C", key, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True,
        )
        value = proc.stdout.strip() if proc.returncode == 0 else ""
        cache[key] = os.path.realpath(value) if value else None
    return cache[key]


def tied_to_checkout(cwd, checkout, cache):
    if not cwd or not checkout:
        return False
    base = os.path.realpath(checkout) if os.path.isdir(checkout) else os.path.abspath(checkout)
    real = os.path.realpath(cwd) if os.path.isdir(cwd) else cwd
    if not os.path.isabs(real):
        real = os.path.abspath(real)
    if real == base or real.startswith(base + os.sep):
        return True
    if os.path.isdir(cwd) and os.path.isdir(checkout):
        left = _common_dir(cwd, cache)
        right = _common_dir(checkout, cache)
        return bool(left and right and left == right)
    return False


def _cwd_mentioned(cwd, blob):
    if not cwd or len(cwd) < 2 or not blob:
        return False
    start = 0
    while True:
        at = blob.find(cwd, start)
        if at < 0:
            return False
        before = blob[at - 1] if at else ""
        after = blob[at + len(cwd):at + len(cwd) + 1]
        if before in ("", " ", "\t", "\n", "\"", "'", "`", "=", "(", "{", "[", ":") and after in (
            "", " ", "\t", "\n", "\"", "'", "`", "\\", "|", ",", ";", "}", ")", "]", ":",
        ):
            return True
        start = at + 1


def iter_jsonl(root):
    if not root or not os.path.isdir(root):
        return []
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith("._") or not name.endswith(".jsonl"):
                continue
            found.append(os.path.join(dirpath, name))
    return found


def _text(path, cache):
    if path not in cache:
        with open(path, errors="replace") as handle:
            cache[path] = handle.read()
    return cache[path]


def sessions_for_run(root, mark_epoch, checkout=None, token=None):
    """Jsonl files that belong to the iteration that started at mark_epoch.

    A file counts when its first stamped record is at or after the mark.
    Modification time does not. With a checkout or a token, the file also has
    to be this run: its cwd is that checkout or another worktree of the same
    repository, or the file contains the token (underscore or hyphen form).
    A later pass adds a worker whose cwd the coordinator recorded, and a
    Claude Code subagent whose sessionId is an included parent. A subagent is
    never included on its own.
    """
    metas = []
    for path in iter_jsonl(root):
        try:
            meta = session_meta(path)
        except OSError:
            continue
        if meta["started"] is None or meta["started"].timestamp() < mark_epoch:
            continue
        metas.append(meta)
    forms = _token_forms(token)
    texts = {}
    git_cache = {}

    def token_hit(path):
        if not forms:
            return False
        body = _text(path, texts)
        return any(form in body for form in forms)

    def primary(meta):
        if meta["subagent"]:
            return False
        if not checkout and not forms:
            return True
        if checkout and tied_to_checkout(meta["cwd"], checkout, git_cache):
            return True
        return token_hit(meta["path"])

    included = [meta for meta in metas if primary(meta)]
    changed = True
    while changed:
        changed = False
        included_paths = {meta["path"] for meta in included}
        included_ids = {meta["session"] for meta in included if meta["session"]}
        blob = "\n".join(_text(meta["path"], texts) for meta in included)
        for meta in metas:
            if meta["path"] in included_paths:
                continue
            take = False
            if meta["subagent"] and meta["session"] and meta["session"] in included_ids:
                take = True
            elif not meta["subagent"] and _cwd_mentioned(meta["cwd"], blob):
                take = True
            if take:
                included.append(meta)
                changed = True
    return [meta["path"] for meta in included]


def run_has_end(root, mark_epoch, checkout=None, token=None):
    for path in sessions_for_run(root, mark_epoch, checkout, token):
        if file_has_end(path):
            return True
    return False


def copy_run_sessions(root, mark_epoch, dest, checkout=None, token=None):
    os.makedirs(dest, exist_ok=True)
    copied = []
    for path in sessions_for_run(root, mark_epoch, checkout, token):
        target = os.path.join(dest, os.path.basename(path))
        shutil.copy(path, target)
        copied.append(target)
    return copied


def run_is_quiet(root, mark_epoch, checkout=None, token=None, quiet_seconds=15, now=None):
    """True when every session that belongs to this run has stopped changing."""
    if now is None:
        now = time.time()
    for path in sessions_for_run(root, mark_epoch, checkout, token):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > now - quiet_seconds:
            return False
    return True


def mean(values):
    if not values:
        return None
    return sum(values) / len(values)


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(rows):
    closed = [row for row in rows if row["closed"]]

    def pair(key):
        values = [row[key] for row in closed if row[key] is not None]
        return mean(values), median(values)

    turns_mean, turns_median = pair("turns")
    tokens_mean, tokens_median = pair("tokens")
    workers_mean, workers_median = pair("workers")
    total_mean, total_median = pair("total_tokens")
    wall_mean, wall_median = pair("wall_seconds")
    return {
        "spans": len(closed),
        "unclosed": sum(1 for row in rows if not row["closed"]),
        "failures": sum(1 for row in rows if row["failed"]),
        "turns_mean": turns_mean,
        "turns_median": turns_median,
        "tokens_mean": tokens_mean,
        "tokens_median": tokens_median,
        "workers_mean": workers_mean,
        "workers_median": workers_median,
        "total_mean": total_mean,
        "total_median": total_median,
        "wall_mean": wall_mean,
        "wall_median": wall_median,
        "rows": rows,
    }


def cell(summary, key, kind):
    if summary is None:
        return "n/a"
    value = summary[key]
    if value is None:
        return "n/a"
    if kind == "d":
        return str(int(value))
    return f"{value:.1f}"


METRICS = (
    ("spans", "spans", "d"),
    ("turns mean", "turns_mean", "f"),
    ("turns median", "turns_median", "f"),
    ("context tokens mean", "tokens_mean", "f"),
    ("context tokens median", "tokens_median", "f"),
    ("worker sessions mean", "workers_mean", "f"),
    ("worker sessions median", "workers_median", "f"),
    ("total tokens mean", "total_mean", "f"),
    ("total tokens median", "total_median", "f"),
    ("wall seconds mean", "wall_mean", "f"),
    ("wall seconds median", "wall_median", "f"),
    ("failures", "failures", "d"),
    ("unclosed", "unclosed", "d"),
)


def render(before, after):
    lines = [f"{'metric':<22}  {'before':>10}  {'after':>10}"]
    for label, key, kind in METRICS:
        lines.append(f"{label:<22}  {cell(before, key, kind):>10}  {cell(after, key, kind):>10}")
    lines.append("")
    for title, summary in (("before", before), ("after", after)):
        if summary is None:
            lines.append("after: not recorded")
            lines.append("")
            continue
        lines.append(title)
        if not summary["rows"]:
            lines.append("  no ship spans")
        for row in summary["rows"]:
            lines.append("  " + format_span(row))
        lines.append("")
    return "\n".join(lines)


def format_span(row):
    task = row["task"] or "-"
    failed = "  failed" if row["failed"] else ""
    if not row["closed"]:
        return f"{row['file']}  {task}  unclosed{failed}"
    wall = "n/a" if row["wall_seconds"] is None else f"{row['wall_seconds']:.1f}"
    return (
        f"{row['file']}  {task}  turns {row['turns']}  "
        f"tokens {row['tokens']}  workers {row['workers']}  "
        f"total {row['total_tokens']}  wall {wall}{failed}"
    )


def pools_of(path):
    if os.path.isfile(path):
        return [[path]]
    if not os.path.isdir(path):
        raise SystemExit(f"ship efficiency: no such transcript {path}")
    pools = []
    direct = sorted(glob.glob(os.path.join(path, "*.jsonl")))
    if direct:
        pools.append(direct)
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        if not os.path.isdir(child):
            continue
        nested = sorted(glob.glob(os.path.join(child, "*.jsonl")))
        if nested:
            pools.append(nested)
    return pools


def collect(paths):
    pools = []
    for path in paths:
        pools.extend(pools_of(path))
    if not pools:
        raise SystemExit("ship efficiency: no transcripts")
    return pools


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", nargs="+", required=True)
    parser.add_argument("--after", nargs="+")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)
    try:
        before_pools = collect(args.before)
        after_pools = collect(args.after) if args.after else None
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    shrink = replay_shrink()

    def combined(pools):
        rows = []
        for pool in pools:
            rows.extend(rows_of(pool, shrink))
        return summarize(rows)

    before = combined(before_pools)
    after = combined(after_pools) if after_pools else None
    sys.stdout.write(render(before, after))
    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump({"before": before, "after": after}, handle, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
