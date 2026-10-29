#!/usr/bin/env python3
"""Poteto-mode harness: pstack resolution, the adapter, and the missing-plugin stop."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "template", "kit")
sys.path.insert(0, KIT)
import gantry_kit as kit

COORDINATOR = os.path.join(KIT, "gantry-coordinator.md")
ADAPTER = os.path.join(KIT, "gantry-poteto.md")
SHIP = os.path.join(KIT, "gantry-ship.md")


def skill_at(home, market, version, body=None):
    root = os.path.join(home, ".claude", "plugins", "cache", market, "pstack", version)
    skill_dir = os.path.join(root, "skills", "poteto-mode")
    os.makedirs(skill_dir)
    if body is not None:
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as handle:
            handle.write(body)
    return root


def test_missing_and_newest(tmp):
    assert kit.pstack_root(tmp) is None
    assert kit.pstack_root(tmp, need_skill=True) is None
    stub = skill_at(tmp, "x", "0")
    assert kit.pstack_root(tmp) == stub
    assert kit.pstack_root(tmp, need_skill=True) is None
    old = skill_at(tmp, "cursor-plugins", "0.9.45", "# old\n")
    new = skill_at(tmp, "other-market", "0.15.36", "# new\n")
    assert kit.pstack_root(tmp, need_skill=True) == new
    assert kit.pstack_root(tmp) == new
    tied = skill_at(tmp, "cursor-plugins", "0.15.36", "# tied\n")
    assert kit.pstack_root(tmp, need_skill=True) == tied
    assert old != new
    saved = os.environ.get(kit.PSTACK_ENV)
    try:
        os.environ[kit.PSTACK_ENV] = "/old"
        assert kit.bind_pstack(os.path.join(tmp, "empty")) is None
        assert kit.PSTACK_ENV not in os.environ
        bound = kit.bind_pstack(tmp)
        assert bound == tied
        assert os.environ[kit.PSTACK_ENV] == tied
    finally:
        if saved is None:
            os.environ.pop(kit.PSTACK_ENV, None)
        else:
            os.environ[kit.PSTACK_ENV] = saved


def test_newer_than_tested(tmp):
    assert kit.PSTACK_TESTED == "0.15.36"
    assert kit.pstack_notice("0.15.36") is None
    assert kit.pstack_notice("0.9.45") is None
    assert kit.pstack_notice("0.15.35") is None
    newer = kit.pstack_notice("0.16.0")
    assert newer == "! pstack 0.16.0 newer than tested 0.15.36 · continuing"
    assert kit.pstack_notice("0.15.37") == "! pstack 0.15.37 newer than tested 0.15.36 · continuing"
    sys.path.insert(0, os.path.join(ROOT, "evals", "quiet"))
    import score
    assert score.quiet_errors([newer, "▸ started slug-helper · 1 worker · overlap clear"]) == []
    assert score.PSTACK_NEWER.match(newer)
    skill_at(tmp, "cursor-plugins", "0.15.36", "# same\n")
    saved_notice = os.environ.get(kit.PSTACK_NOTICE_ENV)
    try:
        os.environ[kit.PSTACK_NOTICE_ENV] = "stale"
        assert kit.bind_pstack(tmp) is not None
        assert kit.PSTACK_NOTICE_ENV not in os.environ
        skill_at(tmp, "cursor-plugins", "0.16.0", "# newer\n")
        assert os.path.basename(kit.bind_pstack(tmp)) == "0.16.0"
        assert os.environ[kit.PSTACK_NOTICE_ENV] == "! pstack 0.16.0 newer than tested 0.15.36 · continuing"
    finally:
        if saved_notice is None:
            os.environ.pop(kit.PSTACK_NOTICE_ENV, None)
        else:
            os.environ[kit.PSTACK_NOTICE_ENV] = saved_notice


def test_adapter_and_prompt():
    coordinator = open(COORDINATOR).read()
    adapter = open(ADAPTER).read()
    ship = open(SHIP).read()
    assert kit.BLOCKED_PSTACK in coordinator
    assert kit.PSTACK_TESTED in coordinator
    assert "! pstack <installed> newer than tested 0.15.36 · continuing" in coordinator
    assert "GANTRY_PSTACK_NOTICE" in coordinator
    assert "Do not run `run-create`" in coordinator
    assert "Do not name a playbook yourself" in coordinator
    assert "gantry-poteto.md" in coordinator
    assert "You are a poteto-agent" in coordinator
    assert "never spawn a poteto-agent to run the playbook" in coordinator
    assert "Never pass /poteto-mode" in coordinator
    for line in ("Charter:", "Ownership:", "Base:", "Proof:"):
        assert line in coordinator
    for gone in (
        "You classify the task",
        "Name one playbook",
        "Skip them for a tiny mechanical change",
        "Use full `/poteto-mode` only",
        "/poteto-mode\nPlaybook:",
    ):
        assert gone not in coordinator, gone
    for phrase in (
        "worker-start",
        "gantry wait",
        "poteto-agent",
        "orchestrate.md",
        "SKILL.md",
        "GANTRY_PSTACK",
        kit.BLOCKED_PSTACK,
        "no second classifier",
        "Charter",
        "Ownership",
        "Base",
        "Proof",
        "Never the Agent tool",
    ):
        assert phrase in adapter, phrase
    assert "You are a poteto-agent" in ship
    assert "playbooks/shipping.md" in ship
    assert "playbooks/babysit.md" in ship


def main():
    import tempfile
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            test_missing_and_newest(tmp)
            checks.append((True, "pstack resolution"))
        except Exception as exc:
            checks.append((False, f"pstack resolution: {exc}"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            test_newer_than_tested(tmp)
            checks.append((True, "newer pstack warns once"))
        except Exception as exc:
            checks.append((False, f"newer pstack warns once: {exc}"))
    try:
        test_adapter_and_prompt()
        checks.append((True, "adapter and prompt"))
    except Exception as exc:
        checks.append((False, f"adapter and prompt: {exc}"))
    failed = [label for ok, label in checks if not ok]
    print(f"{len(checks) - len(failed)}/{len(checks)} passed")
    for label in failed:
        print(label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
