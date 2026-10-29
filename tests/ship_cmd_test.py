#!/usr/bin/env python3
"""gantry ship and gantry cleanup. gh and orca are faked. git status and worktrees are real."""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "template", "kit"))
import gantry_kit as kit

ORIGIN = "https://github.com/example-org/example-repo.git"
SLUG = "example-org/example-repo"
PR_URL = f"https://github.com/{SLUG}/pull/9"
MERGE = ["gh", "pr", "merge", "9", "--repo", SLUG, "--squash", "--match-head-commit", "abcdef1"]


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def run_git(*args, check=True):
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


class World:
    def __init__(self, tmp):
        self.tmp = os.path.realpath(tmp)
        self.repo = os.path.join(self.tmp, "repo")
        self.bare = os.path.join(self.tmp, "bare.git")
        self.wt = os.path.join(self.tmp, "wt-slug")
        self.view = {
            "number": 9,
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "feat/slug",
            "headRefOid": "abcdef1",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
            "url": PR_URL,
            "isDraft": False,
        }
        self.opens = []
        self.view_queue = []
        self.workflows = {"total_count": 0, "workflows": []}
        self.required = None
        self.rules = []
        self.workflows_error = False
        self.required_error = None
        self.rules_error = False
        self.sleeps = []
        self.merged_heads = set()
        self.merge_code = 0
        self.workers = []
        self.fail_update = False
        self.task_update_argv = None
        self.close_code = 0
        self.trace = []
        self.git_calls = []
        self.real_invoke = kit.invoke
        run_git("init", "-q", "-b", "main", self.repo)
        git(self.repo, "config", "user.name", "example")
        git(self.repo, "config", "user.email", "dev@example.com")
        git(self.repo, "commit", "--allow-empty", "-m", "seed")
        git(self.repo, "remote", "add", "origin", ORIGIN)
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git(self.repo, "checkout", "-q", "-b", "feat/slug")
        git(self.repo, "commit", "--allow-empty", "-m", "slug")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "worktree", "add", "-q", self.wt, "feat/slug")
        run_git("init", "-q", "--bare", self.bare)
        run_git("--git-dir", self.bare, "fetch", "-q", self.repo, "refs/heads/*:refs/heads/*")
        self.set_auto(True)
        self.journal("task_slug", PR_URL, "feat/slug")
        self.worker()

    def set_auto(self, value):
        path = os.path.join(self.repo, ".git", "gantry.json")
        with open(path, "w") as handle:
            json.dump({"autoMerge": value}, handle)

    def journal(self, task, pr, branch, run="run_1"):
        path = os.path.join(self.tmp, "gantry-mailbox", run, "journal.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = {
            "id": "msg_done",
            "type": "worker_done",
            "body": f"Ready to ship. task={task} pr={pr} branch={branch}",
            "payload": {"taskId": task},
        }
        with open(path, "w") as handle:
            handle.write(json.dumps(row) + "\n")

    def clear_journal(self):
        path = os.path.join(self.tmp, "gantry-mailbox", "run_1", "journal.jsonl")
        if os.path.exists(path):
            os.remove(path)

    def worker(self, status="completed", path=None, task="task_slug"):
        self.workers = [{
            "taskId": task,
            "dispatchStatus": status,
            "agentTerminalHandle": "term_impl",
            "dispatchId": "ctx_impl",
            "worktreePath": self.wt if path is None else path,
        }]

    def ref_exists(self, branch):
        proc = run_git("--git-dir", self.bare, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
        return proc.returncode == 0

    def invoke(self, argv, cwd=None, timeout=30):
        if argv and argv[0] == "git":
            self.git_calls.append(list(argv))
            if "push" in argv and "--delete" in argv:
                return self.delete_ref(argv)
            if "ls-remote" in argv:
                return self.ls_remote(argv)
            if "worktree" in argv and "remove" in argv:
                self.trace.append("remove")
                assert "--force" not in argv and "-f" not in argv, argv
        if argv and argv[0] == "gh":
            return self.gh(argv)
        if argv and argv[0] == "orca":
            return self.orca(argv)
        return self.real_invoke(argv, cwd=cwd, timeout=timeout)

    def delete_ref(self, argv):
        self.trace.append("delete")
        assert "--force" not in argv and "-f" not in argv, argv
        assert not any(token.startswith("+") for token in argv), argv
        branch = argv[-1]
        if not self.ref_exists(branch):
            return 1, "", "remote ref does not exist"
        proc = run_git("--git-dir", self.bare, "update-ref", "-d", f"refs/heads/{branch}", check=False)
        if proc.returncode != 0:
            return proc.returncode, proc.stdout, proc.stderr
        return 0, "", ""

    def ls_remote(self, argv):
        self.trace.append("ls-remote")
        branch = argv[-1]
        if self.ref_exists(branch):
            return 0, f"abcdef1\trefs/heads/{branch}\n", ""
        return 2, "", ""

    def gh(self, argv):
        if argv[1:3] == ["pr", "view"]:
            self.trace.append("view")
            if self.view_queue:
                self.view = self.view_queue.pop(0)
            return 0, json.dumps(self.view), ""
        if argv[1:2] == ["api"]:
            return self.api(argv[2] if len(argv) > 2 else "")
        if argv[1:3] == ["pr", "list"]:
            state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
            if state == "merged":
                self.trace.append("list-merged")
                head = argv[argv.index("--head") + 1] if "--head" in argv else ""
                rows = [{"number": 9}] if head in self.merged_heads else []
                return 0, json.dumps(rows), ""
            if state == "open":
                self.trace.append("list-open")
                return 0, json.dumps(self.opens), ""
            return 0, "[]", ""
        if argv[1:3] == ["pr", "merge"]:
            self.trace.append("merge")
            if self.merge_code != 0:
                return self.merge_code, "", "merge failed"
            return 0, "", ""
        return 1, "", "unexpected gh"

    def api(self, path):
        self.trace.append("api")
        if path.endswith("/actions/workflows"):
            if self.workflows_error:
                return 1, "", "HTTP 500"
            return 0, json.dumps(self.workflows), ""
        if path.endswith("/protection/required_status_checks"):
            if self.required_error:
                return 1, "", self.required_error
            if self.required is None:
                return 1, "", "gh: Branch not protected (HTTP 404)"
            return 0, json.dumps(self.required), ""
        if "/rules/branches/" in path:
            if self.rules_error:
                text = self.rules_error if isinstance(self.rules_error, str) else "HTTP 500"
                return 1, "", text
            return 0, json.dumps(self.rules), ""
        return 1, "", "unexpected api"

    def orca(self, argv):
        if argv[1:3] == ["orchestration", "task-update"]:
            self.trace.append("task-update")
            self.task_update_argv = list(argv)
            if self.fail_update:
                return 0, json.dumps({"ok": False, "error": "task-update failed"}), ""
            return 0, json.dumps({"ok": True, "result": {}}), ""
        if argv[1:3] == ["orchestration", "worker-release"]:
            self.trace.append("release")
            return 0, json.dumps({"ok": True, "result": {}}), ""
        if argv[1:3] == ["orchestration", "worker-list"]:
            return 0, json.dumps({"ok": True, "result": {"workers": self.workers, "page": {}}}), ""
        if argv[1:3] == ["orchestration", "task-list"]:
            return 0, json.dumps({"ok": True, "result": {"tasks": [], "page": {}}}), ""
        if argv[1:3] == ["orchestration", "run-current"]:
            return 0, json.dumps({"ok": True, "result": {"run": {"id": "run_1"}}}), ""
        if argv[1:3] == ["orchestration", "run-list"]:
            return 0, json.dumps({"ok": True, "result": {"runs": [{"id": "run_1"}]}}), ""
        if argv[1:3] == ["terminal", "close"]:
            self.trace.append("close")
            if self.close_code != 0:
                return self.close_code, "", "terminal close failed"
            return 0, json.dumps({"ok": True}), ""
        return 1, "", "unexpected orca"

    def run(self, argv, cwd=None):
        saved_invoke = kit.invoke
        saved_pause = kit.pause
        saved_cwd = os.getcwd()
        saved_tmp = os.environ.get("TMPDIR")
        saved_handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        kit.invoke = self.invoke
        kit.pause = lambda seconds: self.sleeps.append(seconds)
        os.environ["TMPDIR"] = self.tmp
        os.chdir(cwd or self.repo)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = kit.dispatch(argv)
        finally:
            kit.invoke = saved_invoke
            kit.pause = saved_pause
            os.chdir(saved_cwd)
            if saved_tmp is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved_tmp
            if saved_handle is None:
                os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            else:
                os.environ["ORCA_TERMINAL_HANDLE"] = saved_handle
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, lines, err.getvalue()

    def ship(self, argv):
        code, lines, err = self.run(["ship", *argv])
        assert len(lines) == 1, lines
        return code, lines[0], err

    def cleanup(self, argv, cwd=None):
        return self.run(["cleanup", *argv], cwd=cwd)

    def die(self, argv, cwd=None):
        saved_invoke = kit.invoke
        saved_cwd = os.getcwd()
        kit.invoke = self.invoke
        os.chdir(cwd or self.repo)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    kit.dispatch(argv)
                except SystemExit as exc:
                    return exc.code, out.getvalue(), err.getvalue()
        finally:
            kit.invoke = saved_invoke
            os.chdir(saved_cwd)
        raise AssertionError("command did not exit")


def world():
    tmp = tempfile.TemporaryDirectory()
    return tmp, World(tmp.name)


def assert_task_update(fx, task):
    argv = fx.task_update_argv
    assert argv is not None, fx.trace
    assert argv[argv.index("--id") + 1] == task
    assert "--task" not in argv
    assert argv[argv.index("--status") + 1] == "completed"


def test_ship_lands_a_green_owned_pr():
    tmp, fx = world()
    try:
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed"
        assert line["deleted"] is True
        assert line["task"] == "task_slug"
        assert line["branch"] == "feat/slug"
        assert fx.trace[:3] == ["view", "list-open", "merge"]
        assert "delete" in fx.trace and fx.trace.index("merge") < fx.trace.index("delete")
        assert "task-update" in fx.trace
        assert_task_update(fx, "task_slug")
        assert fx.trace.count("merge") == 1
        recorded = [argv for argv in fx.git_calls if "push" in argv]
        assert recorded == [["git", "-C", fx.repo, "push", "origin", "--delete", "feat/slug"]]
        assert not fx.ref_exists("feat/slug")
        assert fx.ref_exists("main")
    finally:
        tmp.cleanup()


def test_ship_stays_landed_when_task_update_fails():
    tmp, fx = world()
    try:
        fx.fail_update = True
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed"
        assert line["deleted"] is True
        assert "squash-merged" in line["reason"]
        assert "task-update warning" in line["reason"]
        assert "task-update failed" in line["reason"]
        assert_task_update(fx, "task_slug")
        assert not fx.ref_exists("feat/slug")
    finally:
        tmp.cleanup()


def merges_of(fx):
    return [step for step in fx.trace if step == "merge"]


def test_ship_blocks_when_checks_are_not_green():
    cases = [
        ({"statusCheckRollup": [{"conclusion": "FAILURE"}]}, "checks are not green"),
        ({"statusCheckRollup": [{"conclusion": "SUCCESS"}], "mergeStateStatus": "BLOCKED"}, "merge state is BLOCKED"),
        ({"statusCheckRollup": [{"conclusion": None}]}, "checks are not green"),
        ({"statusCheckRollup": [{"conclusion": "PENDING"}]}, "checks are not green"),
        ({"isDraft": True}, "pull request is a draft"),
    ]
    for patch, reason in cases:
        tmp, fx = world()
        try:
            fx.view.update(patch)
            code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
            assert code == 0, (patch, line)
            assert line["status"] == "blocked", (patch, line)
            assert line["reason"] == reason, (patch, line)
            assert merges_of(fx) == [], patch
            assert "delete" not in fx.trace, patch
            assert "api" not in fx.trace, patch
            assert fx.sleeps == [], patch
            assert fx.ref_exists("feat/slug")
        finally:
            tmp.cleanup()


def test_ship_holds_when_automerge_is_false():
    tmp, fx = world()
    try:
        fx.set_auto(False)
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "held"
        assert line["reason"] == "autoMerge is false in .git/gantry.json"
        assert fx.trace == []
        assert fx.ref_exists("feat/slug")
    finally:
        tmp.cleanup()


def test_ship_reports_a_stack_and_does_not_merge():
    tmp, fx = world()
    try:
        fx.view["baseRefName"] = "feat/parent"
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "stack", line
        assert "ship worker" in line["reason"]
        assert merges_of(fx) == []
        assert "delete" not in fx.trace
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.opens = [{"number": 10, "baseRefName": "feat/slug", "headRefName": "feat/child"}]
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "stack", line
        assert "10" in line["reason"]
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()


def test_ship_blocks_a_red_stack_instead_of_dispatching():
    tmp, fx = world()
    try:
        fx.view["baseRefName"] = "feat/parent"
        fx.view["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks are not green"
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()


def test_ship_does_not_delete_an_unrecorded_head():
    tmp, fx = world()
    try:
        fx.view["headRefName"] = "feat/other"
        sha = git(fx.repo, "rev-parse", "feat/slug").stdout.strip()
        run_git("--git-dir", fx.bare, "update-ref", "refs/heads/feat/other", sha)
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed", line
        assert line["deleted"] is False
        assert "head branch was not recorded" in line["reason"]
        assert merges_of(fx) == ["merge"]
        assert "delete" not in fx.trace
        assert fx.ref_exists("feat/other")
        assert fx.ref_exists("main")
    finally:
        tmp.cleanup()


def test_ship_blocks_a_pr_this_run_did_not_open():
    tmp, fx = world()
    try:
        fx.journal("task_slug", f"https://github.com/{SLUG}/pull/8", "feat/slug")
        fx.view["headRefName"] = "feat/other"
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert "no record" in line["reason"]
        assert merges_of(fx) == []
        assert "delete" not in fx.trace
    finally:
        tmp.cleanup()


def test_ship_blocks_the_default_branch():
    tmp, fx = world()
    try:
        fx.view["headRefName"] = "main"
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert "default" in line["reason"]
        assert merges_of(fx) == []
        assert fx.ref_exists("main")
        assert "delete" not in fx.trace
    finally:
        tmp.cleanup()


def test_ship_rejects_unsafe_flags():
    tmp, fx = world()
    try:
        for flag in ("--admin", "--auto", "--force", "--force-with-lease", "-f"):
            code, _out, err = fx.die(["ship", "9", flag])
            assert code == 1, flag
            assert "--admin" in err or "--force" in err, err
            assert merges_of(fx) == []
        code, line, _err = fx.ship([PR_URL, "--task", "task_slug", "--run", "run_1"])
        assert code == 0 and line["status"] == "landed", line
        code, _out, err = fx.die(["ship", "https://github.com/other/repo/pull/9"])
        assert code == 1
        assert SLUG in err
    finally:
        tmp.cleanup()


def test_ship_skips_merge_when_already_merged_and_still_deletes():
    tmp, fx = world()
    try:
        fx.view["state"] = "MERGED"
        fx.view["statusCheckRollup"] = []
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed"
        assert line["reason"].startswith("already merged")
        assert line["deleted"] is True
        assert merges_of(fx) == []
        assert not fx.ref_exists("feat/slug")
        assert "task-update" in fx.trace
    finally:
        tmp.cleanup()


def test_ship_blocks_a_closed_pr_and_a_failed_merge():
    tmp, fx = world()
    try:
        fx.view["state"] = "CLOSED"
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked"
        assert "closed" in line["reason"]
        assert merges_of(fx) == []
        assert fx.ref_exists("feat/slug")
        fx.view["state"] = "OPEN"
        fx.merge_code = 1
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert code == 1, line
        assert line["status"] == "blocked"
        assert "delete" not in fx.trace
        assert fx.ref_exists("feat/slug")
    finally:
        tmp.cleanup()


def test_cleanup_removes_a_merged_worktree_and_updates_state():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed"
        assert lines[0]["removed"] == fx.wt
        assert lines[0]["closed"] == ["term_impl"]
        assert not os.path.exists(fx.wt)
        assert os.path.isdir(fx.repo)
        assert "task-update" in fx.trace and "release" in fx.trace
        assert_task_update(fx, "task_slug")
        removes = [argv for argv in fx.git_calls if "worktree" in argv and "remove" in argv]
        assert removes == [["git", "-C", fx.repo, "worktree", "remove", fx.wt]]
        assert "--force" not in removes[0] and "-f" not in removes[0]
    finally:
        tmp.cleanup()


def test_cleanup_skips_dirty_unmerged_and_unpushed():
    tmp, fx = world()
    try:
        with open(os.path.join(fx.wt, "dirty.txt"), "w") as handle:
            handle.write("local\n")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "skipped"
        assert lines[0]["reason"] == "worktree is dirty"
        assert os.path.exists(fx.wt)
        assert "close" not in fx.trace and "task-update" not in fx.trace
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "skipped", lines
        assert lines[0]["reason"] == "branch is unmerged"
        assert os.path.exists(fx.wt)
        assert "remove" not in fx.trace
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        git(fx.wt, "commit", "--allow-empty", "-m", "ahead")
        run_git("--git-dir", fx.bare, "update-ref", "-d", "refs/heads/feat/slug")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "skipped", lines
        assert "not on the default branch" in lines[0]["reason"]
        assert os.path.exists(fx.wt)
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        main = git(fx.repo, "rev-parse", "main").stdout.strip()
        git(fx.repo, "update-ref", "refs/heads/feat/slug", main)
        run_git("--git-dir", fx.bare, "update-ref", "-d", "refs/heads/feat/slug")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed", lines
        assert not os.path.exists(fx.wt)
    finally:
        tmp.cleanup()


def test_cleanup_closes_the_main_checkout_and_skips_cwd_and_a_running_worker():
    tmp, fx = world()
    try:
        fx.worker(path=fx.repo)
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "closed", lines
        assert lines[0]["op"] == "close"
        assert "main checkout" in lines[0]["reason"]
        assert os.path.isdir(fx.repo)
        assert "remove" not in fx.trace
        assert "close" in fx.trace
        assert "task-update" in fx.trace
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"], cwd=fx.wt)
        assert lines[0]["status"] == "skipped", lines
        assert lines[0]["reason"] == "worktree is the caller cwd"
        assert os.path.exists(fx.wt)
        assert "close" not in fx.trace
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.worker(status="running")
        fx.merged_heads.add("feat/slug")
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "skipped"
        assert lines[0]["reason"] == "worker still running"
        assert os.path.exists(fx.wt)
        assert "close" not in fx.trace and "remove" not in fx.trace
    finally:
        tmp.cleanup()


def test_cleanup_dry_run_scan_and_force_do_not_remove():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        code, lines, _err = fx.cleanup(["--dry-run", "task_slug", "--run", "run_1"])
        assert lines[0]["status"] == "dry-run"
        assert lines[0]["op"] == "remove"
        assert os.path.exists(fx.wt)
        assert "remove" not in fx.trace and "close" not in fx.trace and "task-update" not in fx.trace
        fx.trace.clear()
        code, lines, _err = fx.cleanup(["--scan", "--run", "run_1"])
        assert lines[0]["status"] == "dry-run"
        assert os.path.exists(fx.wt)
        assert "remove" not in fx.trace and "close" not in fx.trace
        code, _out, err = fx.die(["cleanup", "task_slug", "--force", "--run", "run_1"])
        assert code == 1
        assert "--force" in err
        assert os.path.exists(fx.wt)
    finally:
        tmp.cleanup()


def test_cleanup_finished_removes_and_a_close_error_still_updates_state():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        code, lines, _err = fx.cleanup(["--finished", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed"
        assert not os.path.exists(fx.wt)
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        fx.close_code = 1
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed"
        assert lines[0]["closed"] == []
        assert lines[0]["errors"]
        assert "task-update" in fx.trace
        assert not os.path.exists(fx.wt)
    finally:
        tmp.cleanup()


def test_cleanup_retries_state_when_the_worktree_is_already_gone():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        fx.fail_update = True
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 1, lines
        assert lines[0]["status"] == "error"
        assert not os.path.exists(fx.wt)
        fx.fail_update = False
        fx.trace.clear()
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "closed"
        assert lines[0]["reason"] == "worktree already gone"
        assert "remove" not in fx.trace
        assert "task-update" in fx.trace
    finally:
        tmp.cleanup()


def test_cleanup_skips_its_own_terminal_and_still_removes():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_impl"
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed"
        assert lines[0]["closed"] == []
        assert "close" not in fx.trace
        assert not os.path.exists(fx.wt)
        assert "release" in fx.trace
    finally:
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        tmp.cleanup()


def fresh_view(fx, **patch):
    view = dict(fx.view)
    view.update(patch)
    return view


def test_ship_empty_rollup_depends_on_whether_checks_are_configured():
    tmp, fx = world()
    try:
        fx.view["statusCheckRollup"] = []
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed", line
        assert fx.sleeps == []
        assert merges_of(fx) == ["merge"]
        assert fx.trace.count("view") == 1
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.workflows = {"total_count": 1, "workflows": [{"name": "ci", "state": "active"}]}
        fx.view_queue = [
            fresh_view(fx, statusCheckRollup=[]),
            fresh_view(fx, statusCheckRollup=[{"conclusion": "SUCCESS"}]),
        ]
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed", line
        assert fx.sleeps == [kit.CHECK_PAUSE]
        assert fx.trace.count("view") == 2
        assert merges_of(fx) == ["merge"]
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.workflows = {"total_count": 0, "workflows": []}
        fx.required = {"contexts": ["ci"], "checks": []}
        fx.view_queue = [
            fresh_view(fx, statusCheckRollup=[]),
            fresh_view(fx, statusCheckRollup=[{"conclusion": "SUCCESS"}]),
        ]
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "landed", line
        assert fx.sleeps == [kit.CHECK_PAUSE]
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci"}]}}]
        fx.view_queue = [
            fresh_view(fx, statusCheckRollup=[]),
        ] + [fresh_view(fx, statusCheckRollup=[]) for _ in range(kit.CHECK_POLLS)]
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks have not registered"
        assert fx.sleeps == [kit.CHECK_PAUSE] * kit.CHECK_POLLS
        assert merges_of(fx) == []
        assert fx.ref_exists("feat/slug")
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.workflows = {"total_count": 1, "workflows": [{"name": "ci"}]}
        fx.view_queue = [
            fresh_view(fx, statusCheckRollup=[]),
            fresh_view(fx, statusCheckRollup=[{"conclusion": "FAILURE"}]),
        ]
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks are not green"
        assert fx.sleeps == [kit.CHECK_PAUSE]
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.workflows_error = True
        fx.view["statusCheckRollup"] = []
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks have not registered"
        assert len(fx.sleeps) == kit.CHECK_POLLS
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()


UPGRADE_403 = (
    "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"
)
PLAN_403 = "HTTP 403 plan-not-available"


def test_ship_treats_a_free_plan_protection_403_as_no_checks():
    tmp, fx = world()
    try:
        fx.view["statusCheckRollup"] = []
        fx.required_error = UPGRADE_403
        fx.rules_error = PLAN_403
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0, line
        assert line["status"] == "landed", line
        assert fx.sleeps == []
        assert merges_of(fx) == ["merge"]
        assert fx.trace.count("view") == 1
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.workflows = {"total_count": 1, "workflows": [{"name": "ci", "state": "active"}]}
        fx.view["statusCheckRollup"] = []
        fx.required_error = UPGRADE_403
        fx.rules_error = UPGRADE_403
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks have not registered"
        assert fx.sleeps == [kit.CHECK_PAUSE] * kit.CHECK_POLLS
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()
    tmp, fx = world()
    try:
        fx.view["statusCheckRollup"] = []
        fx.required_error = "gh: Resource not accessible by integration (HTTP 403)"
        fx.rules_error = "HTTP 500"
        code, line, _err = fx.ship(["9", "--run", "run_1"])
        assert line["status"] == "blocked", line
        assert line["reason"] == "checks have not registered"
        assert fx.sleeps == [kit.CHECK_PAUSE] * kit.CHECK_POLLS
        assert merges_of(fx) == []
    finally:
        tmp.cleanup()


def test_ship_merge_argv_is_squash_at_the_verified_head():
    tmp, fx = world()
    captured = []
    real = fx.invoke

    def spy(argv, cwd=None, timeout=30):
        if argv[:3] == ["gh", "pr", "merge"]:
            captured.append(list(argv))
        return real(argv, cwd=cwd, timeout=timeout)

    fx.invoke = spy
    try:
        code, line, _err = fx.ship(["9", "--task", "task_slug", "--run", "run_1"])
        assert code == 0 and line["status"] == "landed"
        assert captured == [MERGE]
    finally:
        tmp.cleanup()


def test_worker_path_reads_a_name_directory_and_a_nested_id():
    tmp, fx = world()
    saved_home = os.environ.get("HOME")
    try:
        home = os.path.join(fx.tmp, "home")
        os.environ["HOME"] = home
        name = "add-hello"
        named = os.path.join(home, "orca", "workspaces", os.path.basename(fx.repo), name)
        os.makedirs(named)
        assert kit.worker_path({"resource": {"name": name}}, fx.repo) == os.path.realpath(named)
        assert kit.worker_path({"taskId": "task_slug"}, fx.repo) == ""
        via_id = kit.worker_path({"resource": {"worktreeId": "repo-1::" + fx.wt}}, fx.repo)
        assert via_id == os.path.realpath(fx.wt)
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        tmp.cleanup()


def test_cleanup_removes_the_linked_worktree_when_the_row_has_no_path():
    tmp, fx = world()
    try:
        fx.merged_heads.add("feat/slug")
        fx.workers = [{
            "taskId": "task_slug",
            "dispatchStatus": "completed",
            "agentTerminalHandle": "term_impl",
            "dispatchId": "ctx_impl",
        }]
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "removed", lines
        assert lines[0]["removed"] == os.path.realpath(fx.wt)
        assert not os.path.exists(fx.wt)
    finally:
        tmp.cleanup()


def test_cleanup_does_not_report_removed_without_a_worktree():
    tmp, fx = world()
    try:
        git(fx.repo, "worktree", "remove", fx.wt)
        fx.workers = [{
            "taskId": "task_slug",
            "dispatchStatus": "completed",
            "agentTerminalHandle": "term_impl",
            "dispatchId": "ctx_impl",
        }]
        code, lines, _err = fx.cleanup(["task_slug", "--run", "run_1"])
        assert code == 0, lines
        assert lines[0]["status"] == "closed", lines
        assert lines[0]["removed"] == ""
        assert lines[0]["reason"] == "no worktree record"
        assert lines[0]["closed"] == ["term_impl"]
        assert lines[0]["path"] == ""
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    test_ship_lands_a_green_owned_pr()
    test_ship_stays_landed_when_task_update_fails()
    test_ship_blocks_when_checks_are_not_green()
    test_ship_holds_when_automerge_is_false()
    test_ship_reports_a_stack_and_does_not_merge()
    test_ship_blocks_a_red_stack_instead_of_dispatching()
    test_ship_does_not_delete_an_unrecorded_head()
    test_ship_blocks_a_pr_this_run_did_not_open()
    test_ship_blocks_the_default_branch()
    test_ship_rejects_unsafe_flags()
    test_ship_skips_merge_when_already_merged_and_still_deletes()
    test_ship_blocks_a_closed_pr_and_a_failed_merge()
    test_ship_empty_rollup_depends_on_whether_checks_are_configured()
    test_ship_treats_a_free_plan_protection_403_as_no_checks()
    test_ship_merge_argv_is_squash_at_the_verified_head()
    test_cleanup_removes_a_merged_worktree_and_updates_state()
    test_cleanup_skips_dirty_unmerged_and_unpushed()
    test_cleanup_closes_the_main_checkout_and_skips_cwd_and_a_running_worker()
    test_cleanup_dry_run_scan_and_force_do_not_remove()
    test_cleanup_finished_removes_and_a_close_error_still_updates_state()
    test_cleanup_retries_state_when_the_worktree_is_already_gone()
    test_cleanup_skips_its_own_terminal_and_still_removes()
    test_worker_path_reads_a_name_directory_and_a_nested_id()
    test_cleanup_removes_the_linked_worktree_when_the_row_has_no_path()
    test_cleanup_does_not_report_removed_without_a_worktree()
    print("25/25 passed")
