#!/usr/bin/env python3
"""Score the coordinator charter's quiet-mode lines and a sample transcript.

The Say section must contain the fixed line formats and must forbid plan
recaps and heartbeat narration. The sample after-transcript must be those
lines and nothing else. The before-transcript is the wall of text those
rules replace, and the same checker must reject it.

  python3 evals/quiet/score.py
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "cases.json")
COORDINATOR = os.path.join(ROOT, "template", "kit", "gantry-coordinator.md")
SHIP = os.path.join(ROOT, "template", "kit", "gantry-ship.md")
BEFORE = os.path.join(HERE, "fixtures", "before.txt")
AFTER = os.path.join(HERE, "fixtures", "after.txt")

SLUG = r"[A-Za-z0-9][A-Za-z0-9-]*"
NUM = r"[1-9]\d*"
FIELD = r"(?:(?! · ).)+"
STARTED = re.compile(rf"^▸ started {SLUG} · {NUM} workers? · overlap clear$")
OPENED = re.compile(rf"^▸ PR #{NUM} opened · overlap clear · checks running$")
LANDED = re.compile(rf"^✓ landed #{NUM} · squash-merged · (?:cleaned up|left in place)$")
BLOCKED = re.compile(rf"^! blocked (?:#{NUM}|{SLUG}) · {FIELD} · {FIELD}$")
PSTACK_NEWER = re.compile(r"^! pstack v?\d+(?:\.\d+)* newer than tested v?\d+(?:\.\d+)* · continuing$")
QUESTION = re.compile(r"^\? \S")
OPTION = re.compile(r"^  [1-9]\d*\. \S")


def load_cases(path=None):
    with open(path or CASES) as handle:
        data = json.load(handle)
    for key in ("formats", "forbids", "absent", "ship_formats", "ship_absent", "keeps", "narration", "after"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise ValueError(f"cases missing {key}")
    return data


def say_section(text):
    start = text.find("\n## Say\n")
    if start < 0:
        return ""
    rest = text[start + 1:]
    nxt = rest.find("\n## ", 1)
    return rest if nxt < 0 else rest[:nxt]


def lines_of(path):
    with open(path) as handle:
        return [line.rstrip("\n") for line in handle.read().splitlines() if line.strip()]


def event(line):
    return bool(
        STARTED.match(line) or OPENED.match(line) or LANDED.match(line)
        or BLOCKED.match(line) or PSTACK_NEWER.match(line) or QUESTION.match(line)
    )


def quiet_errors(lines):
    """Return why these user-visible lines are not a quiet report. Empty means they are."""
    errors = []
    options_open = False
    detail_open = False
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if event(line):
            options_open = line.startswith("? ")
            detail_open = line.startswith("✓ landed ")
            continue
        if OPTION.match(line):
            if not options_open:
                errors.append(f"{index}: numbered option without a question")
            detail_open = False
            continue
        if detail_open and line.startswith("  ") and line.strip():
            detail_open = False
            options_open = False
            continue
        errors.append(f"{index}: not a quiet line")
        options_open = False
        detail_open = False
    return errors


def narration_hits(text, phrases):
    return [phrase for phrase in phrases if phrase in text]


def score(cases=None, coordinator=None, ship=None, before=None, after=None):
    cases = cases or load_cases()
    coordinator = coordinator if coordinator is not None else open(COORDINATOR).read()
    ship = ship if ship is not None else open(SHIP).read()
    before = before if before is not None else open(BEFORE).read()
    after_lines = after if after is not None else lines_of(AFTER)
    say = say_section(coordinator)
    checks = []

    def add(ok, label):
        checks.append((ok, label))

    add(bool(say), "coordinator has a Say section")
    for line in cases["formats"]:
        add(line in say, f"Say contains {line}")
    for phrase in cases["forbids"]:
        add(phrase in say, f"Say forbids {phrase}")
    for phrase in cases["absent"]:
        add(phrase not in coordinator, f"coordinator no longer says {phrase}")
    for line in cases["ship_formats"]:
        add(line in ship, f"ship doc contains {line}")
    for phrase in cases["ship_absent"]:
        add(phrase not in ship, f"ship doc no longer says {phrase}")
    for phrase in cases["keeps"]:
        add(phrase in coordinator, f"safety text kept: {phrase}")
    add(after_lines == cases["after"], "after transcript matches the golden lines")
    add(quiet_errors(after_lines) == [], f"after transcript is quiet: {quiet_errors(after_lines)}")
    add(narration_hits("\n".join(after_lines), cases["narration"]) == [], "after transcript has no narration")
    before_errors = quiet_errors(lines_of_text(before))
    add(before_errors != [], "before transcript is not quiet")
    missed = [phrase for phrase in cases["narration"] if phrase not in before]
    add(missed == [], f"before transcript still shows the old narration: {missed}")
    return checks


def lines_of_text(text):
    return [line.rstrip("\n") for line in text.splitlines() if line.strip()]


def main():
    checks = score()
    failures = 0
    for ok, label in checks:
        if not ok:
            failures += 1
            print(f"FAIL {label}")
    total = len(checks)
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
