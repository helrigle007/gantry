#!/usr/bin/env python3
"""Attribute context growth in a Claude Code session transcript.

Context size is message.usage input_tokens + cache_creation_input_tokens +
cache_read_input_tokens, once per requestId. Growth until the next request is
scaled across the assistant text, tool-call inputs, tool results, user text,
and rendered attachments recorded in between, by a character estimate. A drop
of more than 20000 tokens is a compaction, not negative attribution. Content
with no following usage record is charged at the character estimate.

A Read of a background-task output or a persisted tool-result file is charged
to the Bash command that produced it. Bash output is split evenly across the
command families present in the command string.

  scripts/context_attribution.py [--json path] [--top N] [--replay compact] session.jsonl ...

--replay compact rescales measured growth by the output shrink factors in
template/kit/gantry_run.py. Peak and end stay the measured window.
"""

import argparse
import json
import os
import re
import sys

COMPACT_DROP = 20000
CHARS_PER_TOKEN = 4

FAMILIES = (
    "gantry-mailbox wait",
    "worker-read",
    "worker-show",
    "worker-list",
    "task-list",
    "gate-list",
    "gate-create",
    "gh pr view",
    "gh pr checks",
    "gh pr list",
    "orca skills get",
    "briefs/specs",
    "task echo",
    "task-create",
    "worker-start",
    "worker-release",
    "terminal read",
    "git",
    "orca other",
    "bash other",
    "Read skill/guide",
    "Read repo file",
    "Read other",
    "assistant prose",
    "user text",
    "attachment",
    "unattributed",
)

FAMILY_RULES = (
    ("gantry-mailbox wait", re.compile(r"gantry-mailbox\b.*\bwait\b|\bwait\b.*gantry-mailbox")),
    ("worker-read", re.compile(r"\bworker-read\b")),
    ("worker-show", re.compile(r"\bworker-show\b")),
    ("worker-list", re.compile(r"\bworker-list\b")),
    ("task-list", re.compile(r"\btask-list\b")),
    ("gate-list", re.compile(r"\bgate-list\b")),
    ("gate-create", re.compile(r"\bgate-create\b")),
    ("gh pr view", re.compile(r"\bgh\s+pr\s+view\b")),
    ("gh pr checks", re.compile(r"\bgh\s+pr\s+checks\b")),
    ("gh pr list", re.compile(r"\bgh\s+pr\s+list\b")),
    ("orca skills get", re.compile(r"\bskills\s+get\b")),
    ("task-create", re.compile(r"\btask-create\b")),
    ("worker-start", re.compile(r"\bworker-start\b")),
    ("worker-release", re.compile(r"\bworker-release\b")),
    ("terminal read", re.compile(r"\bterminal\s+read\b")),
    ("git", re.compile(r"(^|[\s;&|`(])git(\s|$)")),
    ("orca other", re.compile(r"(^|[\s;&|`(])orca(\s|$)")),
)

BG_OUTPUT = re.compile(r"Output is being written to:\s*(\S+)")
PERSISTED = re.compile(r"Full output saved to:\s*(\S+)")
PERSISTED_SIZE = re.compile(r"Output too large \(([0-9.]+)\s*(KB|MB|B)\)")
SPEC = re.compile(r"--spec\b")


def estimate(text):
    if not text:
        return 0
    return estimate_chars(len(text))


def estimate_chars(n):
    if n <= 0:
        return 0
    return max(1, n // CHARS_PER_TOKEN)


def text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if "text" in part:
                    parts.append(str(part.get("text") or ""))
                elif "content" in part:
                    parts.append(text_of(part.get("content")))
                else:
                    parts.append(json.dumps(part, default=str))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, default=str)
    return str(content)


def bash_families(command):
    command = command or ""
    spec = SPEC.search(command)
    head = command[:spec.start()] if spec else command
    found = [name for name, pattern in FAMILY_RULES if pattern.search(head)]
    if any(name != "orca other" for name in found):
        found = [name for name in found if name != "orca other"]
    if not found:
        return ["bash other"]
    if SPEC.search(command or "") and ("task-create" in found or "worker-start" in found):
        found = ["briefs/specs" if name in ("task-create", "worker-start") else name for name in found]
    return found


