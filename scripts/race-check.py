#!/usr/bin/env python3
"""Start N new-child workers in parallel and time each one's first heartbeat.

Usage: race-check.py <repo-path> [n=3] [timeout-s=120]

Run from an Orca terminal already bound to a Run (orchestration run-create).
Prints one JSON line per Dispatch. `raced: true` means worker-start reported
ready but no heartbeat arrived before the timeout: the prompt race.
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SPEC = (
    "Race probe. As your very first action, send one heartbeat with phase investigating "
    "using the exact preamble command. Then send worker_done --outcome succeeded with body "
    "'Race probe sent heartbeat. Nothing else was done. Nothing remains.' "
    "Do nothing else, and do not load any skills."
)


def orca(*args):
    out = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True).stdout
    data = json.loads(out or "{}")
    return data.get("result") or data.get("error") or {}


def start(repo, stamp, i):
    t0 = time.time()
    r = orca("orchestration", "worker-start", "--spec", SPEC, "--task-title", f"race-{stamp}-{i}",
             "--worktree", "new-child", "--repo", f"path:{repo}", "--name", f"race-{stamp}-{i}",
             "--agent", "claude")
    return {"dispatch": r.get("dispatchId"), "state": r.get("state"), "stage": r.get("stage"),
            "lastError": r.get("lastError"), "start_s": round(time.time() - t0, 1)}


def wait_heartbeat(dispatch, deadline):
    while True:
        d = orca("orchestration", "worker-show", "--dispatch", dispatch).get("dispatch") or {}
        if d.get("lastHeartbeatAt") or time.time() > deadline:
            return d


def main():
    repo = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else 120
    stamp = int(time.time())
    with ThreadPoolExecutor(n) as pool:
        starts = list(pool.map(lambda i: start(repo, stamp, i), range(1, n + 1)))
    deadline = time.time() + timeout_s
    for s in starts:
        d = wait_heartbeat(s["dispatch"], deadline) if s["state"] == "ready" else {}
        s.update(dispatchedAt=d.get("dispatchedAt"), firstHeartbeatAt=d.get("lastHeartbeatAt"),
                 status=d.get("status"), raced=s["state"] == "ready" and not d.get("lastHeartbeatAt"))
        print(json.dumps(s), flush=True)


if __name__ == "__main__":
    main()
