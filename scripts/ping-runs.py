#!/usr/bin/env python3
import glob, json, os, subprocess, sys, time
from datetime import datetime, timezone

def claude_project(repo):
    real = os.path.realpath(repo)
    slug = "".join(ch if ch.isalnum() else "-" for ch in real)
    return os.path.expanduser(os.path.join("~/.claude/projects", slug))
MARK = "Background-wait timing test"
OBJECTIVE = f"""{MARK}, one task, no PR, no ship worker.
Task: one read-only worker in --worktree current. It sends its first heartbeat, then runs exactly one Bash command: python3 -c "import time; time.sleep(180)". Then it sends worker_done --outcome succeeded with the output of `git log --oneline -3`. It edits nothing and opens no PR.
Answer any user message that contains PING with one line that starts with PING-REPLY and names the current UTC time from `date -u +%H:%M:%S`."""


def orca(*args):
    return json.loads(subprocess.run(["orca", *args, "--json"], capture_output=True, text=True, check=True).stdout)["result"]


def events(path):
    out = []
    for line in open(path):
        d = json.loads(line)
        c = d.get("message", {}).get("content")
        ts = d.get("timestamp")
        if isinstance(c, str):
            out.append((ts, d.get("type"), "text", c))
            continue
        for x in c or []:
            if x.get("type") == "tool_use":
                out.append((ts, "use", x["name"], x["input"]))
            elif x.get("type") == "text":
                out.append((ts, d.get("type"), "text", x["text"]))
    return out


def find_transcript(since):
    for p in sorted(glob.glob(f"{PROJ}/*.jsonl"), key=os.path.getmtime, reverse=True):
        if os.path.getmtime(p) < since:
            continue
        ev = events(p)
        if ev and ev[0][1] == "user" and MARK in str(ev[0][3])[:200]:
            return p
    return None


def until(pred, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        r = pred()
        if r:
            return r
        time.sleep(step)
    return None


def is_wait(command):
    return "gantry wait" in command or "gantry-mailbox wait" in command


def waits(ev):
    return [e for e in ev if e[1] == "use" and e[2] == "Bash" and is_wait(e[3].get("command", ""))]


def run(n):
    start = time.time()
    handle = orca("terminal", "create", "--worktree", f"path:{FIXTURE}", "--title", f"gantry-bgwait-{n}",
                  "--command", f"gantry {json.dumps(OBJECTIVE)}")["terminal"]["handle"]
    path = until(lambda: find_transcript(start), 120)
    assert path, "coordinator transcript never appeared"
    assert until(lambda: waits(events(path)), 400), "no gantry wait started"
    time.sleep(10)
    sent = datetime.now(timezone.utc)
    subprocess.run(["orca", "terminal", "send", "--terminal", handle, "--text", "PING: are you still receiving worker messages?", "--enter", "--json"],
                   capture_output=True, check=True)
    reply = until(lambda: next((e for e in events(path) if e[1] == "assistant" and "PING-REPLY" in e[3]), None), 120, 1)
    latency = (datetime.fromisoformat(reply[0].replace("Z", "+00:00")) - sent).total_seconds() if reply else None

    def settled():
        ev = events(path)
        rel = [i for i, e in enumerate(ev) if e[1] == "use" and "worker-release" in str(e[3])]
        rewaits = [i for i, e in enumerate(ev) if e[1] == "use" and e[2] == "Bash" and is_wait(e[3].get("command", ""))]
        return (ev, rel, rewaits) if rel and any(a > rel[0] for a in rewaits) else None

    done = until(settled, 600)
    ev = events(path) if not done else done[0]
    w = waits(ev)
    result = {
        "run": n, "transcript": os.path.basename(path), "ping_sent": sent.strftime("%H:%M:%S"),
        "ping_reply_s": latency, "reply": reply[3][:160] if reply else None,
        "waits": len(w), "waits_background": sum(bool(x[3].get("run_in_background")) for x in w),
        "worker_done_released_and_rewaited": bool(done),
        "gate_list_calls": sum(1 for e in ev if e[1] == "use" and "gate-list" in str(e[3])),
    }
    time.sleep(15)
    subprocess.run(["orca", "terminal", "close", "--terminal", handle, "--json"], capture_output=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: ping-runs.py <repo> <n>")
    FIXTURE = sys.argv[1]
    PROJ = claude_project(FIXTURE)
    for n in range(1, int(sys.argv[2]) + 1):
        try:
            r = run(n)
        except Exception as e:
            r = {"run": n, "error": repr(e)}
        print(json.dumps(r), flush=True)