def read_family(path):
    path = path or ""
    base = os.path.basename(path)
    if "tool-results" in path or path.endswith(".output"):
        return "Read other"
    if base in ("SKILL.md",) or "/skills/" in path or path.endswith("gantry-coordinator.md"):
        return "Read skill/guide"
    if path.endswith("gantry.json") or path.endswith("/SKILL.md"):
        return "Read skill/guide"
    if "/Repos/" in path or "/src/" in path or "/documentation/" in path:
        return "Read repo file"
    return "Read other"


def context_of(usage):
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )


class Bucket:
    def __init__(self):
        self.tokens = 0.0
        self.raw = 0
        self.calls = 0

    def add(self, tokens, raw, calls=1):
        self.tokens += tokens
        self.raw += raw
        self.calls += calls


def empty_buckets():
    return {name: Bucket() for name in FAMILIES}


def add_share(buckets, families, tokens):
    n = len(families)
    for name in families:
        ensure(buckets, name).add(tokens / n, 0, 0)


class Request:
    def __init__(self, request_id, ctx):
        self.request_id = request_id
        self.ctx = ctx
        self.items = []
        self.following = []
        self.worker_start = False

    def add_item(self, families, raw, worker_start=False):
        self.items.append((families, raw))
        self.worker_start = self.worker_start or worker_start


def content_items(content):
    if isinstance(content, str):
        return [("user text", content)]
    if not isinstance(content, list):
        return []
    items = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            items.append(("text", part.get("text") or ""))
        elif kind == "thinking":
            items.append(("thinking", part.get("thinking") or part.get("text") or ""))
        elif kind == "tool_use":
            items.append(("tool_use", part))
        elif kind == "tool_result":
            items.append(("tool_result", part))
    return items


def load_events(path):
    requests = []
    by_id = {}
    order = []
    uses = {}
    producers = {}
    persisted_chars = {}
    pending = []

    def flush_pending(req):
        if req is None:
            return
        req.following.extend(pending)
        pending.clear()

    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            if kind == "assistant":
                message = rec.get("message") or {}
                usage = message.get("usage") or {}
                if "input_tokens" not in usage:
                    continue
                if context_of(usage) == 0 and not usage.get("output_tokens"):
                    continue
                rid = rec.get("requestId") or rec.get("uuid")
                req = by_id.get(rid)
                if req is None:
                    flush_pending(requests[-1] if requests else None)
                    req = Request(rid, context_of(usage))
                    by_id[rid] = req
                    requests.append(req)
                    order.append(req)
                for label, payload in content_items(message.get("content")):
                    if label == "tool_use":
                        tool = payload.get("name") or ""
                        inp = payload.get("input") or {}
                        uses[payload.get("id")] = (tool, inp)
                        if tool == "Bash":
                            command = inp.get("command") or ""
                            families = bash_families(command)
                            started = bool(re.search(r"\bworker-start\b", command))
                            if SPEC.search(command):
                                spec_at = command.find("--spec")
                                req.add_item(["briefs/specs"], estimate(command[spec_at:]), started)
                                other = [name for name in families if name != "briefs/specs"]
                                rest_raw = estimate(command[:spec_at])
                                if rest_raw and other:
                                    req.add_item(other, rest_raw, False)
                            else:
                                req.add_item(families, estimate(command), started)
                        elif tool == "Read":
                            req.add_item([read_family(inp.get("file_path") or "")], estimate(json.dumps(inp)))
                        else:
                            req.add_item([f"tool {tool}" if tool else "tool other"], estimate(json.dumps(inp, default=str)))
                    elif label == "text" and payload.strip():
                        req.add_item(["assistant prose"], estimate(payload))
            elif kind == "user":
                message = rec.get("message") or {}
                for label, payload in content_items(message.get("content")):
                    if label == "tool_result":
                        body = text_of(payload.get("content"))
                        tool, inp = uses.get(payload.get("tool_use_id"), ("", {}))
                        if tool == "Bash":
                            command = inp.get("command") or ""
                            families = bash_families(command)
                            if SPEC.search(command):
                                families = ["task echo" if name == "briefs/specs" else name for name in families]
                            for path in BG_OUTPUT.findall(body) + PERSISTED.findall(body):
                                producers[path.rstrip(".,")] = families
                            sized = PERSISTED_SIZE.search(body)
                            if sized:
                                amount = float(sized.group(1))
                                unit = sized.group(2)
                                chars = int(amount * (1 if unit == "B" else 1024 if unit == "KB" else 1024 * 1024))
                                for path in PERSISTED.findall(body):
                                    persisted_chars[path.rstrip(".,")] = chars
                            pending.append((families, estimate(body)))
                        elif tool == "Read":
                            path = inp.get("file_path") or ""
                            families = producers.get(path) or [read_family(path)]
                            chars = len(body)
                            if path in persisted_chars:
                                chars = max(chars, persisted_chars[path])
                            pending.append((families, estimate_chars(chars)))
                        elif tool:
                            pending.append(([f"tool {tool}"], estimate(body)))
                        else:
                            pending.append((["unattributed"], estimate(body)))
                    elif label == "text" and isinstance(payload, str) and payload.strip():
                        pending.append((["user text"], estimate(payload)))
                if isinstance(message.get("content"), str) and message["content"].strip():
                    pending.append((["user text"], estimate(message["content"])))
            elif kind == "attachment" and (rec.get("rendered") is not None or rec.get("renderedRole")):
                raw = estimate(text_of((rec.get("attachment") or {}).get("content") or rec.get("attachment")))
                if raw:
                    pending.append((["attachment"], raw))
    flush_pending(requests[-1] if requests else None)
    return requests


