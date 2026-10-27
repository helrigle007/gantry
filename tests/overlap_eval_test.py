#!/usr/bin/env python3
"""Overlap measurement harness. The golden cases score classify(); the transcripts score cost."""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals", "overlap"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
import gantry_kit
import overlap_efficiency as efficiency
import score

SCORE = os.path.join(ROOT, "evals", "overlap", "score.py")
EFFICIENCY = os.path.join(ROOT, "scripts", "overlap_efficiency.py")
BEFORE = os.path.join(ROOT, "evals", "overlap", "fixtures", "before")
AFTER = os.path.join(ROOT, "evals", "overlap", "fixtures", "after")
REQUIRED = (
    "exact-duplicate-pr",
    "duplicate-branch-no-pr",
    "related-pr-different-intent",
    "unrelated-prs",
    "assigned-issue-matching-title",
    "active-worker-same-task",
    "no-candidates",
    "near-miss-branch-different-work",
    "mixed-duplicate-and-unrelated",
    "mixed-related-and-unrelated",
    "adversarial-short-title",
    "adversarial-hyphenated-branch",
    "related-owner-prefixed-branch",
)


def test_cases_cover_the_rules_and_match_classify():
    cases = score.load_cases()
    by_id = {case["id"]: case for case in cases}
    assert len(cases) >= 15, len(cases)
    for ident in REQUIRED:
        assert ident in by_id, ident
    issue = by_id["assigned-issue-matching-title"]
    assert issue["candidates"][0]["files"] == []
    assert issue["candidates"][0]["label"] == "unrelated"
    assert issue["verdict"] == "continue"
    worker = by_id["active-worker-same-task"]
    assert worker["candidates"][0]["label"] == "duplicate"
    assert worker["verdict"] == "ask"
    report = score.score_cases(cases, gantry_kit.classify, gantry_kit.verdict_of)
    assert report["cases"] == len(cases)
    assert report["passed"] == report["cases"], report
    assert report["label_passed"] == report["cases"]
    assert report["verdict_passed"] == report["cases"]
    assert report["exact_match"] == 1


def test_stop_maps_to_ask():
    assert score.as_ask("stop") == "ask"
    assert score.as_ask("continue") == "continue"
    assert score.as_ask(gantry_kit.verdict_of(["duplicate", "unrelated"])) == "ask"
    assert score.as_ask(gantry_kit.verdict_of(["related"])) == "ask"
    assert score.as_ask(gantry_kit.verdict_of(["unrelated"])) == "continue"
    assert score.as_ask(gantry_kit.verdict_of([])) == "continue"


def test_wrong_classifier_fails_exact_match():
    def always_unrelated(intent, planned, candidate):
        return "unrelated"

    def always_continue(labels):
        return "continue"

    report = score.score_cases(score.load_cases(), always_unrelated, always_continue)
    by_id = {row["id"]: row for row in report["results"]}
    assert report["exact_match"] < 1
    assert by_id["exact-duplicate-pr"]["passed"] is False
    assert by_id["active-worker-same-task"]["passed"] is False
    assert by_id["adversarial-short-title"]["passed"] is False
    assert by_id["no-candidates"]["passed"] is True
    assert by_id["unrelated-prs"]["passed"] is True
    assert by_id["assigned-issue-matching-title"]["passed"] is True
    assert "expected duplicate, got unrelated" in "\n".join(by_id["exact-duplicate-pr"]["mismatches"])


def test_missing_label_is_an_error():
    broken = [{
        "id": "broken",
        "task": "Fix the crash in app.",
        "intent": "Fix the crash in app",
        "files": ["src/app.py"],
        "verdict": "ask",
        "candidates": [{"kind": "pr", "id": "1", "title": "Fix the crash in app", "files": ["src/app.py"]}],
    }]
    try:
        score.score_cases(broken, gantry_kit.classify, gantry_kit.verdict_of)
    except score.CaseError as exc:
        assert "label" in str(exc)
    else:
        raise AssertionError("expected CaseError")


