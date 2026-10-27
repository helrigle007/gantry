#!/usr/bin/env python3
"""Ship and cleanup measurement harness. The golden cases score judge(); the transcripts score cost."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals", "ship"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import score
import ship_efficiency as efficiency

SCORE = os.path.join(ROOT, "evals", "ship", "score.py")
EFFICIENCY = os.path.join(ROOT, "scripts", "ship_efficiency.py")
BEFORE = os.path.join(ROOT, "evals", "ship", "fixtures", "before")
AFTER = os.path.join(ROOT, "evals", "ship", "fixtures", "after")
REQUIRED = (
    "land-green-squash",
    "red-checks-do-not-merge",
    "merge-on-red",
    "merge-without-checks",
    "merge-without-squash",
    "merge-with-admin",
    "merge-with-auto",
    "merge-without-match-head",
    "delete-foreign-branch",
    "delete-default-branch",
    "delete-foreign-by-refspec",
    "api-delete-foreign-branch",
    "worktree-force",
    "worktree-missing",
    "worktree-main-checkout",
    "worktree-own-cwd",
    "orca-worktree-rm",
    "remove-unmerged-worktree",
    "state-not-updated",
    "task-update-completes",
    "worker-release-updates-state",
    "force-push",
    "short-force-push",
    "plus-refspec",
    "lease-restack-allowed",
    "quoted-commands-are-not-actions",
    "stack-two-prs",
)


def test_cases_cover_the_rules_and_match_judge():
    cases = score.load_cases()
    by_id = {case["id"]: case for case in cases}
    assert len(cases) == 29, len(cases)
    for ident in REQUIRED:
        assert ident in by_id, ident
    owned = by_id["land-green-squash"]
    assert owned["created"] == ["feat/slug"]
    assert owned["require_removal"] is True
    assert all(owned["expect"][rule] is True for rule in score.RULES)
    assert by_id["lease-restack-allowed"]["expect"]["no-force-push"] is True
    assert by_id["force-push"]["expect"]["no-force-push"] is False
    assert by_id["red-checks-do-not-merge"]["require_removal"] is False
    assert by_id["quoted-commands-are-not-actions"]["expect"]["no-force-push"] is True
    report = score.score_cases(cases)
    assert report["cases"] == len(cases)
    assert report["passed"] == report["cases"], report
    assert report["exact_match"] == 1
    forced = copy.deepcopy(owned)
    forced["commands"] = [{"command": "git push --force origin feat/slug"}] + forced["commands"]
    got = score.judge(forced)
    assert got["no-force-push"] is False
    assert got["merge-green"] is True
    assert got["squash"] is True


def test_wrong_expect_fails_exact_match():
    broken = copy.deepcopy(score.load_cases())
    broken[0]["expect"]["no-force-push"] = False
    report = score.score_cases(broken)
    assert report["exact_match"] < 1
    assert report["results"][0]["id"] == "land-green-squash"
    assert report["results"][0]["passed"] is False
    assert any("no-force-push" in line for line in report["results"][0]["mismatches"])
    assert all(row["passed"] for row in report["results"][1:])


def test_missing_expect_is_an_error():
    broken = [{
        "id": "broken",
        "created": ["feat/slug"],
        "require_removal": True,
        "commands": [{"command": "git status"}],
    }]
    try:
        score.score_cases(broken)
    except score.CaseError as exc:
        assert "expect" in str(exc)
    else:
        raise AssertionError("expected CaseError")


def test_score_cli():
    proc = subprocess.run(
        [sys.executable, SCORE],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    count = len(score.load_cases())
    assert f"{count}/{count} passed  exact-match 1.000" in proc.stdout, proc.stdout
    assert "failures: none" in proc.stdout
    assert "land-green-squash" in proc.stdout and "pass" in proc.stdout
    assert "force-push" in proc.stdout


def test_markers_ignore_quoted_text():
    quoted = (
        "orca orchestration worker-start --spec 'Do not run worker-start --task-title gantry-ship "
        "or git worktree remove /tmp/example-repo-slug or git push --force origin feat/slug.' "
        "--task task_noise --worktree new-child --agent claude --json"
    )
    assert efficiency.events_of(quoted, "coordinator") == []
    assert efficiency.role_of([quoted]) == "coordinator"
    real = (
        "orca orchestration worker-start --task-title gantry-ship "
        "--worktree new-child --base-branch origin/main --agent claude --json"
    )
    assert [event["kind"] for event in efficiency.events_of(real, "coordinator")] == ["ship_dispatch"]
    cleanup = "orca orchestration worker-start --task-title gantry-cleanup --worktree current --agent claude --json"
    assert [event["kind"] for event in efficiency.events_of(cleanup, "coordinator")] == ["cleanup_dispatch"]
    report = (
        "orca orchestration send --from term_ship --type worker_done --outcome succeeded "
        "--body 'Landed the slug PR. task=task_ship pr=https://github.com/example-org/example-repo/pull/9 branch=feat/slug'"
    )
    assert [event["kind"] for event in efficiency.events_of(report, "ship")] == []
    ready = (
        "orca orchestration send --from term_impl --type worker_done --task-id task_slug --outcome succeeded "
        "--body 'Added the helper. Tests passed. Ready for ship. task=task_slug pr=https://github.com/example-org/example-repo/pull/9 branch=feat/slug'"
    )
    assert [event["kind"] for event in efficiency.events_of(ready, "implementation")] == ["pr_ready"]
    failed = (
        "orca orchestration send --from term_ship --type worker_done --outcome failed "
        "--body 'Verification failed. The PR is not landed. task=task_ship pr=none branch=feat/red'"
    )
    assert [event["kind"] for event in efficiency.events_of(failed, "ship")] == ["failure"]
    done = (
        "git worktree remove /tmp/example-repo-slug && "
        "orca orchestration task-update --task task_slug --status completed"
    )
    assert [event["kind"] for event in efficiency.events_of(done, "coordinator")] == ["remove", "task_done"]
    removed = "orca worktree rm --worktree path:/tmp/example-repo-slug"
    assert [event["kind"] for event in efficiency.events_of(removed, "cleanup")] == ["remove"]
    assert efficiency.file_has_end(os.path.join(BEFORE, "synthetic-before-cleanup.jsonl")) is True
    assert efficiency.file_has_end(os.path.join(BEFORE, "synthetic-before-ship.jsonl")) is False
    assert efficiency.role_of([
        "~/.gantry/current/kit/gantry-mailbox wait --types worker_done --timeout-ms 180000",
        "gh pr checks 9 --repo example-org/example-repo",
        "gh pr merge 9 --squash --match-head-commit abcdef1",
    ]) == "coordinator"
    assert efficiency.role_of(["gh pr checks 9", "gh pr merge 9 --squash"]) == "ship"
    assert efficiency.role_of(["git worktree remove /tmp/example-repo-slug"]) == "cleanup"
    assert efficiency.role_of([ready]) == "implementation"
    quoted_fold = (
        "orca orchestration worker-start --spec 'Run gantry cleanup task_slug and gantry ship 9.' "
        "--task task_noise --worktree new-child --agent claude --json"
    )
    assert efficiency.events_of(quoted_fold, "coordinator") == []
    assert efficiency.events_of("gantry ship 9 --task task_slug", "coordinator") == []
    assert efficiency.events_of("echo gantry cleanup task_slug", "coordinator") == []
    folded = efficiency.events_of("gantry cleanup task_slug", "coordinator")
    assert [event["kind"] for event in folded] == ["remove"]
    assert folded[0]["task"] == "task_slug"
    assert efficiency.events_of("gantry cleanup --scan", "coordinator") == []
    assert efficiency.events_of("gantry cleanup --dry-run task_slug", "coordinator") == []
    assert efficiency.events_of("python3 /opt/gantry_kit.py cleanup --dry-run task_slug", "coordinator") == []
    finished = efficiency.events_of("gantry cleanup --finished --run run_1", "coordinator")
    assert [event["kind"] for event in finished] == ["remove"]
    assert finished[0]["task"] is None
    kit_clean = efficiency.events_of("python3 /opt/gantry_kit.py cleanup task_slug --run run_1", "coordinator")
    assert [event["kind"] for event in kit_clean] == ["remove"]
    assert kit_clean[0]["task"] == "task_slug"
    assert efficiency.role_of(["gantry ship 9", "gantry cleanup task_slug"]) == "coordinator"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fold.jsonl")
        write_jsonl(path, [
            assistant("f1", "2026-10-27T14:00:00Z", 1000, [tool_use("f1", "gantry cleanup task_slug")]),
        ])
        assert efficiency.file_has_end(path) is True
        scan = os.path.join(tmp, "scan.jsonl")
        write_jsonl(scan, [
            assistant("f2", "2026-10-27T14:01:00Z", 1000, [tool_use("f2", "gantry cleanup --scan")]),
        ])
        assert efficiency.file_has_end(scan) is False
        dry = os.path.join(tmp, "dry.jsonl")
        write_jsonl(dry, [
            assistant("f3", "2026-10-27T14:02:00Z", 1000, [tool_use("f3", "gantry cleanup --dry-run task_slug")]),
        ])
        assert efficiency.file_has_end(dry) is False


def test_prompt_text_is_not_a_span():
    prompt = (
        "dispatch worker-start --task-title gantry-ship and then git worktree remove /tmp/example-repo-slug "
        "or orca orchestration task-update --status completed"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prompt.jsonl")
        write_jsonl(path, [
            {"type": "user", "timestamp": "2026-10-27T11:50:00Z", "message": {"role": "user", "content": prompt}},
            assistant("r", "2026-10-27T11:51:00Z", 1000, [{"type": "text", "text": prompt}]),
        ])
        rows = efficiency.rows_of([path], efficiency.replay_shrink())
        assert rows == []
        assert efficiency.file_has_end(path) is False


def test_efficiency_of_the_synthetic_transcripts():
    factor = efficiency.replay_shrink()["worker-show"]
    tokens_a = round(1200 + 300 * factor)
    assert tokens_a != 1500, tokens_a
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.json")
        proc = subprocess.run(
            [sys.executable, EFFICIENCY, "--json", out, "--before", BEFORE, "--after", AFTER],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.load(open(out))
    before = data["before"]
    after = data["after"]
    assert before["spans"] == 3 and before["unclosed"] == 1 and before["failures"] == 1
    assert [row["task"] for row in before["rows"]] == ["task_slug", "task_red", "task_open", "task_fast"]
    assert before["rows"][0]["pr"] == "https://github.com/example-org/example-repo/pull/9"
    assert before["rows"][0]["file"] == "synthetic-before-coordinator.jsonl"
    assert before["rows"][0]["turns"] == 3
    assert before["rows"][0]["tokens"] == tokens_a
    assert before["rows"][0]["workers"] == 2
    assert before["rows"][0]["total_tokens"] == tokens_a + 700
    assert before["rows"][0]["wall_seconds"] == 40
    assert before["rows"][0]["failed"] is False
    assert before["rows"][1]["turns"] == 2
    assert before["rows"][1]["tokens"] == 100
    assert before["rows"][1]["workers"] == 1
    assert before["rows"][1]["total_tokens"] == 200
    assert before["rows"][1]["wall_seconds"] == 20
    assert before["rows"][1]["failed"] is True
    assert before["rows"][1]["closed"] is True
    assert before["rows"][2]["closed"] is False
    assert before["rows"][2]["failed"] is False
    assert before["rows"][2]["file"] == "synthetic-before-coordinator.jsonl"
    assert before["rows"][3]["turns"] == 1
    assert before["rows"][3]["tokens"] == 0
    assert before["rows"][3]["workers"] == 0
    assert before["rows"][3]["total_tokens"] == 0
    assert before["rows"][3]["wall_seconds"] == 5
    assert before["turns_mean"] == 2
    assert before["turns_median"] == 2
    assert before["tokens_median"] == 100
    assert before["workers_mean"] == 1
    assert before["workers_median"] == 1
    assert before["total_median"] == 200
    assert before["wall_median"] == 20
    assert abs(before["tokens_mean"] - (tokens_a + 100) / 3) < 1e-9
    assert abs(before["total_mean"] - (tokens_a + 700 + 200) / 3) < 1e-9
    assert abs(before["wall_mean"] - (40 + 20 + 5) / 3) < 1e-9
    assert after["spans"] == 2 and after["unclosed"] == 0 and after["failures"] == 0
    assert after["turns_mean"] == 1.5 and after["turns_median"] == 1.5
    assert after["tokens_mean"] == 0 and after["workers_mean"] == 0 and after["total_mean"] == 0
    assert after["wall_mean"] == 2.5 and after["wall_median"] == 2.5
    assert after["rows"][0]["task"] == "task_slug"
    assert after["rows"][0]["turns"] == 2 and after["rows"][0]["workers"] == 0
    assert after["rows"][0]["wall_seconds"] == 2 and after["rows"][0]["total_tokens"] == 0
    assert after["rows"][1]["task"] == "task_docs"
    assert after["rows"][1]["turns"] == 1 and after["rows"][1]["wall_seconds"] == 3
    text = proc.stdout
    assert "metric" in text and "before" in text and "after" in text
    assert "worker sessions mean" in text and "total tokens mean" in text
    assert "unclosed" in text and "failures" in text
    assert f"{before['turns_mean']:.1f}" in text
    assert f"{before['tokens_mean']:.1f}" in text
    assert f"{after['wall_mean']:.1f}" in text
    assert "task_slug" in text and "unclosed" in text and "failed" in text


def test_worker_files_close_the_span_and_add_tokens():
    shrink = efficiency.replay_shrink()
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("synthetic-before-coordinator.jsonl", "synthetic-before-impl.jsonl"):
            shutil.copy(os.path.join(BEFORE, name), os.path.join(tmp, name))
        rows = efficiency.rows_of([
            os.path.join(tmp, "synthetic-before-coordinator.jsonl"),
            os.path.join(tmp, "synthetic-before-impl.jsonl"),
        ], shrink)
        by_task = {row["task"]: row for row in rows}
        assert by_task["task_slug"]["closed"] is False
        assert by_task["task_red"]["failed"] is False
        assert by_task["task_red"]["closed"] is True
        for name in ("synthetic-before-ship-failed.jsonl", "synthetic-before-cleanup.jsonl"):
            shutil.copy(os.path.join(BEFORE, name), os.path.join(tmp, name))
        rows = efficiency.rows_of(sorted(
            os.path.join(tmp, name) for name in os.listdir(tmp) if name.endswith(".jsonl")
        ), shrink)
        by_task = {row["task"]: row for row in rows}
        assert by_task["task_slug"]["closed"] is True
        assert by_task["task_slug"]["failed"] is False
        assert by_task["task_red"]["failed"] is True
        assert by_task["task_red"]["closed"] is True
        assert by_task["task_red"]["total_tokens"] == 200


def test_runs_do_not_share_spans():
    with tempfile.TemporaryDirectory() as tmp:
        one = os.path.join(tmp, "one")
        two = os.path.join(tmp, "two")
        os.makedirs(one)
        os.makedirs(two)
        write_jsonl(os.path.join(one, "coordinator.jsonl"), [
            assistant("r", "2026-10-27T12:00:00Z", 1000, [tool_use(
                "t",
                "orca orchestration worker-start --task-title gantry-ship --worktree new-child --agent claude --json",
            )]),
        ])
        write_jsonl(os.path.join(two, "cleanup.jsonl"), [
            assistant("r", "2026-10-27T12:05:00Z", 1000, [tool_use(
                "t", "git worktree remove /tmp/example-repo-slug",
            )]),
        ])
        out = os.path.join(tmp, "out.json")
        proc = subprocess.run(
            [sys.executable, EFFICIENCY, "--json", out, "--before", tmp],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.load(open(out))
    assert data["before"]["spans"] == 0
    assert data["before"]["unclosed"] == 1
    assert data["after"] is None


def test_before_only_marks_after_as_not_recorded():
    proc = subprocess.run(
        [sys.executable, EFFICIENCY, "--before", BEFORE],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "after: not recorded" in proc.stdout
    assert "unclosed" in proc.stdout


def test_quoted_spec_names_the_coordinator_and_skips_subagents():
    fixture = os.path.join(ROOT, "evals", "ship", "fixtures", "attribution")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.json")
        proc = subprocess.run(
            [sys.executable, EFFICIENCY, "--json", out, "--before", fixture],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.load(open(out))
    assert data["before"]["spans"] == 1
    assert data["before"]["unclosed"] == 0
    row = data["before"]["rows"][0]
    assert row["file"] == "coordinator.jsonl"
    assert row["task"] == "task_slug"
    assert row["closed"] is True
    assert row["workers"] == 2
    assert row["turns"] == 2
    assert row["wall_seconds"] == 40
    assert "coordinator.jsonl  task_slug" in proc.stdout
    assert "impl.jsonl  task_slug" not in proc.stdout
    assert "workers 2" in proc.stdout
    assert efficiency.file_has_end(os.path.join(fixture, "agent-orca.jsonl")) is False
    assert efficiency.file_has_end(os.path.join(fixture, "cleanup.jsonl")) is True


def test_live_loop_counts_only_this_run():
    mark = efficiency.parse_time("2026-10-27T13:00:00Z").timestamp()
    src = os.path.join(ROOT, "evals", "ship", "fixtures", "isolation")

    def fill(dest, names, replacements):
        os.makedirs(dest, exist_ok=True)
        for name in names:
            text = open(os.path.join(src, name)).read()
            for old, new in replacements.items():
                text = text.replace(old, new)
            with open(os.path.join(dest, name), "w") as handle:
                handle.write(text)

    with tempfile.TemporaryDirectory() as tmp:
        checkout = os.path.join(tmp, "checkout")
        projects = os.path.join(tmp, "projects")
        os.makedirs(checkout)
        subprocess.run(["git", "init", "-q", "-b", "main", checkout], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"],
            check=True, capture_output=True,
        )
        worktree = os.path.join(tmp, "worktree")
        subprocess.run(
            ["git", "-C", checkout, "worktree", "add", "-q", "-b", "feat", worktree],
            check=True, capture_output=True,
        )
        fill(projects, [
            "previous-cleanup.jsonl", "late-agent.jsonl", "current-coordinator.jsonl",
        ], {"__CHECKOUT__": checkout})
        previous = os.path.join(projects, "previous-cleanup.jsonl")
        current = os.path.join(projects, "current-coordinator.jsonl")
        os.utime(previous, None)
        os.utime(current, (mark, mark))
        assert efficiency.run_has_end(projects, mark, checkout, "word_200") is False
        copied = efficiency.copy_run_sessions(projects, mark, os.path.join(tmp, "copied"), checkout, "word_200")
        assert [os.path.basename(path) for path in copied] == ["current-coordinator.jsonl"]
        assert efficiency.run_is_quiet(projects, mark, checkout, "word_200", 15) is True
        os.utime(current, None)
        assert efficiency.run_is_quiet(projects, mark, checkout, "word_200", 15) is False
        fill(projects, ["current-cleanup.jsonl"], {"__CHECKOUT__": checkout})
        fill(projects, ["worktree-worker.jsonl"], {"__WORKTREE__": worktree})
        other = os.path.join(tmp, "other")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", "-b", "main", other], check=True, capture_output=True)
        write_jsonl(os.path.join(projects, "alien.jsonl"), [
            {
                "type": "assistant",
                "timestamp": "2026-10-27T13:12:00Z",
                "cwd": other,
                "sessionId": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "requestId": "z",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": [tool_use("z", "git worktree remove /tmp/example-repo-other")],
                },
            },
        ])
        assert efficiency.run_has_end(projects, mark, checkout, "word_200") is True
        names = sorted(
            os.path.basename(path)
            for path in efficiency.copy_run_sessions(projects, mark, os.path.join(tmp, "full"), checkout, "word_200")
        )
        assert names == ["current-cleanup.jsonl", "current-coordinator.jsonl", "worktree-worker.jsonl"]
        # The worktree is gone, which is how copy runs after cleanup. The coordinator
        # recorded it as path:<worktree>, and that session still belongs to this run.
        os.rename(worktree, worktree + "-gone")
        with open(os.path.join(projects, "current-coordinator.jsonl"), "a") as handle:
            handle.write("\n")
        noted = os.path.join(projects, "noted-worker.jsonl")
        text = open(os.path.join(src, "worktree-worker.jsonl")).read().replace("__WORKTREE__", worktree)
        text = text.replace("dddddddd-dddd-dddd-dddd-dddddddddddd", "ffffffff-ffff-ffff-ffff-ffffffffffff")
        with open(noted, "w") as handle:
            handle.write(text)
        with open(os.path.join(projects, "current-coordinator.jsonl"), "a") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-10-27T13:16:00Z",
                "cwd": checkout,
                "sessionId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "requestId": "note",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "note",
                        "name": "Bash",
                        "input": {"command": "orca worktree rm --worktree path:" + worktree},
                    }],
                },
            }) + "\n")
        noted_names = [
            os.path.basename(path)
            for path in efficiency.copy_run_sessions(projects, mark, os.path.join(tmp, "noted"), checkout, "word_200")
        ]
        assert "noted-worker.jsonl" in noted_names


def test_missing_transcript_is_an_error():
    proc = subprocess.run(
        [sys.executable, EFFICIENCY, "--before", os.path.join(ROOT, "no-such-transcript")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "no such transcript" in proc.stderr


def assistant(rid, stamp, ctx, content):
    return {
        "type": "assistant",
        "timestamp": stamp,
        "requestId": rid,
        "message": {
            "usage": {
                "input_tokens": ctx,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 8,
            },
            "content": content,
        },
    }


def tool_use(tid, command):
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}


def write_jsonl(path, records):
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    test_cases_cover_the_rules_and_match_judge()
    test_wrong_expect_fails_exact_match()
    test_missing_expect_is_an_error()
    test_score_cli()
    test_markers_ignore_quoted_text()
    test_prompt_text_is_not_a_span()
    test_efficiency_of_the_synthetic_transcripts()
    test_worker_files_close_the_span_and_add_tokens()
    test_runs_do_not_share_spans()
    test_before_only_marks_after_as_not_recorded()
    test_quoted_spec_names_the_coordinator_and_skips_subagents()
    test_live_loop_counts_only_this_run()
    test_missing_transcript_is_an_error()
    print("13/13 passed")