def ensure(buckets, name):
    if name not in buckets:
        buckets[name] = Bucket()
    return buckets[name]


def _new_wave(index, ctx):
    return {
        "index": index + 1,
        "start": ctx,
        "peak": ctx,
        "end": ctx,
        "added": 0.0,
        "requests": 0,
        "worker_starts": 0,
        "in_burst": False,
        "since_start": 0,
    }


def attribute_clean(requests, shrink=None):
    shrink = shrink or {}
    buckets = empty_buckets()
    waves = []
    compactions = 0
    positive = 0.0
    baseline = requests[0].ctx if requests else 0
    peak = baseline
    seen = set()

    def request_factors(pieces):
        factors = []
        for families, _raw in pieces:
            piece = []
            for name in families:
                factor = shrink.get(name, 1)
                if factor == "once":
                    factor = 0 if name in seen else 1
                piece.append(factor)
            factors.append(min(piece) if piece else 1)
        for families, _raw in pieces:
            for name in families:
                if shrink.get(name) == "once":
                    seen.add(name)
        return factors

    def close(wave, end_ctx):
        wave["end"] = end_ctx
        wave["added"] = round(wave["added"])
        waves.append({k: wave[k] for k in ("index", "start", "peak", "end", "added", "requests", "worker_starts")})

    wave = _new_wave(0, baseline)
    for index, req in enumerate(requests):
        peak = max(peak, req.ctx)
        wave["peak"] = max(wave["peak"], req.ctx)
        wave["requests"] += 1
        if req.worker_start:
            if wave["worker_starts"] and not wave["in_burst"]:
                close(wave, req.ctx)
                wave = _new_wave(len(waves), req.ctx)
                wave["requests"] = 1
            wave["worker_starts"] += 1
            wave["in_burst"] = True
            wave["since_start"] = 0
        else:
            wave["since_start"] += 1
            if wave["since_start"] > 1:
                wave["in_burst"] = False
        nxt = requests[index + 1] if index + 1 < len(requests) else None
        pieces = [(families, raw) for families, raw in (req.items + req.following) if raw > 0]
        for families, raw in pieces:
            for name in families:
                ensure(buckets, name).raw += raw / len(families)
            ensure(buckets, families[0]).calls += 1
        if nxt is None:
            continue
        delta = nxt.ctx - req.ctx
        wave["peak"] = max(wave["peak"], nxt.ctx)
        if delta <= -COMPACT_DROP:
            compactions += 1
            close(wave, req.ctx)
            wave = _new_wave(len(waves), nxt.ctx)
            continue
        growth = max(delta, 0)
        factors = request_factors(pieces)
        weight = sum(raw for _, raw in pieces)
        new_weight = sum(raw * factor for (_, raw), factor in zip(pieces, factors))
        projected = growth * (new_weight / weight) if growth and weight else growth
        positive += projected
        wave["added"] += projected
        if projected and new_weight:
            for (families, raw), factor in zip(pieces, factors):
                part = raw * factor
                if part:
                    add_share(buckets, families, projected * (part / new_weight))
        elif projected:
            buckets["unattributed"].add(projected, 0, 1)
    if requests:
        close(wave, requests[-1].ctx)
    return {
        "requests": len(requests),
        "baseline": baseline,
        "peak": peak,
        "end": requests[-1].ctx if requests else 0,
        "compactions": compactions,
        "positive_growth": round(positive),
        "buckets": buckets,
        "waves": waves,
    }