def test_score_cli():
    proc = subprocess.run(
        [sys.executable, SCORE, "--classify", "gantry_kit:classify", "--verdict", "gantry_kit:verdict_of"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    count = len(score.load_cases())
    assert f"{count}/{count} passed  exact-match 1.000" in proc.stdout, proc.stdout
    assert f"classify {count}/{count}  verdict_of {count}/{count}" in proc.stdout
    assert "failures: none" in proc.stdout
    assert "exact-duplicate-pr" in proc.stdout and "pass" in proc.stdout


def test_quoted_spec_is_not_an_overlap_check():
    spec = "orca orchestration worker-start --spec 'Playbook: feature. Do not run gantry overlap yourself.'"
    assert efficiency.overlap_role(spec) is None
    prefixed = "~/.gantry/current/bin/gantry overlap --checkpoint scoped --intent 'x' --files a.py"
    assert efficiency.overlap_role(prefixed) == "gather"
    chained = "cd /path/to/example-repo && gantry overlap --checkpoint scoped --intent 'a && b' --files a.py"
    assert efficiency.overlap_role(chained) == "gather"
    recorded = "gantry overlap --record classified --checkpoint pre-pr --run run_1 --task task_1 --verdict continue"
    assert efficiency.overlap_role(recorded) == "record"
    answered = "gantry overlap --record answered --checkpoint scoped --run run_1 --task task_1 --answer proceed"
    assert efficiency.overlap_role(answered) == "record"
    gathered = "gantry overlap --record gathered --checkpoint scoped --run run_1 --task task_1"
    assert efficiency.overlap_role(gathered) is None


def test_efficiency_of_the_synthetic_transcripts():
    from gantry_run import replay_shrink
    factor = replay_shrink()["worker-show"]
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
    assert before["checks"] == 3 and before["unclosed"] == 1
    assert before["turns_mean"] == 4
    assert before["turns_median"] == 3
    assert before["rows"][0]["turns"] == 3
    assert before["rows"][0]["tokens"] == tokens_a
    assert before["rows"][0]["wall_seconds"] == 40
    assert before["rows"][0]["verdict"] == "stop"
    assert before["rows"][0]["checkpoint"] == "scoped"
    assert before["rows"][1]["turns"] == 2 and before["rows"][1]["tokens"] == 900
    assert before["rows"][1]["checkpoint"] == "pre-pr"
    assert before["rows"][1]["verdict"] == "continue"
    assert before["rows"][2]["turns"] == 7 and before["rows"][2]["tokens"] == 100
    assert before["rows"][3]["closed"] is False
    assert before["tokens_median"] == 900
    assert abs(before["tokens_mean"] - (tokens_a + 900 + 100) / 3) < 1e-9
    assert abs(before["wall_mean"] - (40 + 20 + 10) / 3) < 1e-9
    assert before["wall_median"] == 20
    assert after["checks"] == 2 and after["unclosed"] == 0
    assert after["turns_mean"] == 1 and after["tokens_mean"] == 0
    assert after["wall_mean"] == 2.5
    assert after["rows"][0]["wall_seconds"] == 2
    assert after["rows"][1]["wall_seconds"] == 3
    assert after["rows"][0]["verdict"] == "continue"
    text = proc.stdout
    assert f"{before['turns_mean']:.1f}" in text
    assert f"{before['turns_median']:.1f}" in text
    assert f"{before['tokens_mean']:.1f}" in text
    assert f"{after['wall_mean']:.1f}" in text
    assert "metric" in text and "before" in text and "after" in text
    assert "unclosed" in text


def test_answered_record_closes_the_check():
    lines = [
        assistant("r1", "2026-10-26T12:10:00Z", 1000, [tool_use("t1", "gantry overlap --checkpoint scoped --intent 'Add a slug helper' --files src/slug.py --run run_1 --task task_slug")]),
        tool_result("t1", "2026-10-26T12:10:01Z", '{"intent":"Add a slug helper","files":["src/slug.py"],"candidates":[]}'),
        assistant("r2", "2026-10-26T12:10:05Z", 1100, [tool_use("t2", "gantry overlap --record answered --checkpoint scoped --run run_1 --task task_slug --answer proceed")]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "answered.jsonl")
        write_jsonl(path, lines)
        rows = efficiency.rows_of([path], efficiency.replay_shrink())
    assert len(rows) == 1 and rows[0]["closed"] is True
    assert rows[0]["turns"] == 2
    assert rows[0]["verdict"] == "proceed"
    assert rows[0]["wall_seconds"] == 5
    assert rows[0]["tokens"] == 100


def test_before_only_marks_after_as_not_recorded():
    proc = subprocess.run(
        [sys.executable, EFFICIENCY, "--before", BEFORE],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "after: not recorded" in proc.stdout
    assert "unclosed" in proc.stdout


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


def tool_result(tid, stamp, text):
    return {
        "type": "user",
        "timestamp": stamp,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
    }


def write_jsonl(path, records):
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    test_cases_cover_the_rules_and_match_classify()
    test_stop_maps_to_ask()
    test_wrong_classifier_fails_exact_match()
    test_missing_label_is_an_error()
    test_score_cli()
    test_quoted_spec_is_not_an_overlap_check()
    test_efficiency_of_the_synthetic_transcripts()
    test_answered_record_closes_the_check()
    test_before_only_marks_after_as_not_recorded()
    print("9/9 passed")
