#!/usr/bin/env python3
"""Score a duplicate-work classifier against evals/overlap/cases.json.

A case passes only when every candidate label matches and the verdict matches.
That exact-match rate is passed cases / cases. classify() returns duplicate,
related, or unrelated. verdict_of() returns continue or stop. stop means ask:
the coordinator stops and asks the user. A verdict function may return ask
directly.

  python3 evals/overlap/score.py
  python3 evals/overlap/score.py --classify gantry_kit:classify --verdict gantry_kit:verdict_of
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KIT = os.path.join(ROOT, "template", "kit")
DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.json")
LABELS = ("duplicate", "related", "unrelated")
VERDICTS = ("continue", "ask")
HIDDEN = ("label", "note")


class CaseError(ValueError):
    pass


def as_ask(value):
    if value == "continue":
        return "continue"
    if value in ("stop", "ask", "flag"):
        return "ask"
    return value


def load_cases(path=None):
    path = path or DEFAULT_CASES
    with open(path) as handle:
        data = json.load(handle)
    cases = data["cases"] if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise CaseError(f"{path} has no cases")
    seen = set()
    for case in cases:
        check_case(case)
        if case["id"] in seen:
            raise CaseError(f"duplicate case id {case['id']}")
        seen.add(case["id"])
    return cases


def check_case(case):
    if not isinstance(case, dict):
        raise CaseError("a case is not an object")
    ident = case.get("id") or "<missing id>"
    if not isinstance(case.get("id"), str) or not case["id"]:
        raise CaseError("a case is missing an id")
    if not isinstance(case.get("task"), str) or not case["task"].strip():
        raise CaseError(f"{ident} is missing a task description")
    if "intent" not in case or not isinstance(case["intent"], str):
        raise CaseError(f"{ident} is missing an intent")
    if not isinstance(case.get("files"), list):
        raise CaseError(f"{ident} is missing planned files")
    if case.get("verdict") not in VERDICTS:
        raise CaseError(f"{ident} verdict must be continue or ask")
    candidates = case.get("candidates")
    if not isinstance(candidates, list):
        raise CaseError(f"{ident} is missing candidates")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise CaseError(f"{ident} has a candidate that is not an object")
        if not candidate.get("kind") or not candidate.get("id"):
            raise CaseError(f"{ident} has a candidate without kind and id")
        if "files" not in candidate or not isinstance(candidate["files"], list):
            raise CaseError(f"{ident} candidate {candidate.get('id')} is missing files")
        if candidate.get("label") not in LABELS:
            raise CaseError(f"{ident} candidate {candidate.get('id')} has a bad label")


def visible(candidate):
    return {key: value for key, value in candidate.items() if key not in HIDDEN}


def score_cases(cases, classify, verdict):
    results = []
    for case in cases:
        check_case(case)
        expected_labels = [candidate["label"] for candidate in case["candidates"]]
        got_labels = []
        mismatches = []
        error = None
        try:
            for candidate in case["candidates"]:
                got = classify(case["intent"], case["files"], visible(candidate))
                got_labels.append(got)
                if got != candidate["label"]:
                    mismatches.append(
                        f"  {candidate['kind']} {candidate['id']}: expected {candidate['label']}, got {got}"
                    )
        except Exception as exc:
            error = f"  classify raised {exc.__class__.__name__}: {exc}"
        label_ok = error is None and got_labels == expected_labels
        verdict_note = None
        end_note = None
        try:
            isolated = as_ask(verdict(expected_labels))
        except Exception as exc:
            isolated = None
            verdict_note = f"  verdict_of(expected labels) raised {exc.__class__.__name__}: {exc}"
        else:
            if isolated != case["verdict"]:
                verdict_note = f"  verdict_of(expected labels): expected {case['verdict']}, got {isolated}"
        if error is None:
            try:
                end = as_ask(verdict(got_labels))
            except Exception as exc:
                end = None
                end_note = f"  verdict raised {exc.__class__.__name__}: {exc}"
            else:
                if end != case["verdict"]:
                    end_note = f"  verdict: expected {case['verdict']}, got {end}"
        else:
            end = None
        passed = label_ok and end == case["verdict"] and isolated == case["verdict"]
        results.append({
            "id": case["id"],
            "passed": passed,
            "label_ok": label_ok,
            "verdict_ok": isolated == case["verdict"],
            "mismatches": mismatches,
            "error": error,
            "verdict_note": verdict_note,
            "end_note": end_note,
        })
    total = len(results)
    passed = sum(1 for row in results if row["passed"])
    label_passed = sum(1 for row in results if row["label_ok"])
    verdict_passed = sum(1 for row in results if row["verdict_ok"])
    return {
        "cases": total,
        "passed": passed,
        "label_passed": label_passed,
        "verdict_passed": verdict_passed,
        "exact_match": (passed / total) if total else 0.0,
        "results": results,
    }


def format_report(report):
    width = max(len(row["id"]) for row in report["results"])
    lines = []
    for row in report["results"]:
        state = "pass" if row["passed"] else "fail"
        lines.append(f"{row['id']:<{width}}  {state}")
        if row["error"]:
            lines.append(row["error"])
        for line in row["mismatches"]:
            lines.append(line)
        if row["end_note"]:
            lines.append(row["end_note"])
        if row["verdict_note"]:
            lines.append(row["verdict_note"])
    failed = [row["id"] for row in report["results"] if not row["passed"]]
    lines.append("")
    lines.append(
        f"{report['passed']}/{report['cases']} passed  exact-match {report['exact_match']:.3f}"
    )
    lines.append(
        f"classify {report['label_passed']}/{report['cases']}  "
        f"verdict_of {report['verdict_passed']}/{report['cases']}"
    )
    lines.append("failures: " + (", ".join(failed) if failed else "none"))
    lines.append("")
    return "\n".join(lines)


def resolve(spec):
    if spec.count(":") != 1:
        raise CaseError(f"expected module:function, got {spec}")
    mod_name, func_name = spec.split(":")
    if mod_name == "gantry_kit":
        sys.path.insert(0, KIT)
    try:
        module = __import__(mod_name)
    except ImportError as exc:
        raise CaseError(f"cannot import {mod_name}: {exc}") from exc
    try:
        return getattr(module, func_name)
    except AttributeError as exc:
        raise CaseError(f"{spec} was not found") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--classify", default="gantry_kit:classify")
    parser.add_argument("--verdict", default="gantry_kit:verdict_of")
    args = parser.parse_args(argv)
    try:
        report = score_cases(load_cases(args.cases), resolve(args.classify), resolve(args.verdict))
    except CaseError as exc:
        print(f"overlap score: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(format_report(report))
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