def rows_of(buckets):
    rows = []
    for name, bucket in buckets.items():
        if bucket.tokens <= 0 and bucket.raw <= 0 and bucket.calls <= 0:
            continue
        rows.append({
            "family": name,
            "tokens": round(bucket.tokens),
            "raw_tokens": round(bucket.raw),
            "calls": bucket.calls,
        })
    rows.sort(key=lambda row: (-row["tokens"], -row["raw_tokens"], row["family"]))
    return rows


def analyze(path, shrink=None):
    requests = load_events(path)
    result = attribute_clean(requests, shrink)
    result["file"] = os.path.basename(path)
    result["families"] = rows_of(result.pop("buckets"))
    result["cycles"] = cycles_of(result["waves"])
    result["replay"] = bool(shrink)
    return result


def cycles_of(waves):
    cycles = []
    current = None
    for wave in waves:
        if wave["requests"] == 0:
            continue
        if current is None or wave["start"] + COMPACT_DROP < current["end"]:
            if current:
                cycles.append(current)
            current = {
                "start": wave["start"],
                "peak": wave["peak"],
                "end": wave["end"],
                "added": wave["added"],
                "waves": 1,
            }
        else:
            current["peak"] = max(current["peak"], wave["peak"])
            current["end"] = wave["end"]
            current["added"] += wave["added"]
            current["waves"] += 1
    if current:
        cycles.append(current)
    return cycles


def render(result, top):
    growth = "projected growth" if result.get("replay") else "positive growth"
    lines = [
        f"# {result['file']}",
        f"requests {result['requests']}  baseline {result['baseline']}  peak {result['peak']}  "
        f"end {result['end']}  compactions {result['compactions']}  "
        f"{growth} {result['positive_growth']}",
        "",
    ]
    if result.get("replay"):
        lines.append("replay scales measured growth by output shrink factors; peak and end stay the measured window")
        lines.append("")
    lines += [
        f"{'tokens':>10}  {'raw':>10}  {'calls':>6}  family",
    ]
    shown = [row for row in result["families"] if row["tokens"] or row["raw_tokens"]][:top]
    for row in shown:
        lines.append(f"{row['tokens']:10}  {row['raw_tokens']:10}  {row['calls']:6}  {row['family']}")
    lines.append("")
    lines.append("compaction cycles (waves between resets of the window):")
    lines.append(f"{'cycle':>6}  {'start':>8}  {'peak':>8}  {'end':>8}  {'added':>8}  waves")
    for index, cycle in enumerate(result["cycles"], 1):
        lines.append(
            f"{index:6}  {cycle['start']:8}  {cycle['peak']:8}  {cycle['end']:8}  "
            f"{cycle['added']:8}  {cycle['waves']}"
        )
    lines.append("")
    lines.append(f"{'wave':>5}  {'start':>8}  {'peak':>8}  {'end':>8}  {'added':>8}  {'reqs':>5}  starts")
    for wave in result["waves"]:
        if wave["requests"] == 0:
            continue
        lines.append(
            f"{wave['index']:5}  {wave['start']:8}  {wave['peak']:8}  {wave['end']:8}  "
            f"{wave['added']:8}  {wave['requests']:5}  {wave['worker_starts']}"
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--replay", choices=("compact",))
    args = parser.parse_args(argv)
    shrink = None
    if args.replay:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(root, "template", "kit"))
        from gantry_run import replay_shrink
        shrink = replay_shrink()
    results = [analyze(path, shrink) for path in args.transcripts]
    for result in results:
        print(render(result, args.top))
        print()
    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump(results, handle, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
