#!/usr/bin/env python3
"""Quiet-mode charter check. The golden lines score the Say section and a sample transcript."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals", "quiet"))
import score

SCORE = os.path.join(ROOT, "evals", "quiet", "score.py")
COORDINATOR = os.path.join(ROOT, "template", "kit", "gantry-coordinator.md")


def test_charter_and_sample_match():
    cases = score.load_cases()
    report = score.score(cases)
    failed = [label for ok, label in report if not ok]
    assert failed == [], failed
    assert score.lines_of(score.AFTER) == cases["after"]
    text = open(COORDINATOR).read()
    say = score.say_section(text)
    assert "Any other user-visible text is forbidden" in say
    assert "No plan recap" in say
    for line in cases["formats"]:
        assert line in say
    assert "Send the preamble heartbeat" in text


def test_narration_and_bad_lines_fail():
    wall = "Worker confirmed (heartbeat). Waiting for next messages.\nNothing needs your input.\n"
    assert score.quiet_errors(score.lines_of_text(wall))
    assert score.narration_hits(wall, ["Worker confirmed (heartbeat)", "Nothing needs your input"]) == [
        "Worker confirmed (heartbeat)",
        "Nothing needs your input",
    ]
    assert score.quiet_errors(["▸ started slug-helper · 1 worker(s) · overlap clear"])
    assert score.quiet_errors(["▸ started slug-helper · 1 worker · overlap clear"]) == []
    landed = [
        "✓ landed #6 · squash-merged · cleaned up",
        "  head branch was already gone",
    ]
    assert score.quiet_errors(landed) == []
    assert score.quiet_errors(landed + ["  and then a second paragraph"])
    assert score.quiet_errors(["✓ landed #4 · squash-merged · left in place"]) == []
    assert score.quiet_errors(["  1. Proceed"])
    held = ["! blocked slug-helper · overlap hold · not starting"]
    assert score.quiet_errors(held) == []


def test_a_missing_format_fails_the_score():
    cases = score.load_cases()
    text = open(COORDINATOR).read().replace(cases["formats"][0], "started: free prose", 1)
    report = score.score(cases, coordinator=text)
    failed = [label for ok, label in report if not ok]
    assert any(cases["formats"][0] in label for label in failed), failed


def test_score_cli():
    proc = subprocess.run([sys.executable, SCORE], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("passed")
    assert "FAIL" not in proc.stdout


if __name__ == "__main__":
    test_charter_and_sample_match()
    test_narration_and_bad_lines_fail()
    test_a_missing_format_fails_the_score()
    test_score_cli()
    print("4/4 passed")
