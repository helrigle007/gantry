#!/usr/bin/env python3
"""Compare coordinator overlap checks in Claude Code session transcripts.

A check starts at the assistant turn that runs `gantry overlap` (no --record)
and ends at the recorded verdict. The verdict is a later
`gantry overlap --record classified|answered`, or a top-level "verdict" of
continue or stop on that same command's JSON result. Quoted text is ignored,
so a worker spec that mentions the command does not open a check.

For each closed check this prints:

  turns        assistant requests from the overlap command through the verdict
  tokens       context added on that span, from context_attribution.py --replay
  wall seconds timestamp of the overlap command to timestamp of the verdict

Unclosed checks are listed and left out of the means. --replay rescales
measured growth by the shrink factors in template/kit/gantry_run.py. Peak and
end stay out of this table. Families the shrink map does not name keep their
measured growth.

  python3 scripts/overlap_efficiency.py --before <dir-or-jsonl>...
  python3 scripts/overlap_efficiency.py --before <before>... --after <after>...
"""

import argparse
import glob
import json
import os
import re
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import context_attribution as ca

OVERLAP = re.compile(r"(?:^|\s)(?:\S*/)?(?:gantry|gantry_kit\.py)\s+overlap\b(.*)$")
RECORD = re.compile(r"--record(?:=|\s+)(classified|answered|gathered)\b")
FLAG = re.compile(r"--([A-Za-z0-9-]+)(?:=|\s+)([^\s'\"]+|'[^']*'|\"[^\"]*\")")
SPLIT = re.compile(r"&&|\|\||[;&|\n]")
ENV = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def unquote(command):
    return QUOTED.sub(" ", command or "")


def segments(command):
    found = []
    for part in SPLIT.split(unquote(command)):
        part = ENV.sub("", part.strip())
        if part:
            found.append(part)
    return found


def overlap_role(command):
    role = None
    for segment in segments(command):
        match = OVERLAP.search(segment)
        if not match:
            continue
        record = RECORD.search(match.group(1))
        if record and record.group(1) in ("classified", "answered"):
            return "record"
        if record:
            continue
        role = "gather"
    return role


def flag_value(command, name):
    for flag, value in FLAG.findall(command or ""):
        if flag == name:
            return value.strip("'\"")
    return None


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


def verdict_in_text(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("verdict") in ("continue", "stop"):
        return data["verdict"]
    return None


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


def pop_open(open_checks, checkpoint):
    if checkpoint:
        for check in reversed(open_checks):
            if check["checkpoint"] == checkpoint:
                open_checks.remove(check)
                return check
        return None
    if not open_checks:
        return None
    return open_checks.pop()


def extract_checks(path):
    records = load_records(path)
    checks = []
    open_checks = []
    gathers = {}
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
                role = overlap_role(command)
                if role == "gather":
                    check = {
                        "file": base,
                        "checkpoint": flag_value(command, "checkpoint"),
                        "start_index": index,
                        "end_index": index,
                        "start_request": request,
                        "end_request": request,
                        "start_time": stamp,
                        "end_time": stamp,
                        "verdict": None,
                        "closed": False,
                    }
                    checks.append(check)
                    open_checks.append(check)
                    if part.get("id"):
                        gathers[part["id"]] = check
                elif role == "record":
                    target = pop_open(open_checks, flag_value(command, "checkpoint"))
                    if target is None:
                        continue
                    target["closed"] = True
                    target["end_index"] = index
                    target["end_request"] = request
                    target["end_time"] = stamp or target["end_time"]
                    target["verdict"] = flag_value(command, "verdict") or flag_value(command, "answer")
        elif kind == "user":
            for part in parts(record):
                tool_id, text = tool_result(part)
                check = gathers.get(tool_id)
                if check is None or check["closed"]:
                    continue
                verdict = verdict_in_text(text)
                if verdict is None:
                    continue
                check["closed"] = True
                check["end_index"] = index
                check["end_time"] = stamp or check["end_time"]
                check["verdict"] = verdict
                if check in open_checks:
                    open_checks.remove(check)
    return checks, records


def replay_shrink():
    sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
    from gantry_run import replay_shrink as shrink
    return shrink()


def replay_growth(records, shrink):
    handle = tempfile.NamedTemporaryFile("w", prefix="overlap-span-", suffix=".jsonl", delete=False)
    try:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.close()
        return ca.analyze(handle.name, shrink)["positive_growth"]
    finally:
        os.remove(handle.name)


def measure(check, records, shrink):
    row = {
        "file": check["file"],
        "checkpoint": check["checkpoint"],
        "verdict": check["verdict"],
        "closed": check["closed"],
        "turns": None,
        "tokens": None,
        "wall_seconds": None,
    }
    if not check["closed"]:
        return row
    span = records[check["start_index"]:check["end_index"] + 1]
    seen = []
    for offset, record in enumerate(span):
        if record.get("type") != "assistant":
            continue
        key = request_key(record, check["start_index"] + offset)
        if key not in seen:
            seen.append(key)
    row["turns"] = len(seen)
    row["tokens"] = replay_growth(span, shrink)
    if check["start_time"] and check["end_time"]:
        row["wall_seconds"] = (check["end_time"] - check["start_time"]).total_seconds()
    return row


def rows_of(paths, shrink):
    rows = []
    for path in paths:
        checks, records = extract_checks(path)
        for check in checks:
            rows.append(measure(check, records, shrink))
    return rows


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
    wall_mean, wall_median = pair("wall_seconds")
    return {
        "checks": len(closed),
        "unclosed": sum(1 for row in rows if not row["closed"]),
        "turns_mean": turns_mean,
        "turns_median": turns_median,
        "tokens_mean": tokens_mean,
        "tokens_median": tokens_median,
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
    ("checks", "checks", "d"),
    ("turns mean", "turns_mean", "f"),
    ("turns median", "turns_median", "f"),
    ("context tokens mean", "tokens_mean", "f"),
    ("context tokens median", "tokens_median", "f"),
    ("wall seconds mean", "wall_mean", "f"),
    ("wall seconds median", "wall_median", "f"),
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
            lines.append("  no overlap checks")
        for row in summary["rows"]:
            lines.append("  " + format_check(row))
        lines.append("")
    return "\n".join(lines)


def format_check(row):
    checkpoint = row["checkpoint"] or "-"
    if not row["closed"]:
        return f"{row['file']}  {checkpoint}  unclosed"
    wall = "n/a" if row["wall_seconds"] is None else f"{row['wall_seconds']:.1f}"
    verdict = row["verdict"] or "-"
    return (
        f"{row['file']}  {checkpoint}  turns {row['turns']}  "
        f"tokens {row['tokens']}  wall {wall}  verdict {verdict}"
    )


def collect(paths):
    found = []
    for path in paths:
        if os.path.isdir(path):
            found.extend(sorted(glob.glob(os.path.join(path, "*.jsonl"))))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise SystemExit(f"overlap efficiency: no such transcript {path}")
    if not found:
        raise SystemExit("overlap efficiency: no transcripts")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", nargs="+", required=True)
    parser.add_argument("--after", nargs="+")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)
    try:
        before_paths = collect(args.before)
        after_paths = collect(args.after) if args.after else None
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    shrink = replay_shrink()
    before = summarize(rows_of(before_paths, shrink))
    after = summarize(rows_of(after_paths, shrink)) if after_paths else None
    sys.stdout.write(render(before, after))
    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump({"before": before, "after": after}, handle, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
