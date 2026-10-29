#!/usr/bin/env python3
"""Gantry kit: registry, user-level hooks, and the gantry command."""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

VERSION = "0.4.0"
AGENT_NAME = "gantry-coordinator"
HOOK_MARK = ".gantry/current/kit/"
RUN_EVENTS = ("PreCompact", "SessionStart", "UserPromptSubmit")
BAD = object()
USAGE = """usage: gantry install [repo] [--repair]
       gantry update [repo] [--tag <tag>]
       gantry uninstall [repo]
       gantry uninstall --all
       gantry doctor [repo]
       gantry overlap --checkpoint <scoped|pre-pr> --intent <text> --files <a,b> [--run <id> --task <id>]
       gantry overlap --record <gathered|classified|answered> --checkpoint <name> --run <id> --task <id> [--verdict <continue|stop>] [--answer <proceed|hold>]
       gantry charter add [--global] '<rule>'
       gantry charter list
       gantry charter rm <id>
       gantry ship <pr> [--task <id>] [--run <id>]
       gantry wait [--run <id>] [--timeout-ms N] [--heartbeat-ms N] [--stall-ms N] [--dispatch <id>]
       gantry cleanup <task> [--run <id>] [--dry-run]
       gantry cleanup --scan [--run <id>]
       gantry cleanup --finished [--run <id>] [--dry-run]
       gantry restart
       gantry --version
       gantry [objective]"""
RESTART_PROMPT = (
    "Continue the Orca Run this terminal is already bound to. "
    "If a gantry handoff is in context, follow its Next line. "
    "If it is not, run orca orchestration run-current, task-list, worker-list, and gate-list, and continue that Run. "
    "Do not create a new Run. Do not run orca skills get orchestration."
)
REMOTE_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/(?P<o1>[^/]+)/(?P<r1>[^/]+)"
    r"|(?:[^@]+)@(?P<h2>[^:]+):(?P<o2>[^/]+)/(?P<r2>[^/]+))/?$",
    re.I,
)
PRE_PUSH_STUB = """#!/bin/sh
# gantry pre-push
kit="${HOME}/.gantry/current/kit/pre-push"
if [ ! -x "$kit" ]; then
  echo "gantry pre-push: kit missing; refusing the push." >&2
  exit 1
fi
exec "$kit"
"""
ACTIONS = {"duplicate": "stop", "related": "flag", "unrelated": "ignore"}
KIND_RANK = {"pr": 0, "branch": 1, "issue": 2, "worker": 3}
PROBE = [
    ("lease push", "git push --force-with-lease=refs/heads/feat:0123abc origin HEAD:refs/heads/feat",
     "Shipping restacks PRs with a lease push",
     "allow the lease form, e.g. change the pattern to `push (--force|-f)([^-]|$)`"),
    ("worktree resync", "git reset --hard origin/feat",
     "Opening a PR resyncs a worktree to its remote branch",
     "allow `reset --hard origin/<branch>`, e.g. exempt commands matching `reset --hard origin/`"),
    ("guarded merge", "gh pr merge 1 --squash --match-head-commit 0123abc",
     "Shipping merges only the head commit it verified",
     "allow `gh pr merge` when it carries `--match-head-commit`"),
]
NO_ORIGIN = "no origin remote in {repo}"
BAD_ORIGIN = "origin {url} is not a parseable git remote. Expected https://host/owner/repo or git@host:owner/repo"
GLOBAL = "global"
CHARTER_CAP = 3000
TEXT_CAP = 1000
RULE_ID = re.compile(r"(?P<kind>[gr])-[0-9a-f]{6,64}")
CHARTER_HEADER = (
    "gantry charter. Standing rules from the user. "
    "Follow every one, and copy the lines a task needs onto its brief's Charter line."
)
OVERFLOW_LINE = "- {count} more rules; run gantry charter list"
UNREADABLE_LINE = "- unreadable; run gantry doctor"


class RegistryError(Exception):
    pass


class CharterError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    scope: str
    rules: dict | None


def die(message):
    print(f"gantry: {message}", file=sys.stderr)
    raise SystemExit(1)


def home():
    return os.path.join(os.path.expanduser("~"), ".gantry")


def claude_dir():
    return os.path.join(os.path.expanduser("~"), ".claude")


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def agent_path():
    return os.path.join(claude_dir(), "agents", f"{AGENT_NAME}.md")


def kit_file(*parts):
    return os.path.join(home(), "current", "kit", *parts)


def git_stdout(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def parse_remote(url):
    match = REMOTE_RE.match((url or "").strip())
    if not match:
        return None
    host = (match["h1"] or match["h2"]).lower()
    owner, repo = match["o1"] or match["o2"], match["r1"] or match["r2"]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if host == "www.github.com":
        host = "github.com"
    if not owner or not repo or owner in (".", "..") or repo in (".", ".."):
        return None
    slug = f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"
    return {"host": host, "owner": owner, "repo": repo, "slug": slug}


def normalize_origin(url):
    parsed = parse_remote(url)
    if not parsed:
        return None
    return f"https://{parsed['host']}/{parsed['owner'].lower()}/{parsed['repo'].lower()}"


def registry_path():
    return os.path.join(home(), "registry.json")


def load_registry():
    path = registry_path()
    if not os.path.exists(path):
        return {"version": 1, "repos": {}}
    try:
        data = json.load(open(path))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"registry cannot be read ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        raise RegistryError("registry is not an object with repos")
    return data


def registry_failure():
    """None when the registry can be used. 'missing' or 'unreadable' otherwise."""
    path = registry_path()
    if not os.path.exists(path):
        return "missing"
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return "unreadable"
    return None


_registry_warned = False


def warn_registry():
    global _registry_warned
    if _registry_warned:
        return
    _registry_warned = True
    print("gantry: registry is unreadable; run gantry doctor", file=sys.stderr)


def fallback_governed(common, key):
    if not common or not os.path.isfile(os.path.join(common, "gantry.json")):
        return None
    return Governed(os.path.dirname(common), common, key or "", read_config(common))


def salvage_checkouts(raw):
    """Checkout paths still visible in an unreadable registry file."""
    found = []
    seen = set()
    for match in re.findall(r"(/[^\s\"'\\]+)", raw or ""):
        path = match.rstrip("}])>,.")
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        common = git_stdout(real, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common or not os.path.isfile(os.path.join(common, "gantry.json")):
            continue
        url = origin_url(real)
        key = normalize_origin(url) if url else None
        if not key:
            continue
        seen.add(real)
        found.append((key, real))
    return found


def rebuild_registry():
    path = registry_path()
    try:
        raw = open(path).read()
    except OSError:
        raw = ""
    found = salvage_checkouts(raw)
    repos = {}
    for key, checkout in found:
        entry = repos.setdefault(key, {"checkouts": []})
        if checkout not in entry["checkouts"]:
            entry["checkouts"].append(checkout)
    save_registry({"version": 1, "repos": repos})
    if found:
        print(f"gantry: registry repaired from {len(found)} checkout(s)")
    else:
        print("gantry: registry was unreadable; started empty")


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path + ".lock", "a+")
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        lock.close()


def save_registry(data):
    write_json_atomic(registry_path(), data)


def update_registry(mutator):
    with locked(registry_path()):
        data = load_registry()
        mutator(data)
        save_registry(data)


def register(key, checkout):
    if not key:
        die("cannot register an empty origin")
    checkout = os.path.realpath(checkout)

    def mutate(data):
        entry = data["repos"].setdefault(key, {"checkouts": []})
        if checkout not in entry["checkouts"]:
            entry["checkouts"].append(checkout)

    update_registry(mutate)


def read_config(common):
    path = os.path.join(common, "gantry.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return BAD
    if not isinstance(data, dict):
        return BAD
    return data


class Governed:
    def __init__(self, root, common, origin, config):
        self.root = root
        self.common = common
        self.origin = origin
        self.config = config


def look(start, warn=False):
    if not start or not os.path.isdir(start):
        return None
    common = git_stdout(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    url = git_stdout(start, "config", "--get", "remote.origin.url")
    key = normalize_origin(url) if url else None
    failure = registry_failure()
    if failure:
        if warn:
            warn_registry()
        return fallback_governed(common, key)
    data = load_registry()
    if not key or key not in data["repos"]:
        return None
    return Governed(os.path.dirname(common), common, key, read_config(common))


def governing_start(project_dir, cwd):
    if project_dir and git_stdout(project_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        return project_dir
    return cwd or os.getcwd()


def governing(project_dir=None, cwd=None, warn=False):
    return look(governing_start(project_dir, cwd), warn=warn)


def checkout_of(path):
    common = git_stdout(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        die(f"{path} is not a git repository")
    return os.path.dirname(common), common


def origin_url(repo):
    return git_stdout(repo, "config", "--get", "remote.origin.url")


def require_origin(repo):
    url = origin_url(repo)
    if not url:
        die(NO_ORIGIN.format(repo=repo))
    key = normalize_origin(url)
    if not key:
        die(BAD_ORIGIN.format(url=url))
    return url, key


def source_checkout():
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.basename(parent) == "template":
        return os.path.dirname(parent)
    return None


def install_info_path():
    return os.path.join(home(), "install.json")


def read_install_info():
    path = install_info_path()
    if not os.path.exists(path):
        return None
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_install_info(source, ref):
    write_json_atomic(install_info_path(), {"version": VERSION, "source": source, "ref": ref})


def configured_source():
    info = read_install_info()
    if info and os.path.isdir(info.get("source") or ""):
        return info["source"]
    root = source_checkout()
    if root:
        return root
    die("no gantry source checkout to install from")


def version_of(path):
    match = re.search(r'^VERSION = "([^"]+)"', open(path).read(), re.M)
    return match.group(1) if match else "none"


def tree_hash(path):
    digest = hashlib.sha256()
    for dirpath, dirs, files in os.walk(path):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path).encode()
            digest.update(rel)
            digest.update(b"\0")
            digest.update(open(full, "rb").read())
            digest.update(b"\0")
    return digest.hexdigest()[:12]


def stage_tree(root, tag):
    if tag:
        extract = tempfile.mkdtemp(prefix="gantry-archive-")
        proc = subprocess.run(["git", "-C", root, "archive", tag, "template/kit", "bin/gantry"], capture_output=True)
        if proc.returncode != 0:
            shutil.rmtree(extract, ignore_errors=True)
            die(proc.stderr.decode() or f"git archive {tag} failed")
        subprocess.run(["tar", "-x", "-C", extract], input=proc.stdout, check=True)
        kit = os.path.join(extract, "template", "kit")
        binary = os.path.join(extract, "bin", "gantry")
    else:
        extract = None
        kit = os.path.join(root, "template", "kit")
        binary = os.path.join(root, "bin", "gantry")
    if not os.path.isdir(kit) or not os.path.isfile(binary):
        if extract:
            shutil.rmtree(extract, ignore_errors=True)
        die("gantry source is missing template/kit or bin/gantry")
    staged = tempfile.mkdtemp(prefix="gantry-stage-")
    shutil.copytree(kit, os.path.join(staged, "kit"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(staged, "bin"))
    shutil.copy(binary, os.path.join(staged, "bin", "gantry"))
    for name in ("gantry-guard.py", "gantry_run.py", "gantry-mailbox", "pre-push"):
        path = os.path.join(staged, "kit", name)
        if os.path.exists(path):
            os.chmod(path, 0o755)
    os.chmod(os.path.join(staged, "bin", "gantry"), 0o755)
    if extract:
        shutil.rmtree(extract, ignore_errors=True)
    return staged


def smoke(dest):
    kit = os.path.join(dest, "kit", "gantry_kit.py")
    proc = subprocess.run([sys.executable, kit, "--version"], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        shutil.rmtree(dest, ignore_errors=True)
        die(f"refusing to switch to a kit that will not run: {proc.stderr.strip()}")


def swap_current(dest):
    link = os.path.join(home(), "current")
    os.makedirs(home(), exist_ok=True)
    tmp = link + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(dest, tmp)
    os.replace(tmp, link)


def current_release():
    link = os.path.join(home(), "current")
    if os.path.lexists(link):
        return os.path.realpath(link)
    return None


def installed_version():
    kit = kit_file("gantry_kit.py")
    if not os.path.isfile(kit):
        return "none"
    proc = subprocess.run([sys.executable, kit, "--version"], capture_output=True, text=True)
    return proc.stdout.strip() or "none"


def ensure_kit(tag=None):
    root = configured_source()
    staged = stage_tree(root, tag)
    try:
        digest = tree_hash(staged)
        version = version_of(os.path.join(staged, "kit", "gantry_kit.py"))
        dest = os.path.join(home(), "releases", f"{version}-{digest}")
        if os.path.isdir(dest) and tree_hash(dest) != digest:
            shutil.rmtree(dest)
        built = False
        if not os.path.isdir(dest):
            partial = dest + ".partial"
            if os.path.exists(partial):
                shutil.rmtree(partial)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(staged, partial)
            smoke(partial)
            os.rename(partial, dest)
            built = True
        current = current_release()
        changed = built or current != os.path.realpath(dest)
        if current != os.path.realpath(dest):
            swap_current(dest)
        write_install_info(root, tag or "worktree")
        return dest, changed, version
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def owns(command):
    return isinstance(command, str) and HOOK_MARK in command and (
        "gantry-guard.py" in command or "gantry_run.py" in command
    )


def pretool_command(guard):
    quoted = shlex.quote(guard)
    inner = f"if [ ! -x {quoted} ]; then exit 0; fi; {quoted}; code=$?; if [ \"$code\" -ne 0 ] && [ \"$code\" -ne 2 ]; then exit 2; fi; exit \"$code\""
    return "/bin/sh -c " + shlex.quote(inner)


def strip_ours(settings):
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            kept = []
            for entry in entries or []:
                if not isinstance(entry, dict):
                    kept.append(entry)
                    continue
                inner = []
                for hook in entry.get("hooks") or []:
                    if isinstance(hook, dict) and owns(hook.get("command")):
                        continue
                    inner.append(hook)
                if inner:
                    cloned = dict(entry)
                    cloned["hooks"] = inner
                    kept.append(cloned)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        if not hooks:
            settings.pop("hooks", None)
    status = settings.get("statusLine")
    if isinstance(status, dict) and owns(status.get("command")):
        settings.pop("statusLine", None)
    return settings


def merge_settings(settings):
    settings = json.loads(json.dumps(settings))
    strip_ours(settings)
    guard = kit_file("gantry-guard.py")
    run = kit_file("gantry_run.py")
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": pretool_command(guard)}],
    })
    for event in RUN_EVENTS:
        hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": run}]})
    status = settings.get("statusLine")
    if not isinstance(status, dict) or owns(status.get("command")):
        settings["statusLine"] = {"type": "command", "command": run}
    return settings


def render_settings(settings):
    return json.dumps(settings, indent=2) + "\n"


def save_prior_once():
    path = settings_path()
    dest = os.path.join(home(), "prior", "settings.json")
    if os.path.exists(dest) or not os.path.exists(path):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(path, dest)


def write_settings(text):
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def install_user_hooks():
    save_prior_once()
    path = settings_path()
    before = open(path).read() if os.path.exists(path) else None
    if before is None:
        current = {}
    else:
        try:
            current = json.loads(before)
        except ValueError:
            die("user settings are not JSON; refusing to merge")
        if not isinstance(current, dict):
            die("user settings are not a JSON object; refusing to merge")
    merged = render_settings(merge_settings(current))
    if before == merged:
        return
    if before is not None and open(path).read() != before:
        die("~/.claude/settings.json changed during the hook merge; rerun install")
    write_settings(merged)


def ours_agent(path):
    try:
        head = open(path).read(400)
    except OSError:
        return False
    return "name: gantry-coordinator" in head


def install_agent():
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gantry-coordinator.md")
    # The running file may be the source tree. The installed copy is the one just swapped.
    installed = kit_file("gantry-coordinator.md")
    if os.path.isfile(installed):
        src = installed
    dest = agent_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and not ours_agent(dest):
        print(f"gantry: {dest} is not the gantry coordinator; left it alone.", file=sys.stderr)
        return
    shutil.copy(src, dest)


def place_prepush(repo):
    _root, common = checkout_of(repo)
    hooks = os.path.join(common, "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-push")
    if os.path.exists(path) and "gantry pre-push" not in open(path).read():
        print(f"gantry: {path} exists and differs; left it alone. Merge template/kit/pre-push by hand.", file=sys.stderr)
        return
    fd, tmp = tempfile.mkstemp(dir=hooks)
    with os.fdopen(fd, "w") as handle:
        handle.write(PRE_PUSH_STUB)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)


def remove_registered_prepush():
    try:
        data = load_registry()
    except RegistryError:
        try:
            raw = open(registry_path()).read()
        except OSError:
            return
        for _key, checkout in salvage_checkouts(raw):
            remove_prepush(checkout)
        return
    for entry in data.get("repos", {}).values():
        for checkout in entry.get("checkouts") or []:
            if os.path.isdir(checkout):
                remove_prepush(checkout)


def remove_prepush(repo):
    common = git_stdout(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return
    path = os.path.join(common, "hooks", "pre-push")
    if os.path.isfile(path) and "gantry pre-push" in open(path).read():
        os.remove(path)


def seed_config(common):
    path = os.path.join(common, "gantry.json")
    if os.path.exists(path):
        return
    src = kit_file("gantry.json")
    if not os.path.isfile(src):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gantry.json")
    shutil.copy(src, path)


# Claude Code's Agent tool creates this inside the checkout when a worker asks
# for worktree isolation. It is not part of the repo. info/exclude is local:
# install does not commit it.
WORKTREE_EXCLUDE = "/.claude/worktrees/"
_WORKTREE_EXCLUDE_COVERED = {
    "/.claude/worktrees/",
    "/.claude/worktrees",
    ".claude/worktrees/",
    ".claude/worktrees",
}


def worktree_exclude_covered(text):
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in _WORKTREE_EXCLUDE_COVERED:
            return True
    return False


def ensure_worktree_exclude(repo):
    path = git_stdout(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if not path:
        return
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    current = ""
    if os.path.isfile(path):
        with open(path) as handle:
            current = handle.read()
    if worktree_exclude_covered(current):
        return
    text = current
    if text and not text.endswith("\n"):
        text += "\n"
    text += WORKTREE_EXCLUDE + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_trust(repo):
    store = os.path.join(os.path.expanduser("~"), ".claude.json")
    if not os.path.isfile(store):
        die("~/.claude.json is missing; open Claude Code once so the trust file exists")
    before = os.stat(store)
    data = json.load(open(store))
    if not isinstance(data, dict):
        die("~/.claude.json is not a JSON object")
    data.setdefault("projects", {}).setdefault(os.path.realpath(repo), {})["hasTrustDialogAccepted"] = True
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(store))
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, before.st_mode & 0o777)
    if os.stat(store).st_mtime_ns != before.st_mtime_ns:
        os.unlink(tmp)
        die("~/.claude.json changed during the trust write; rerun install")
    os.replace(tmp, store)


def bash_matches(matcher):
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return False


def probe_hooks(repo):
    found = []
    for source in (settings_path(), os.path.join(repo, ".claude", "settings.json"), os.path.join(repo, ".claude", "settings.local.json")):
        try:
            entries = json.load(open(source))["hooks"]["PreToolUse"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not bash_matches(str(entry.get("matcher") or "")):
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                command, timeout = hook.get("command"), hook.get("timeout")
                if isinstance(command, str) and command and "gantry-guard.py" not in command:
                    ok = isinstance(timeout, (int, float)) and timeout > 0
                    found.append((source, command, min(timeout, 10) if ok else 10))
    env = {"HOME": os.environ["HOME"], "PATH": os.environ.get("PATH", ""), "CLAUDE_PROJECT_DIR": repo}
    header = False
    for source, command, timeout in found:
        for name, sample, why, suggestion in PROBE:
            reason = probe_verdict(command, timeout, sample, repo, env)
            if reason is None:
                continue
            if not header:
                print("gantry: warning: your Claude Code hooks block commands pstack needs. gantry merges its own hooks and never edits yours.", file=sys.stderr)
                header = True
            print(
                f"  {source}: hook `{command}`\n"
                f"    blocks {name}: {sample}\n"
                f"    needed because: {why}\n"
                f"    hook said: {reason[:200] or '(no reason)'}\n"
                f"    suggestion: {suggestion}",
                file=sys.stderr,
            )


def probe_verdict(hook_cmd, timeout, command, repo, env):
    payload = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": command}, "cwd": repo, "session_id": "gantry-install",
        "transcript_path": "", "permission_mode": "bypassPermissions",
    })
    try:
        proc = subprocess.run(["/bin/sh", "-c", hook_cmd], input=payload, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode == 2:
        return (proc.stderr.strip().splitlines() or [""])[0]
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(out, dict):
        return None
    specific = out.get("hookSpecificOutput") or {}
    if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
        return str(specific.get("permissionDecisionReason", "")).strip()
    if out.get("decision") == "block":
        return str(out.get("reason", "")).strip()
    return None


PSTACK_ENV = "GANTRY_PSTACK"
PSTACK_NOTICE_ENV = "GANTRY_PSTACK_NOTICE"
PSTACK_TESTED = "0.15.36"
BLOCKED_PSTACK = "! blocked run · pstack not installed · stopped"


def _version_key(name):
    text = name[1:] if name.startswith("v") and len(name) > 1 and name[1].isdigit() else name
    parts = []
    for part in text.split("."):
        if not part.isdigit():
            return ()
        parts.append(int(part))
    return tuple(parts)


def pstack_roots(home_dir=None):
    base = home_dir or os.path.expanduser("~")
    pattern = os.path.join(base, ".claude", "plugins", "cache", "*", "pstack", "*", "skills", "poteto-mode")
    roots = []
    for skill in glob.glob(pattern):
        if os.path.isdir(skill):
            roots.append(os.path.abspath(os.path.dirname(os.path.dirname(skill))))
    return roots


def pstack_root(home_dir=None, need_skill=False):
    """Newest installed pstack root.

    Version is the dotted numeric directory name. `cursor-plugins` wins a tie.
    `need_skill` requires `skills/poteto-mode/SKILL.md`, which the session follows.
    Install only needs the `poteto-mode` directory.
    """
    ranked = []
    for root in pstack_roots(home_dir):
        skill = os.path.join(root, "skills", "poteto-mode", "SKILL.md")
        if need_skill and not os.path.isfile(skill):
            continue
        market = os.path.basename(os.path.dirname(os.path.dirname(root)))
        prefer = 1 if market == "cursor-plugins" else 0
        ranked.append((_version_key(os.path.basename(root)), prefer, root))
    if not ranked:
        return None
    ranked.sort()
    return ranked[-1][2]


def pstack_notice(installed, tested=None):
    """One quiet line when the installed pstack is newer than the tested release.

    Equal and older versions return None. The caller continues either way.
    """
    tested = PSTACK_TESTED if tested is None else tested
    if _version_key(installed) > _version_key(tested):
        return f"! pstack {installed} newer than tested {tested} · continuing"
    return None


def bind_pstack(home_dir=None):
    """Export GANTRY_PSTACK when SKILL.md is installed, otherwise clear it.

    GANTRY_PSTACK_NOTICE is set only when that copy is newer than PSTACK_TESTED.
    """
    root = pstack_root(home_dir, need_skill=True)
    if root:
        os.environ[PSTACK_ENV] = root
        notice = pstack_notice(os.path.basename(root))
        if notice:
            os.environ[PSTACK_NOTICE_ENV] = notice
        else:
            os.environ.pop(PSTACK_NOTICE_ENV, None)
        return root
    os.environ.pop(PSTACK_ENV, None)
    os.environ.pop(PSTACK_NOTICE_ENV, None)
    return None


def require_tools():
    if shutil.which("orca") is None:
        die("orca CLI not on PATH")
    if pstack_root() is None:
        die("pstack plugin not installed")


def cmd_install(argv):
    repair = "--repair" in argv
    repos = positional(argv)
    repo = repos[0] if repos else "."
    root, common = checkout_of(repo)
    _url, key = require_origin(root)
    failure = registry_failure()
    if failure == "unreadable" and not repair:
        die("registry is unreadable; rerun with gantry install --repair")
    if failure == "unreadable":
        rebuild_registry()
    require_tools()
    old = installed_version()
    _dest, changed, new = ensure_kit()
    install_user_hooks()
    install_agent()
    place_prepush(root)
    seed_config(common)
    ensure_worktree_exclude(root)
    write_trust(root)
    probe_hooks(root)
    register(key, root)
    print(f"gantry installed in {os.path.realpath(root)}")
    print(f"gantry: version {old} -> {new}" if changed else f"gantry: version {new}")
    print("Nothing in the target repo was committed.")
    return 0


def positional(argv):
    skip = False
    items = []
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("--tag", "--record", "--checkpoint", "--intent", "--files", "--run", "--task", "--verdict", "--answer", "--repo"):
            skip = True
            continue
        if arg == "--all" or arg.startswith("--"):
            continue
        items.append(arg)
    return items


def cmd_update(argv):
    tag = None
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
    repos = positional(argv)
    repo = repos[0] if repos else None
    old = installed_version()
    _dest, changed, new = ensure_kit(tag)
    install_user_hooks()
    install_agent()
    if repo:
        root, _common = checkout_of(repo)
        place_prepush(root)
        ensure_worktree_exclude(root)
    print(f"gantry: version {old} -> {new}")
    if changed:
        print("gantry: refreshed ~/.gantry")
    else:
        print("gantry: ~/.gantry already matches the template")
    print("gantry: no PR opened")
    return 0


def restore_settings():
    path = settings_path()
    if not os.path.exists(path):
        return
    try:
        current = json.loads(open(path).read())
    except ValueError:
        return
    if not isinstance(current, dict):
        return
    before = json.loads(json.dumps(current))
    strip_ours(current)
    if current == before:
        return
    prior_path = os.path.join(home(), "prior", "settings.json")
    prior = None
    prior_bytes = None
    if os.path.isfile(prior_path):
        prior_bytes = open(prior_path, "rb").read()
        try:
            prior = json.loads(prior_bytes)
        except ValueError:
            prior = None
    if prior is not None and prior == current:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "wb") as handle:
            handle.write(prior_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return
    hooks = current.get("hooks") or {}
    empty_hooks = not any(hooks.get(event) for event in list(hooks))
    leftover = {key: value for key, value in current.items() if not (key == "hooks" and empty_hooks)}
    if not leftover:
        os.remove(path)
        return
    if empty_hooks:
        current.pop("hooks", None)
    write_settings(render_settings(current))


def cmd_uninstall(argv):
    if "--all" in argv:
        remove_registered_prepush()
        restore_settings()
        dest = agent_path()
        if os.path.isfile(dest) and ours_agent(dest):
            os.remove(dest)
        if os.path.isdir(home()):
            shutil.rmtree(home())
        print("gantry removed user hooks and agent")
        return 0
    repo = argv[0] if argv else "."
    root, _common = checkout_of(repo)
    url = origin_url(root)
    key = normalize_origin(url) if url else None
    if key and os.path.exists(registry_path()):
        checkout = os.path.realpath(root)

        def mutate(data):
            entry = data["repos"].get(key)
            if not entry:
                return
            entry["checkouts"] = [item for item in entry.get("checkouts") or [] if item != checkout]
            if not entry["checkouts"]:
                del data["repos"][key]

        update_registry(mutate)
    remove_prepush(root)
    print(f"gantry unregistered {os.path.realpath(root)}")
    return 0


def cmd_doctor(argv):
    repo = argv[0] if argv else "."
    print(f"version: {VERSION}")
    found_pstack = pstack_root()
    print("pstack: " + (found_pstack if found_pstack else "not installed"))
    print(f"pstack tested: {PSTACK_TESTED}")
    current = os.path.join(home(), "current")
    print(f"kit: {current}" + (f" -> {os.path.realpath(current)}" if os.path.lexists(current) else " (missing)"))
    failure = registry_failure()
    if failure == "unreadable":
        print("registry: unreadable")
    elif failure == "missing":
        print("registry: missing")
    start = repo if os.path.isdir(repo) else os.getcwd()
    found = governing(os.environ.get("CLAUDE_PROJECT_DIR"), start)
    if found:
        print(f"registered: {found.origin}")
        print(f"config: {os.path.join(found.common, 'gantry.json')}")
    else:
        print("registered: no")
    sections = _read_sections(os.environ.get("CLAUDE_PROJECT_DIR"), start)
    print(_charter_doctor_line(sections))
    path = settings_path()
    if os.path.isfile(path):
        try:
            settings = json.load(open(path))
        except ValueError:
            settings = {}
        text = json.dumps(settings)
        print("hooks: gantry guard " + ("present" if "gantry-guard.py" in text else "absent"))
        status = settings.get("statusLine") if isinstance(settings, dict) else None
        if isinstance(status, dict) and owns(status.get("command")):
            print("status line: gantry")
        elif status:
            print("status line: foreign, left in place")
        else:
            print("status line: absent")
    else:
        print("hooks: no user settings")
    print("agent: " + ("ours" if os.path.isfile(agent_path()) and ours_agent(agent_path()) else "absent"))
    if git_stdout(repo, "rev-parse", "--git-dir"):
        common = git_stdout(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        hook = os.path.join(common or "", "hooks", "pre-push")
        if os.path.isfile(hook) and "gantry pre-push" in open(hook).read():
            print("pre-push: gantry stub")
        elif os.path.isfile(hook):
            print("pre-push: foreign, left in place")
        else:
            print("pre-push: absent")
        hooks_path = git_stdout(repo, "config", "--get", "core.hooksPath")
        if hooks_path:
            print("core.hooksPath is set; the gantry pre-push stub will not run")
    return 1 if failure == "unreadable" or any(section.rules is None for section in sections) else 0


def normalize_files(paths):
    cleaned = []
    for path in paths:
        path = (path or "").strip()
        if path.startswith("./"):
            path = path[2:]
        if path:
            cleaned.append(path)
    return cleaned


def shared_count(planned, files):
    return len(set(normalize_files(planned)) & set(normalize_files(files)))


def gh_json(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    if not proc.stdout.strip():
        return []
    data = json.loads(proc.stdout)
    return data


def pull_requests(slug):
    fields = "number,title,body,headRefName,url,files"
    try:
        rows = gh_json(["pr", "list", "--repo", slug, "--state", "open", "--limit", "50", "--json", fields])
        have_files = True
    except RuntimeError:
        rows = gh_json(["pr", "list", "--repo", slug, "--state", "open", "--limit", "50", "--json", "number,title,body,headRefName,url"])
        have_files = False
    found = []
    for row in rows:
        if have_files and isinstance(row.get("files"), list):
            files = [item.get("path") if isinstance(item, dict) else str(item) for item in row["files"]]
        else:
            diff = subprocess.run(["gh", "pr", "diff", str(row.get("number")), "--repo", slug, "--name-only"], capture_output=True, text=True)
            files = [line.strip() for line in diff.stdout.splitlines() if line.strip()] if diff.returncode == 0 else []
        found.append({
            "kind": "pr",
            "id": str(row.get("number")),
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "branch": row.get("headRefName") or "",
            "url": row.get("url") or "",
            "files": normalize_files(files),
        })
    return found


def remote_branches(repo):
    proc = subprocess.run(["git", "-C", repo, "ls-remote", "--heads", "origin"], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    names = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.append(parts[1][len("refs/heads/"):])
    return names


def branch_files(repo, branch):
    proc = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", f"origin/HEAD...origin/{branch}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return normalize_files(line.strip() for line in proc.stdout.splitlines() if line.strip())


def assigned_issues(slug):
    try:
        rows = gh_json(["issue", "list", "--repo", slug, "--assignee", "@me", "--state", "open", "--limit", "30", "--json", "number,title,body,url"])
    except (RuntimeError, ValueError):
        return [], True
    found = []
    for row in rows:
        found.append({
            "kind": "issue",
            "id": str(row.get("number")),
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "branch": "",
            "url": row.get("url") or "",
            "files": [],
        })
    return found, False


def load_orca(args):
    try:
        proc = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None
    result = payload.get("result") if "result" in payload else payload
    return result if isinstance(result, dict) else None


def active_workers():
    proc = subprocess.run(["orca", "orchestration", "worker-list", "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return [], True
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return [], True
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = (result or payload or {}).get("workers") if isinstance(result or payload, dict) else None
    if not isinstance(rows, list):
        return [], True
    found = []
    for row in rows:
        if row.get("dispatchStatus") in ("completed", "failed"):
            continue
        found.append({
            "kind": "worker",
            "id": str(row.get("taskId") or row.get("dispatchId") or ""),
            "title": str(row.get("task_title") or row.get("taskId") or ""),
            "body": "",
            "branch": "",
            "url": "",
            "files": normalize_files(row.get("files") or []),
        })
    return found, False


def plain_branch(name):
    name = (name or "").strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    elif name.startswith("refs/remotes/origin/"):
        name = name[len("refs/remotes/origin/"):]
    if name.startswith("origin/"):
        name = name[len("origin/"):]
    return name


def recorded_branch(result):
    for _ in range(3):
        if isinstance(result, dict):
            direct = result.get("branch")
            if isinstance(direct, str) and direct.strip() and direct.strip() != "none":
                return plain_branch(direct)
            result = result.get("body") if isinstance(result.get("body"), str) else ""
            continue
        if not isinstance(result, str) or not result:
            return ""
        try:
            data = json.loads(result)
        except ValueError:
            data = None
        if isinstance(data, dict):
            result = data
            continue
        for token in result.replace(",", " ").split():
            if token.startswith("branch=") and token != "branch=none":
                return plain_branch(token[len("branch="):])
        return ""
    return ""


def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value[:1] == "{":
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def task_id_of(row):
    if not isinstance(row, dict):
        return ""
    for key in ("taskId", "task_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def dispatch_id_of(row):
    if not isinstance(row, dict):
        return ""
    for key in ("dispatchId", "dispatch_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    nested = as_dict(row.get("dispatch"))
    if nested:
        value = nested.get("id")
        if isinstance(value, str) and value:
            return value
    return ""


def explicit_branch(row):
    if not isinstance(row, dict):
        return ""
    for node in (row, as_dict(row.get("dispatch"))):
        if not isinstance(node, dict):
            continue
        value = node.get("branch")
        if isinstance(value, str) and value.strip() and value.strip() != "none":
            return plain_branch(value)
    return ""


def worker_checkout(row):
    if not isinstance(row, dict):
        return ""
    nodes = [row]
    for key in ("resource", "terminal", "workspace", "dispatch"):
        node = as_dict(row.get(key))
        if node:
            nodes.append(node)
    projection = as_dict(row.get("projection"))
    if projection:
        workspace = as_dict(projection.get("workspace"))
        if workspace:
            nodes.append(workspace)
    for node in nodes:
        for key in ("worktreePath", "worktree", "cwd", "path"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                path = os.path.realpath(os.path.expanduser(value.strip()))
                if os.path.isdir(path):
                    return path
        for key in ("worktreeId", "worktree_id"):
            value = node.get(key)
            if isinstance(value, str) and "::" in value:
                suffix = value.split("::", 1)[1]
                if suffix:
                    path = os.path.realpath(os.path.expanduser(suffix))
                    if os.path.isdir(path):
                        return path
    return ""


def checkout_branch(path):
    if not path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return plain_branch(proc.stdout.strip())


def default_branch_name(repo):
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True,
        )
    except OSError:
        return ""
    if proc.returncode == 0 and proc.stdout.strip():
        return plain_branch(proc.stdout.strip())
    return ""


def paged_orca(sub, key, run):
    rows = []
    cursor = None
    seen = set()
    for _ in range(10):
        args = ["orchestration", sub]
        if run:
            args.extend(["--run", run])
        if cursor:
            if cursor in seen:
                break
            seen.add(cursor)
            args.extend(["--cursor", cursor])
        result = load_orca(args)
        if result is None:
            return rows, bool(rows)
        chunk = result.get(key)
        if not isinstance(chunk, list):
            return rows, bool(rows)
        rows.extend(item for item in chunk if isinstance(item, dict))
        page = result.get("page")
        cursor = page.get("nextCursor") if isinstance(page, dict) else None
        if not cursor:
            return rows, True
    return rows, True


def current_work(repo, run, task):
    if not task:
        return None
    ids = set()
    branches = set()
    known = False
    if run:
        tasks, tasks_ok = paged_orca("task-list", "tasks", run)
        if tasks_ok:
            for row in tasks:
                if task_id_of(row) != task:
                    continue
                known = True
                name = recorded_branch(row.get("result"))
                if name:
                    branches.add(name)
    workers, workers_ok = paged_orca("worker-list", "workers", run)
    if not workers_ok and run:
        workers, workers_ok = paged_orca("worker-list", "workers", "")
    if workers_ok:
        for row in workers:
            if task_id_of(row) != task:
                continue
            known = True
            dispatch = dispatch_id_of(row)
            if dispatch:
                ids.add(dispatch)
            for name in (explicit_branch(row), recorded_branch(row.get("result")), checkout_branch(worker_checkout(row))):
                if name:
                    branches.add(name)
    if not known:
        return None
    default = default_branch_name(repo)
    if default:
        branches.discard(default)
    branches.discard("")
    branches.discard("none")
    ids.add(task)
    return ids, branches


def own_candidate(candidate, ids, branches):
    if candidate.get("kind") == "worker" and str(candidate.get("id") or "") in ids:
        return True
    branch = plain_branch(candidate.get("branch") or "")
    if not branch and candidate.get("kind") == "branch":
        branch = plain_branch(str(candidate.get("id") or ""))
    return bool(branch) and branch in branches


def drop_current_task(repo, candidates, run, task):
    # Identity is the task id, the branch recorded for that task, and the
    # branch checked out in its worktree. Title text is not an identity.
    identity = current_work(repo, run, task)
    if identity is None:
        return candidates
    ids, branches = identity
    return [item for item in candidates if not own_candidate(item, ids, branches)]


def gather(repo, planned, intent, run="", task=""):
    url = origin_url(repo)
    parsed = parse_remote(url or "")
    if not parsed:
        die("origin is missing or not a parseable git remote, so overlap cannot list pull requests")
    prs = pull_requests(parsed["slug"])
    covered = {item["branch"] for item in prs if item["branch"]}
    candidates = list(prs)
    for branch in remote_branches(repo):
        if branch not in covered:
            candidates.append({
                "kind": "branch", "id": branch, "title": branch, "body": "", "branch": branch, "url": "",
                "files": branch_files(repo, branch),
            })
    issues, issues_unavailable = assigned_issues(parsed["slug"])
    workers, workers_unavailable = active_workers()
    candidates.extend(issues)
    candidates.extend(workers)
    candidates = drop_current_task(repo, candidates, run, task)
    planned_files = normalize_files(planned)
    for candidate in candidates:
        candidate["score"] = shared_count(planned_files, candidate["files"])
    candidates.sort(key=lambda item: (-item["score"], KIND_RANK.get(item["kind"], 9), item["id"]))
    return {
        "intent": intent,
        "files": planned_files,
        "sees": "pushed GitHub work only; unpushed local branches are invisible",
        "workersUnavailable": workers_unavailable,
        "issuesUnavailable": issues_unavailable,
        "candidates": candidates,
    }


def classify(intent, planned, candidate):
    shared = set(normalize_files(planned)) & set(normalize_files(candidate.get("files") or []))
    title = (candidate.get("title") or "").strip().lower()
    body = (candidate.get("body") or "").strip().lower()
    text = title + "\n" + body
    intent_text = (intent or "").strip().lower()
    if shared and intent_text and (intent_text in text or (title and title in intent_text)):
        return "duplicate"
    if shared:
        return "related"
    return "unrelated"


def verdict_of(labels):
    actions = [ACTIONS[label] for label in labels]
    if any(action in ("stop", "flag") for action in actions):
        return "stop"
    return "continue"


def overlap_path(run):
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox", run, "overlap.jsonl")


def record_overlap(run, event):
    if not run:
        die("overlap record needs a run id")
    path = overlap_path(run)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(event)
    payload["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_overlap(run):
    path = overlap_path(run)
    if not os.path.isfile(path):
        return []
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def cleared(run, task, checkpoint):
    newest = None
    for event in read_overlap(run):
        if event.get("task") == task and event.get("checkpoint") == checkpoint:
            newest = event
    if not newest:
        return False
    if newest.get("kind") == "answered" and newest.get("answer") == "proceed":
        return True
    if newest.get("kind") == "classified" and newest.get("verdict") == "continue":
        return True
    return False


def apply_classification(data):
    labels = []
    for candidate in data["candidates"]:
        label = classify(data["intent"], data["files"], candidate)
        candidate["label"] = label
        labels.append(label)
    data["verdict"] = verdict_of(labels)
    return data


def flag_map(argv):
    opts = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            opts[arg[2:]] = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--"):
            opts[arg[2:]] = "true"
        index += 1
    return opts


def cmd_overlap(argv):
    opts = flag_map(argv)
    if "record" in opts:
        kind = opts["record"]
        if kind not in ("gathered", "classified", "answered"):
            die("overlap --record is gathered, classified, or answered")
        record_overlap(opts.get("run"), {
            "kind": kind,
            "checkpoint": opts.get("checkpoint"),
            "task": opts.get("task"),
            "run": opts.get("run"),
            "verdict": opts.get("verdict"),
            "answer": opts.get("answer"),
            "intent": opts.get("intent"),
        })
        return 0
    repo = opts.get("repo") or "."
    files = [item for item in (opts.get("files") or "").split(",") if item]
    data = apply_classification(gather(
        repo, files, opts.get("intent") or "", opts.get("run") or "", opts.get("task") or "",
    ))
    print(json.dumps(data, separators=(",", ":")))
    if opts.get("run"):
        checkpoint = opts.get("checkpoint") or "scoped"
        record_overlap(opts.get("run"), {
            "kind": "gathered",
            "checkpoint": checkpoint,
            "task": opts.get("task"),
            "run": opts.get("run"),
            "intent": data["intent"],
            "files": data["files"],
            "candidates": [{"kind": item["kind"], "id": item["id"], "score": item["score"], "label": item["label"], "url": item["url"]} for item in data["candidates"][:8]],
        })
        record_overlap(opts.get("run"), {
            "kind": "classified",
            "checkpoint": checkpoint,
            "task": opts.get("task"),
            "run": opts.get("run"),
            "verdict": data["verdict"],
            "answer": None,
            "intent": opts.get("intent"),
        })
    return 0


def charters_dir():
    return os.path.join(home(), "charters")


def repo_scope(project_dir, cwd, hint=""):
    start = governing_start(project_dir, cwd)
    if not git_stdout(start, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise CharterError(f"{start} is not a git repository{hint}")
    url = origin_url(start)
    if not url:
        raise CharterError(NO_ORIGIN.format(repo=start))
    key = normalize_origin(url)
    if not key:
        raise CharterError(BAD_ORIGIN.format(url=url))
    return key


def _charter_path(scope):
    if scope == GLOBAL:
        return os.path.join(charters_dir(), "global.json")
    parts = scope.removeprefix("https://").split("/")
    if len(parts) != 3 or any(part in ("", ".", "..") for part in parts):
        raise CharterError(f"origin {scope} does not name a charter file")
    return os.path.join(charters_dir(), "repos", parts[0], parts[1], parts[2] + ".json")


def _parse_rules(raw):
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CharterError(f"not JSON: {exc}") from exc
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, dict) or data.get("version") != 1:
        raise CharterError('not {"version": 1, "rules": {...}}')
    if any(not RULE_ID.fullmatch(rule_id) or not isinstance(text, str) for rule_id, text in rules.items()):
        raise CharterError("a rule id or rule text is malformed")
    if len(set(rules.values())) != len(rules):
        raise CharterError("a rule is stored twice")
    return rules


def _load_rules(scope):
    path = _charter_path(scope)
    try:
        with open(path, "rb") as handle:
            return _parse_rules(handle.read())
    except FileNotFoundError:
        return {}
    except (OSError, CharterError) as exc:
        raise CharterError(f"{path} is unreadable ({exc}); fix or remove it") from exc


def _admit(scope, rules, text):
    for rule_id, existing in rules.items():
        if existing == text:
            return rule_id, False
    if not text.strip():
        raise CharterError("charter rule is empty")
    if len(text) > TEXT_CAP:
        raise CharterError(f"charter rule is {len(text)} characters; the limit is {TEXT_CAP}")
    digest = hashlib.sha256(f"{scope}\0{text}".encode()).hexdigest()
    kind = "g-" if scope == GLOBAL else "r-"
    return next(kind + digest[:size] for size in range(6, len(digest) + 1) if kind + digest[:size] not in rules), True


def _scope_label(scope):
    return "every repo" if scope == GLOBAL else scope


def _entry_lines(rule_id, text):
    first, *rest = text.split("\n")
    return [f"- {rule_id} {first}", *("  " + line for line in rest)]


def _charter_add(scope, text):
    path = _charter_path(scope)
    with locked(path):
        rules = _load_rules(scope)
        rule_id, new = _admit(scope, rules, text)
        if new:
            rules[rule_id] = text
            write_json_atomic(path, {"version": 1, "rules": rules})
    return rule_id, new


def _charter_rm(scope, rule_id):
    path = _charter_path(scope)
    with locked(path):
        rules = _load_rules(scope)
        if rule_id not in rules:
            raise CharterError(f"no charter rule {rule_id} for {_scope_label(scope)}; run gantry charter list")
        text = rules.pop(rule_id)
        write_json_atomic(path, {"version": 1, "rules": rules})
    return text


def _read_sections(project_dir, cwd):
    scopes = [GLOBAL]
    try:
        scope = repo_scope(project_dir, cwd)
        _charter_path(scope)
        scopes.append(scope)
    except CharterError:
        pass
    sections = []
    for scope in scopes:
        try:
            sections.append(Section(scope, _load_rules(scope)))
        except CharterError:
            sections.append(Section(scope, None))
    return sections


def _render_charter(sections, cap):
    kept = {section.scope: None if section.rules is None else list(section.rules.items()) for section in sections}
    dropped = 0
    while True:
        lines = []
        for scope, entries in kept.items():
            if entries is None:
                lines += [f"{_scope_label(scope)}:", UNREADABLE_LINE]
            elif entries:
                lines.append(f"{_scope_label(scope)}:")
                for rule_id, text in entries:
                    lines += _entry_lines(rule_id, text)
        if dropped:
            lines.append(OVERFLOW_LINE.format(count=dropped))
        lines = [CHARTER_HEADER, *lines] if lines else []
        sizes = {scope: len(entries) for scope, entries in kept.items() if entries}
        if cap is None or len("\n".join(lines)) <= cap or sum(sizes.values()) <= 1:
            return lines
        largest = max(sizes, key=lambda scope: (sizes[scope], scope == GLOBAL))
        kept[largest].pop(0)
        dropped += 1


def charter_block(project_dir, cwd):
    try:
        return _render_charter(_read_sections(project_dir, cwd or os.getcwd()), CHARTER_CAP)
    except Exception:
        return [CHARTER_HEADER, UNREADABLE_LINE]


def _charter_doctor_line(sections):
    counts = ["unreadable" if section.rules is None else str(len(section.rules)) for section in sections]
    return f"charter: every repo {counts[0]}, this repo {counts[1] if len(counts) > 1 else 'none'}"


def cmd_charter(argv):
    verb, args = (argv[0], argv[1:]) if argv else ("", [])
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if verb == "add":
        every = args[:1] == ["--global"]
        if every:
            args = args[1:]
        if len(args) != 1:
            raise CharterError("charter add takes the rule as one quoted argument")
        scope = GLOBAL if every else repo_scope(project_dir, os.getcwd(), "; add --global for a rule that applies in every repo")
        rule_id, new = _charter_add(scope, args[0])
        print(f"{rule_id} {'saved' if new else 'already saved'} for {_scope_label(scope)}: {args[0]}")
        return 0
    if verb == "list":
        if args:
            raise CharterError("charter list takes no arguments")
        _charter_path(repo_scope(project_dir, os.getcwd()))
        sections = _read_sections(project_dir, os.getcwd())
        lines = _render_charter(sections, None)
        if lines:
            print("\n".join(lines))
        unreadable = [section.scope for section in sections if section.rules is None]
        for scope in unreadable:
            print(f"gantry: {_charter_path(scope)} does not parse; fix or remove it", file=sys.stderr)
        return 1 if unreadable else 0
    if verb == "rm":
        if len(args) != 1:
            raise CharterError("charter rm takes one id; run gantry charter list")
        match = RULE_ID.fullmatch(args[0])
        if not match:
            raise CharterError(f"{args[0]} is not a charter id; run gantry charter list")
        scope = GLOBAL if match["kind"] == "g" else repo_scope(project_dir, os.getcwd())
        text = _charter_rm(scope, args[0])
        print(f"{args[0]} removed from {_scope_label(scope)}: {text}")
        return 0
    raise CharterError("charter takes add, list, or rm")


def require_launch(restart):
    root, _common = checkout_of(".")
    if os.path.realpath(os.getcwd()) != os.path.realpath(root):
        die(f"run gantry from the main checkout ({root}), not a worktree")
    if not os.path.isfile(agent_path()):
        die("gantry is not installed here; run: gantry install")
    if not os.environ.get("ORCA_TERMINAL_HANDLE"):
        die("run gantry inside an Orca terminal so the coordinator can bind a Run")
    found = governing(None, root)
    if found is None:
        die("gantry is not installed here; run: gantry install")
    if restart:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gantry_run.py")
        installed = kit_file("gantry_run.py")
        if os.path.isfile(installed):
            script = installed
        subprocess.run([sys.executable, script, "write"], check=True)
    return root


def autocompact_arg():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gantry_run
    tokens = gantry_run.autocompact_tokens(gantry_run.load_config(os.getcwd()))
    if not tokens:
        return []
    return ["--autocompact", str(tokens)]


def exec_claude(argv):
    os.environ["GANTRY_COORDINATOR"] = "1"
    os.environ["GANTRY_COORDINATOR_HANDLE"] = os.environ["ORCA_TERMINAL_HANDLE"]
    bind_pstack()
    os.execvp("claude", [
        "claude", *argv, *autocompact_arg(),
        "--agent", AGENT_NAME, "--dangerously-skip-permissions",
        "--disallowedTools", "Edit,Write,NotebookEdit,Agent,Task,Workflow,Monitor",
    ])


def cmd_start(argv):
    require_launch(False)
    exec_claude(argv)


def cmd_restart(argv):
    if argv:
        die("gantry restart takes no objective; it continues the Run this terminal is bound to")
    os.environ["GANTRY_RESTART"] = "1"
    require_launch(True)
    exec_claude([RESTART_PROMPT])


def cmd_governed(argv):
    if "--repo" in argv:
        found = look(argv[argv.index("--repo") + 1], warn=True)
    else:
        found = governing(os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd(), warn=True)
    print("yes" if found else "no")
    return 0


PR_NUMBER = re.compile(r"(?:https://github\.com/([^/]+)/([^/]+)/pull/|#)?(\d+)$")
PR_FIELDS = "number,state,baseRefName,headRefName,headRefOid,statusCheckRollup,mergeStateStatus,url,isDraft"
SHIP_VALUED = {"--task", "--run"}
CLEAN_VALUED = {"--run"}
CLEAN_FLAGS = {"--dry-run", "--scan", "--finished", "--run"}
SETTLED_STATUS = {"completed", "failed"}
WAIT_TIMEOUT_MS = 180000
WAIT_HEARTBEAT_MS = 180000
WAIT_STALL_MS = 300000
WAIT_MAX_MS = 200000
WAIT_POLL_S = 1
WAIT_QUIET = {"heartbeat", "status"}
WAIT_BLOCKING = {"question", "escalation", "pre-pr", "pre_pr", "prepr"}


def invoke(argv, cwd=None, timeout=30):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def option_name(token):
    if token.startswith("--") and "=" in token[2:]:
        return token.split("=", 1)[0]
    return token


def banned_option(token):
    name = option_name(token)
    if name in {"--admin", "--auto", "--force", "-f"}:
        return True
    if name.startswith("--force") or name.startswith("--admin") or name.startswith("--auto"):
        return True
    if name.startswith("-") and not name.startswith("--") and "f" in name[1:]:
        return True
    return False


def take_options(argv, valued):
    flags, positionals = [], []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positionals.extend(argv[index + 1:])
            break
        if token.startswith("-") and token != "-":
            if banned_option(token):
                die("gantry ship and gantry cleanup reject --admin, --auto, and --force")
            name = option_name(token)
            if token.startswith("--") and "=" in token:
                flags.append((name, token.split("=", 1)[1]))
                index += 1
                continue
            if name in valued:
                if index + 1 >= len(argv):
                    die(f"{name} needs a value")
                flags.append((name, argv[index + 1]))
                index += 2
                continue
            flags.append((name, ""))
            index += 1
            continue
        positionals.append(token)
        index += 1
    return flags, positionals


def paired_flags(flags):
    found = {}
    for name, value in flags:
        found[name] = value
    return found


def emit(payload, code=0):
    print(json.dumps(payload, separators=(",", ":")))
    return code


def read_auto_merge(common):
    path = os.path.join(common, "gantry.json")
    if not os.path.isfile(path):
        return True
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        die(".git/gantry.json must be a JSON object")
    if not isinstance(data, dict):
        die(".git/gantry.json must be a JSON object")
    if "autoMerge" not in data:
        return True
    value = data["autoMerge"]
    if not isinstance(value, bool):
        die("autoMerge in .git/gantry.json must be true or false")
    return value


def repo_context():
    cwd = os.getcwd()
    common = git_stdout(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        die("not a git repository")
    common = os.path.realpath(common)
    root = os.path.dirname(common) if os.path.basename(common) == ".git" else os.path.realpath(cwd)
    url = git_stdout(cwd, "config", "--get", "remote.origin.url") or ""
    parsed = parse_remote(url)
    if not parsed:
        die(BAD_ORIGIN.format(url=url or "(missing)"))
    ref = git_stdout(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default = ref[len("origin/"):] if ref and ref.startswith("origin/") else "main"
    return {
        "root": root,
        "common": common,
        "slug": parsed["slug"],
        "default": default,
        "auto": read_auto_merge(common),
    }


def run_json(argv):
    try:
        code, out, err = invoke(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:400]
    if code != 0:
        return None, (err or out or "gh failed").strip()[:400]
    try:
        return json.loads(out or "null"), ""
    except ValueError:
        return None, "gh did not return JSON"


def orca_call(*args):
    try:
        code, out, err = invoke(["orca", *args, "--json"], timeout=25)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:400]
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        return None, (err or "orca did not return JSON").strip()[:400]
    if code != 0 or not isinstance(data, dict) or not data.get("ok"):
        message = data.get("error") if isinstance(data, dict) else None
        return None, str(message or err or "orca failed").strip()[:400]
    result = data.get("result")
    return (result if isinstance(result, dict) else {}), ""


def bound_run(explicit):
    if explicit:
        return explicit, ""
    result, err = orca_call("orchestration", "run-current")
    if result is None:
        return None, err
    run = result.get("run")
    if isinstance(run, dict) and run.get("id"):
        return run["id"], ""
    if isinstance(run, str) and run:
        return run, ""
    if result.get("id"):
        return result["id"], ""
    return None, "this terminal has no bound run"


def paged(sub, run):
    key = {"task-list": "tasks", "worker-list": "workers"}[sub]
    rows, cursor = [], None
    for _ in range(20):
        args = ["orchestration", sub, "--run", run]
        if cursor:
            args.extend(["--cursor", cursor])
        result, err = orca_call(*args)
        if result is None:
            return None, err
        rows.extend(result.get(key) or [])
        cursor = (result.get("page") or {}).get("nextCursor")
        if not cursor:
            return rows, ""
    return rows, ""


def mailbox_dir(run):
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox", run)


def read_jsonl(path):
    rows = []
    try:
        handle = open(path)
    except OSError:
        return rows
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def record_from_text(text, task):
    pr = branch = ""
    found = task or ""
    for token in str(text or "").replace(",", " ").split():
        if token.startswith("pr=") and token not in ("pr=none", "pr="):
            pr = token[len("pr="):]
        elif token.startswith("branch=") and token not in ("branch=none", "branch="):
            branch = token[len("branch="):]
        elif token.startswith("task=") and token not in ("task=none", "task=") and not found:
            found = token[len("task="):]
    if not pr and not branch:
        return None
    return {"task": found, "pr": pr, "branch": branch}


def add_record(found, text, task):
    row = record_from_text(text, task)
    if row:
        found.append(row)


def load_records(run, tasks):
    found = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id") or ""
        result = task.get("result")
        if isinstance(result, (dict, list)):
            result = json.dumps(result)
        add_record(found, result, task_id)
    if not run:
        return found
    for row in read_jsonl(os.path.join(mailbox_dir(run), "journal.jsonl")):
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        task_id = ""
        if isinstance(payload, dict):
            task_id = payload.get("taskId") or payload.get("task") or ""
        add_record(found, row.get("body"), task_id)
        if isinstance(payload, dict):
            add_record(found, json.dumps(payload), task_id)
    return found


def pr_matches(recorded, number):
    if not recorded or not number:
        return False
    text = str(recorded).rstrip("/")
    if text in (str(number), f"#{number}"):
        return True
    return text.endswith(f"/pull/{number}")


def parse_pr_token(token, slug):
    match = PR_NUMBER.fullmatch((token or "").strip())
    if not match:
        die("gantry ship needs a pull request number or https://github.com/<owner>/<repo>/pull/<n>")
    owner, repo, number = match.group(1), match.group(2), match.group(3)
    if owner and repo:
        want = f"{owner}/{repo}"
        if slug != want and not slug.endswith("/" + want):
            die(f"pull request {number} is not in {slug}")
    return number


def safe_branch(name, default):
    if not name or name == default or name in {"HEAD", "origin"}:
        return False
    if name.startswith(("-", "+", ":")):
        return False
    if any(ch in name for ch in " \t~^:?*[\\"):
        return False
    return True


def delete_push(repo, branch, default):
    if not safe_branch(branch, default):
        return None
    argv = ["git", "-C", repo, "push", "origin", "--delete", branch]
    if "--force" in argv or "-f" in argv or any(token.startswith("+") for token in argv):
        return None
    return argv


CHECK_POLLS = 4
CHECK_PAUSE = 3


def pause(seconds):
    time.sleep(seconds)


def checks_verdict(view):
    """green, blocked, or empty. Empty is no rollup yet, not a failing check."""
    if view.get("isDraft") is True:
        return "blocked", "pull request is a draft"
    rollup = view.get("statusCheckRollup")
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list):
        return "blocked", "could not read checks"
    if not rollup:
        return "empty", "no checks"
    for item in rollup:
        if not isinstance(item, dict) or item.get("conclusion") != "SUCCESS":
            return "blocked", "checks are not green"
    state = view.get("mergeStateStatus")
    if state not in (None, "CLEAN", "HAS_HOOKS"):
        return "blocked", f"merge state is {state}"
    return "green", ""


def gh_api(path):
    try:
        code, out, err = invoke(["gh", "api", path])
    except (OSError, subprocess.TimeoutExpired):
        return None, "error"
    if code == 0:
        try:
            return json.loads(out or "null"), ""
        except ValueError:
            return None, "error"
    text = f"{err or ''}\n{out or ''}"
    return None, _api_failure(text)


def _api_failure(text):
    """absent is a missing resource. plan is a free private repo that cannot enable protection."""
    lowered = text.lower()
    if "upgrade to github pro" in lowered or "plan-not-available" in lowered:
        return "plan"
    if "404" in text or "Not Found" in text:
        return "absent"
    return "error"


def _nonempty(value):
    return isinstance(value, list) and len(value) > 0


def checks_configured(slug, branch):
    """True when this repo expects checks. False when none are configured. None when unknown.

    Branch protection and rulesets on a private repo whose plan cannot enable
    them return HTTP 403 ("Upgrade to GitHub Pro" or plan-not-available). That
    feature cannot exist there, so the 403 is no required checks. Any other
    error stays unknown. A plan response from the workflows list stays unknown.
    """
    unknown = False
    configured = False
    workflows, werr = gh_api(f"repos/{slug}/actions/workflows")
    if werr in ("error", "plan"):
        unknown = True
    elif isinstance(workflows, dict):
        count = workflows.get("total_count")
        if (isinstance(count, int) and count > 0) or _nonempty(workflows.get("workflows")):
            configured = True
    encoded = quote(branch or "main", safe="")
    protection, perr = gh_api(f"repos/{slug}/branches/{encoded}/protection/required_status_checks")
    if perr == "error":
        unknown = True
    elif isinstance(protection, dict):
        if _nonempty(protection.get("contexts")) or _nonempty(protection.get("checks")):
            configured = True
    rules, rerr = gh_api(f"repos/{slug}/rules/branches/{encoded}")
    if rerr == "error":
        unknown = True
    elif rerr != "plan":
        rows = rules
        if isinstance(rules, dict):
            rows = rules.get("rules") or []
        if isinstance(rows, list):
            for rule in rows:
                if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                    continue
                params = rule.get("parameters")
                if not isinstance(params, dict):
                    configured = True
                    continue
                if _nonempty(params.get("required_status_checks")):
                    configured = True
    if configured:
        return True
    if unknown:
        return None
    return False


def resolve_checks(number, slug, view):
    """Return the latest view and whether it may merge.

    No Actions workflows and no required status checks means an empty rollup is
    the whole story: nothing is configured, so the pull request can land. An
    empty rollup when checks are configured is the post-push race. Poll a few
    times. Red, pending, and failing checks block without waiting.
    """
    kind, why = checks_verdict(view)
    if kind == "green":
        return view, True, ""
    if kind == "blocked":
        return view, False, why
    expected = checks_configured(slug, view.get("baseRefName") or "")
    if expected is False:
        state = view.get("mergeStateStatus")
        if state not in (None, "CLEAN", "HAS_HOOKS"):
            return view, False, f"merge state is {state}"
        return view, True, ""
    last = "checks have not registered"
    for _ in range(CHECK_POLLS):
        pause(CHECK_PAUSE)
        nxt, err = run_json(["gh", "pr", "view", number, "--repo", slug, "--json", PR_FIELDS])
        if not isinstance(nxt, dict):
            return view, False, err or "could not read the pull request"
        view = nxt
        kind, why = checks_verdict(view)
        if kind == "green":
            return view, True, ""
        if kind == "blocked":
            return view, False, why
        last = "checks have not registered"
    return view, False, last


def matching_records(records, number, head, task_id):
    matched = []
    for rec in records:
        if task_id and rec.get("task") and rec["task"] != task_id:
            continue
        if pr_matches(rec.get("pr"), number) or (rec.get("branch") and rec["branch"] == head):
            matched.append(rec)
    return matched


def task_of_records(matched, requested):
    if requested:
        return requested
    for rec in matched:
        if rec.get("task"):
            return rec["task"]
    return ""


def stack_reason(view, number, default, opens):
    base = view.get("baseRefName") or ""
    head = view.get("headRefName") or ""
    if base and base != default:
        return f"base {base} is not {default}; dispatch the ship worker"
    for row in opens:
        if not isinstance(row, dict):
            continue
        if str(row.get("number")) == str(number):
            continue
        if head and row.get("baseRefName") == head:
            return f"pull {row.get('number')} targets {head}; dispatch the ship worker"
    return ""


def mark_completed(task_id):
    if not task_id:
        return False, "no task id"
    result, err = orca_call("orchestration", "task-update", "--task", task_id, "--status", "completed")
    if result is None:
        if "completed" in (err or "").lower():
            return True, "already completed"
        return False, err or "task-update failed"
    return True, "completed"


def release_dispatch(dispatch):
    if not dispatch:
        return True, ""
    result, err = orca_call("orchestration", "worker-release", "--dispatch", dispatch)
    if result is None:
        if "release" in (err or "").lower():
            return True, ""
        return False, err or "worker-release failed"
    return True, ""


def ship_payload(number, status, reason, **extra):
    payload = {"pr": str(number), "status": status, "reason": reason, "deleted": False}
    payload.update(extra)
    return payload


def cmd_ship(argv):
    flags, args = take_options(argv, SHIP_VALUED)
    known = paired_flags(flags)
    unknown = set(known) - SHIP_VALUED
    if unknown:
        die("gantry ship accepts only <pr>, --task, and --run")
    if len(args) != 1:
        die("usage: gantry ship <pr> [--task <id>] [--run <id>]")
    ctx = repo_context()
    number = parse_pr_token(args[0], ctx["slug"])
    if not ctx["auto"]:
        return emit(ship_payload(number, "held", "autoMerge is false in .git/gantry.json"))
    run, run_err = bound_run(known.get("--run"))
    tasks = []
    if run:
        loaded, _err = paged("task-list", run)
        tasks = loaded or []
    records = load_records(run, tasks)
    if not run and not records:
        return emit(ship_payload(number, "blocked", run_err or "could not read the run, so nothing was merged"))
    view, err = run_json(["gh", "pr", "view", number, "--repo", ctx["slug"], "--json", PR_FIELDS])
    if not isinstance(view, dict):
        return emit(ship_payload(number, "blocked", err or "could not read the pull request"), 1)
    head = view.get("headRefName") or ""
    url = view.get("url") or ""
    base = {
        "url": url,
        "branch": head,
        "task": known.get("--task") or "",
    }
    if not safe_branch(head, ctx["default"]):
        return emit(ship_payload(number, "blocked", "head is the default branch or is not a task branch", **base))
    matched = matching_records(records, number, head, known.get("--task"))
    if not matched:
        return emit(ship_payload(number, "blocked", "this run has no record that it opened this pull request", **base))
    task_id = task_of_records(matched, known.get("--task"))
    base["task"] = task_id
    state = str(view.get("state") or "").upper()
    if state == "CLOSED":
        return emit(ship_payload(number, "blocked", "pull request is closed", **base))
    if state != "MERGED":
        view, green, why = resolve_checks(number, ctx["slug"], view)
        if isinstance(view, dict):
            head = view.get("headRefName") or head
            url = view.get("url") or url
            base["branch"] = head
            base["url"] = url
        if not green:
            return emit(ship_payload(number, "blocked", why, **base))
        if not safe_branch(head, ctx["default"]):
            return emit(ship_payload(number, "blocked", "head is the default branch or is not a task branch", **base))
        opens, list_err = run_json([
            "gh", "pr", "list", "--repo", ctx["slug"], "--state", "open",
            "--json", "number,baseRefName,headRefName",
        ])
        if not isinstance(opens, list):
            return emit(ship_payload(number, "blocked", list_err or "could not list open pull requests", **base), 1)
        stacked = stack_reason(view, number, ctx["default"], opens)
        if stacked:
            return emit(ship_payload(number, "stack", stacked, **base))
        oid = view.get("headRefOid") or ""
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", oid):
            return emit(ship_payload(number, "blocked", "refusing to merge without the head sha", **base))
        merge = ["gh", "pr", "merge", number, "--repo", ctx["slug"], "--squash", "--match-head-commit", oid]
        if {"--admin", "--auto", "--force", "-f", "--delete-branch"} & set(merge):
            die("refusing an unsafe merge")
        try:
            code, out, err = invoke(merge)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return emit(ship_payload(number, "blocked", str(exc)[:400], **base), 1)
        if code != 0:
            return emit(ship_payload(number, "blocked", (err or out or "merge failed").strip()[:400], **base), 1)
    owned_name = head if any(rec.get("branch") == head for rec in matched) else ""
    deleted = False
    delete_note = ""
    argv = delete_push(ctx["root"], owned_name, ctx["default"]) if owned_name else None
    if argv:
        try:
            code, out, err = invoke(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            delete_note = str(exc)[:200]
            code = 1
        if code == 0:
            deleted = True
        else:
            text = (err or out or "").strip()
            delete_note = "" if "remote ref does not exist" in text or "does not exist" in text else text[:200]
    elif not owned_name:
        delete_note = "head branch was not recorded for this run"
    ok, state_note = mark_completed(task_id)
    reason = "squash-merged" if state != "MERGED" else "already merged"
    if delete_note:
        reason = f"{reason}; {delete_note}"
    if not ok:
        reason = f"{reason}; {state_note}"
    return emit(ship_payload(
        number, "landed", reason, deleted=deleted, state=state_note, **base,
    ), 0 if ok else 1)


def worker_path(row):
    for key in ("worktreePath", "worktree", "cwd", "path"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return os.path.realpath(os.path.expanduser(value.strip()))
    return ""


def live_paths(workers):
    found = set()
    for row in workers:
        if row.get("dispatchStatus") in SETTLED_STATUS:
            continue
        path = worker_path(row)
        if path:
            found.add(path)
    return found


def github_merged(slug, branch):
    data, err = run_json([
        "gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "merged", "--json", "number",
    ])
    if not isinstance(data, list):
        return None, err or "could not ask GitHub"
    return len(data) > 0, ""


def head_on_origin(repo, branch):
    try:
        code, out, err = invoke(["git", "-C", repo, "ls-remote", "--exit-code", "--heads", "origin", branch])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:200]
    if code == 0:
        return True, ""
    if code == 2:
        return False, ""
    return None, (err or out or "ls-remote failed").strip()[:200]


def ahead_count(path, default):
    try:
        code, out, err = invoke(["git", "-C", path, "rev-list", "--count", f"origin/{default}..HEAD"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:200]
    if code != 0:
        return None, (err or out or "rev-list failed").strip()[:200]
    return out.strip(), ""


def branch_removable(path, default, slug):
    branch = git_stdout(path, "symbolic-ref", "--short", "-q", "HEAD") or ""
    if not safe_branch(branch, default):
        return False, "not a task branch"
    merged, err = github_merged(slug, branch)
    if merged is None:
        return False, err
    if merged:
        return True, "merged"
    present, err = head_on_origin(path, branch)
    if present is None:
        return False, err
    if present:
        return False, "branch is unmerged"
    count, err = ahead_count(path, default)
    if count is None:
        return False, err
    if count == "0":
        return True, "gone from origin with no commits of its own"
    return False, "branch has commits that are not on the default branch"


def plan_cleanup(task_id, workers, ctx, cwd, live):
    own = os.environ.get("ORCA_TERMINAL_HANDLE") or ""
    if any(row.get("dispatchStatus") not in SETTLED_STATUS for row in workers):
        return {"task": task_id, "op": "skip", "reason": "worker still running", "path": "", "handles": [], "dispatches": []}
    path = ""
    handles, dispatches = [], []
    for row in workers:
        handle = row.get("agentTerminalHandle") or ""
        if handle and handle != own and handle not in handles:
            handles.append(handle)
        dispatch = row.get("dispatchId") or ""
        if dispatch and dispatch not in dispatches:
            dispatches.append(dispatch)
        if not path:
            path = worker_path(row)
    if path and path in live:
        return {"task": task_id, "op": "skip", "reason": "worktree of a worker that is still running", "path": path, "handles": [], "dispatches": []}
    if path and os.path.realpath(path) == ctx["root"]:
        return {"task": task_id, "op": "close", "reason": "main checkout stays", "path": path, "handles": handles, "dispatches": dispatches}
    if path and os.path.realpath(path) == os.path.realpath(cwd):
        return {"task": task_id, "op": "skip", "reason": "worktree is the caller cwd", "path": path, "handles": [], "dispatches": []}
    if not path or not os.path.exists(path):
        if handles or path:
            return {
                "task": task_id,
                "op": "close",
                "reason": "worktree already gone" if path else "no worktree",
                "path": path,
                "handles": handles,
                "dispatches": dispatches,
            }
        return {"task": task_id, "op": "skip", "reason": "no worktree", "path": "", "handles": [], "dispatches": []}
    try:
        code, out, err = invoke(["git", "-C", path, "status", "--porcelain"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"task": task_id, "op": "skip", "reason": str(exc)[:200], "path": path, "handles": [], "dispatches": []}
    if code != 0:
        return {"task": task_id, "op": "skip", "reason": (err or out or "status failed").strip()[:200], "path": path, "handles": [], "dispatches": []}
    if out.strip():
        return {"task": task_id, "op": "skip", "reason": "worktree is dirty", "path": path, "handles": [], "dispatches": []}
    ok, why = branch_removable(path, ctx["default"], ctx["slug"])
    if not ok:
        return {"task": task_id, "op": "skip", "reason": why, "path": path, "handles": [], "dispatches": []}
    return {"task": task_id, "op": "remove", "reason": why, "path": path, "handles": handles, "dispatches": dispatches}


def act_cleanup(plan, ctx):
    closed, errors = [], []
    for handle in plan["handles"]:
        try:
            code, out, err = invoke(["orca", "terminal", "close", "--terminal", handle, "--json"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc)[:200])
            continue
        if code != 0:
            errors.append((err or out or "terminal close failed").strip()[:200])
        else:
            closed.append(handle)
    removed = ""
    remove_error = ""
    if plan["op"] == "remove":
        argv = ["git", "-C", ctx["root"], "worktree", "remove", plan["path"]]
        if "--force" in argv or "-f" in argv:
            die("gantry cleanup never uses --force")
        try:
            code, out, err = invoke(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            remove_error = str(exc)[:200]
            code = 1
        if code != 0 and not remove_error:
            remove_error = (err or out or "worktree remove failed").strip()[:200]
        elif code == 0:
            removed = plan["path"]
    state = ""
    ok = True
    if plan["op"] != "remove" or removed:
        ok, state = mark_completed(plan["task"])
        if not ok:
            errors.append(state)
        for dispatch in plan["dispatches"]:
            released, note = release_dispatch(dispatch)
            if not released:
                errors.append(note)
    if remove_error:
        errors.append(remove_error)
        status = "error"
    elif not ok:
        status = "error"
    elif removed:
        status = "removed"
    else:
        status = "closed"
    return {
        "task": plan["task"],
        "status": status,
        "op": plan["op"],
        "reason": plan["reason"],
        "path": plan["path"],
        "removed": removed,
        "closed": closed,
        "state": state,
        "errors": errors,
    }


def cmd_cleanup(argv):
    flags, tasks = take_options(argv, CLEAN_VALUED)
    known = paired_flags(flags)
    unknown = set(known) - CLEAN_FLAGS
    if unknown:
        die("gantry cleanup accepts a task id, --scan, --finished, --dry-run, and --run")
    scan = "--scan" in known
    finished = "--finished" in known
    dry = "--dry-run" in known or scan
    if scan and (tasks or finished):
        die("gantry cleanup --scan takes no tasks")
    if finished and tasks:
        die("gantry cleanup --finished takes no tasks")
    if not scan and not finished and not tasks:
        die("usage: gantry cleanup <task> [--dry-run] [--run <id>]")
    ctx = repo_context()
    runs, err = cleanup_runs(known.get("--run"))
    if not runs:
        die(err or "no run")
    workers, errors = collect_workers(runs)
    if errors and not workers:
        die(errors[0])
    by_task = {}
    for row in workers:
        task_id = row.get("taskId") or ""
        if task_id:
            by_task.setdefault(task_id, []).append(row)
    if scan or finished:
        chosen = sorted(by_task)
    else:
        chosen = tasks
    live = live_paths(workers)
    code = 0
    for task_id in chosen:
        rows = by_task.get(task_id) or []
        if not rows:
            line = {"task": task_id, "status": "skipped", "op": "skip", "reason": "no worker for this task", "path": "", "removed": "", "closed": [], "state": "", "errors": []}
            print(json.dumps(line, separators=(",", ":")))
            continue
        plan = plan_cleanup(task_id, rows, ctx, os.getcwd(), live)
        if dry or (finished and plan["op"] == "skip") or plan["op"] == "skip":
            if not dry and finished and plan["op"] == "skip":
                shown = "skipped"
            elif dry:
                shown = "dry-run"
            else:
                shown = "skipped"
            line = {
                "task": task_id,
                "status": shown,
                "op": plan["op"],
                "reason": plan["reason"],
                "path": plan["path"],
                "removed": "",
                "closed": [],
                "state": "",
                "errors": [],
            }
            print(json.dumps(line, separators=(",", ":")))
            continue
        line = act_cleanup(plan, ctx)
        if line["status"] == "error":
            code = 1
        print(json.dumps(line, separators=(",", ":")))
    return code


def cleanup_runs(explicit):
    if explicit:
        return [explicit], ""
    result, err = orca_call("orchestration", "run-list")
    if result is None:
        current, err2 = bound_run(None)
        if current:
            return [current], ""
        return None, err or err2
    raw = result.get("runs") if isinstance(result, dict) else None
    runs = []
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("id"):
                runs.append(row["id"])
            elif isinstance(row, str) and row:
                runs.append(row)
    if not runs:
        current, err2 = bound_run(None)
        if current:
            return [current], ""
        return None, "no run"
    return runs, ""


def collect_workers(runs):
    rows, errors = [], []
    for run in runs:
        page, err = paged("worker-list", run)
        if page is None:
            errors.append(err or f"could not list workers for {run}")
            continue
        for row in page:
            if isinstance(row, dict):
                copied = dict(row)
                copied["_run"] = run
                rows.append(copied)
    return rows, errors


def monotonic_now():
    return time.monotonic()


def wall_now():
    return datetime.now(timezone.utc)


def start_mailbox(run):
    """Start the mailbox daemon when this Run does not already have one."""
    if fcntl is None:
        return
    directory = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox", run)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        fcntl.flock(lock, fcntl.LOCK_UN)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gantry-mailbox")
    subprocess.Popen(
        [sys.executable, script, "daemon", run],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pause(1)


def wait_dir(run):
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gantry-mailbox", run)
    os.makedirs(path, exist_ok=True)
    return path


def wait_payload(status, reason="", **extra):
    payload = {"status": status}
    if reason:
        payload["reason"] = reason
    for key, value in extra.items():
        if value is None or value == "" or value == []:
            continue
        payload[key] = value
    return payload


def parse_ms(text, name, cap=None):
    try:
        value = int(text)
    except (TypeError, ValueError):
        die(f"{name} needs an integer number of milliseconds")
    if value <= 0:
        die(f"{name} must be greater than 0")
    if cap is not None and value > cap:
        die(f"{name} must be {cap} ms or less")
    return value


def parse_wait_args(argv):
    timeout_ms, heartbeat_ms, stall_ms = WAIT_TIMEOUT_MS, WAIT_HEARTBEAT_MS, WAIT_STALL_MS
    run, dispatches = "", []
    index = 0
    valued = {"--run": "run", "--timeout-ms": "timeout", "--heartbeat-ms": "heartbeat", "--stall-ms": "stall", "--dispatch": "dispatch"}
    while index < len(argv):
        token = argv[index]
        if token == "--":
            die("usage: gantry wait [--run <id>] [--timeout-ms N] [--heartbeat-ms N] [--stall-ms N] [--dispatch <id>]")
        if not token.startswith("-"):
            die("usage: gantry wait [--run <id>] [--timeout-ms N] [--heartbeat-ms N] [--stall-ms N] [--dispatch <id>]")
        name = option_name(token)
        if name not in valued:
            die("gantry wait accepts only --run, --timeout-ms, --heartbeat-ms, --stall-ms, and --dispatch")
        if "=" in token and token.startswith("--"):
            value = token.split("=", 1)[1]
            index += 1
        else:
            if index + 1 >= len(argv):
                die(f"{name} needs a value")
            value = argv[index + 1]
            index += 2
        kind = valued[name]
        if kind == "run":
            run = value
        elif kind == "dispatch":
            if value:
                dispatches.append(value)
        elif kind == "timeout":
            timeout_ms = parse_ms(value, name, WAIT_MAX_MS)
        elif kind == "heartbeat":
            heartbeat_ms = parse_ms(value, name)
        else:
            stall_ms = parse_ms(value, name)
    return {
        "run": run,
        "timeout_ms": timeout_ms,
        "heartbeat_ms": heartbeat_ms,
        "stall_ms": stall_ms,
        "dispatches": dispatches,
    }


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def stamp_age(value):
    parsed = parse_stamp(value)
    if parsed is None:
        return None
    return (wall_now() - parsed).total_seconds()


def heartbeat_of(node):
    found = ""

    def walk(value):
        nonlocal found
        if found or not isinstance(value, dict):
            return
        stamp = value.get("lastHeartbeatAt")
        if isinstance(stamp, str) and stamp.strip():
            found = stamp.strip()
            return
        for child in value.values():
            if isinstance(child, dict):
                walk(child)

    walk(node if isinstance(node, dict) else {})
    return found


def row_dispatch(row):
    nested = row.get("dispatch") if isinstance(row.get("dispatch"), dict) else {}
    return row.get("dispatchId") or nested.get("id") or ""


def row_task(row):
    nested = row.get("dispatch") if isinstance(row.get("dispatch"), dict) else {}
    return row.get("taskId") or nested.get("taskId") or ""


def row_status(row):
    nested = row.get("dispatch") if isinstance(row.get("dispatch"), dict) else {}
    return row.get("dispatchStatus") or nested.get("status") or ""


def load_wait_state(run):
    path = os.path.join(wait_dir(run), "wait.json")
    empty = {"confirmed": {}, "reported": {}, "since": {}}
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    for key in empty:
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return {"confirmed": data["confirmed"], "reported": data["reported"], "since": data["since"]}


def save_wait_state(run, state):
    path = os.path.join(wait_dir(run), "wait.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(state, handle, separators=(",", ":"))
    os.replace(tmp, path)


def read_journal(run):
    path = os.path.join(wait_dir(run), "journal.jsonl")
    rows = []
    try:
        handle = open(path)
    except OSError:
        return rows
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_cursor(run):
    path = os.path.join(wait_dir(run), "cursor")
    try:
        return max(0, int(open(path).read() or 0))
    except (OSError, ValueError):
        return 0


def write_cursor(run, value):
    path = os.path.join(wait_dir(run), "cursor")
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(str(value))
    os.replace(tmp, path)


def payload_dict(message):
    payload = message.get("payload")
    if isinstance(payload, str) and payload:
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def note_heartbeat(confirmed, dispatch, stamp, live):
    if not dispatch:
        return
    if live:
        confirmed[dispatch] = iso(wall_now())
        return
    if not stamp:
        return
    incoming = parse_stamp(stamp)
    current = parse_stamp(confirmed.get(dispatch))
    if incoming is None:
        if dispatch not in confirmed:
            confirmed[dispatch] = stamp
        return
    if current is None or incoming >= current:
        confirmed[dispatch] = stamp


def event_status(kind, payload, subject):
    if kind in ("pre-pr", "pre_pr", "prepr"):
        return "pre-pr"
    checkpoint = payload.get("checkpoint") or ""
    if kind not in WAIT_QUIET and (checkpoint == "pre-pr" or str(subject or "").strip().lower() == "pre-pr"):
        return "pre-pr"
    return kind


def message_fields(message):
    from gantry_run import project_message, project_pr

    projected = project_message(message)
    payload = payload_dict(message)
    kind = projected.get("type") or ""
    status = event_status(kind, payload, projected.get("subject"))
    files = payload.get("files") or ""
    if isinstance(files, list):
        files = ",".join(str(item) for item in files if item)
    fields = {
        "id": projected.get("id") or "",
        "task": projected.get("task") or payload.get("taskId") or payload.get("task") or "",
        "dispatch": projected.get("dispatch") or payload.get("dispatchId") or payload.get("dispatch") or "",
        "subject": projected.get("subject") or "",
        "body": projected.get("body") or "",
        "phase": projected.get("phase") or "",
        "outcome": projected.get("outcome") or payload.get("outcome") or "",
        "options": projected.get("options") or "",
        "intent": payload.get("intent") or "",
        "files": files if isinstance(files, str) else "",
    }
    if status == "worker_done":
        pr, branch, outcome = project_pr(message.get("body") or projected.get("body") or "")
        if pr:
            fields["pr"] = pr
        if branch:
            fields["branch"] = branch
        if outcome and not fields["outcome"]:
            fields["outcome"] = outcome
    return status, fields


def latest_types(messages):
    found = {}
    for message in messages:
        status, fields = message_fields(message)
        dispatch = fields.get("dispatch") or ""
        if dispatch:
            found[dispatch] = status or found.get(dispatch) or ""
    return found


def watched_dispatches(explicit, rows):
    if explicit:
        return list(explicit)
    found = []
    for row in rows:
        if not isinstance(row, dict) or row_status(row) in SETTLED_STATUS:
            continue
        dispatch = row_dispatch(row)
        if dispatch and dispatch not in found:
            found.append(dispatch)
    return found


def remember_tasks(tasks, rows):
    for row in rows:
        if not isinstance(row, dict):
            continue
        dispatch, task = row_dispatch(row), row_task(row)
        if dispatch and task:
            tasks[dispatch] = task


def show_worker(dispatch):
    result, _err = orca_call("orchestration", "worker-show", "--dispatch", dispatch)
    if not isinstance(result, dict):
        return {}
    nested = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
    task = nested.get("taskId") or result.get("taskId") or ""
    status = nested.get("status") or result.get("status") or ""
    return {"heartbeat": heartbeat_of(result), "task": task, "status": status}


def read_worker(dispatch):
    orca_call("orchestration", "worker-read", "--dispatch", dispatch, "--limit", "8")


def heartbeat_reason(heartbeat_ms):
    if heartbeat_ms % 1000 == 0:
        return f"no heartbeat in {heartbeat_ms // 1000}s"
    return f"no heartbeat in {heartbeat_ms}ms"


def failure_for(run, state, dispatch, task, reason, read):
    if read:
        read_worker(dispatch)
    state["reported"][dispatch] = reason
    save_wait_state(run, state)
    return emit(wait_payload("failure", reason, task=task, dispatch=dispatch, action="left running"))


def cmd_wait(argv):
    """Confirm first heartbeats, then block until the coordinator must act.

    A missing first heartbeat, or a heartbeat that goes stale while the worker
    is not blocked on a question, is a failure. The worker is left running.
    Absence is not a retry and not a stop. One JSON line, then return.
    """
    opts = parse_wait_args(argv)
    run, err = bound_run(opts["run"])
    if not run:
        return emit(wait_payload("failure", err or "this terminal has no bound run"), 1)
    state = load_wait_state(run)
    confirmed = state["confirmed"]
    reported = state["reported"]
    since = state["since"]
    tasks = {}
    heartbeat_s = opts["heartbeat_ms"] / 1000
    stall_s = opts["stall_ms"] / 1000
    timeout_s = opts["timeout_ms"] / 1000
    started = monotonic_now()
    last_daemon = started
    start_mailbox(run)
    while True:
        if monotonic_now() - last_daemon >= 10:
            start_mailbox(run)
            last_daemon = monotonic_now()
        journal = read_journal(run)
        cursor = min(read_cursor(run), len(journal))
        fresh = journal[cursor:]
        consumed = cursor
        actionable = {"question", "escalation", "worker_done", "pre-pr"}
        for offset, message in enumerate(fresh):
            status, fields = message_fields(message)
            dispatch = fields.get("dispatch") or ""
            if dispatch and fields.get("task"):
                tasks[dispatch] = fields["task"]
            if status == "heartbeat":
                note_heartbeat(confirmed, dispatch, "", True)
                consumed = cursor + offset + 1
                continue
            if status in WAIT_QUIET or status not in actionable:
                consumed = cursor + offset + 1
                continue
            write_cursor(run, cursor + offset + 1)
            save_wait_state(run, state)
            return emit(wait_payload(status, **fields))
        if consumed != cursor:
            write_cursor(run, consumed)
        rows, _list_err = paged("worker-list", run)
        rows = rows or []
        remember_tasks(tasks, rows)
        watch = watched_dispatches(opts["dispatches"], rows)
        kinds = latest_types(journal)
        settled = set()
        for dispatch in watch:
            shown = show_worker(dispatch)
            if shown.get("task"):
                tasks[dispatch] = shown["task"]
            if shown.get("status") in SETTLED_STATUS:
                settled.add(dispatch)
                continue
            if shown.get("heartbeat"):
                note_heartbeat(confirmed, dispatch, shown["heartbeat"], False)
        watch = [dispatch for dispatch in watch if dispatch not in settled]
        now = wall_now()
        missing = []
        for dispatch in watch:
            if dispatch in reported or dispatch in confirmed:
                continue
            since.setdefault(dispatch, iso(now))
            age = stamp_age(since[dispatch])
            if age is not None and age >= heartbeat_s:
                missing.append(dispatch)
        if missing:
            dispatch = sorted(missing)[0]
            return failure_for(run, state, dispatch, tasks.get(dispatch, ""), heartbeat_reason(opts["heartbeat_ms"]), True)
        for dispatch in sorted(watch):
            if dispatch in reported or dispatch not in confirmed:
                continue
            if kinds.get(dispatch) in WAIT_BLOCKING or kinds.get(dispatch) == "worker_done":
                continue
            age = stamp_age(confirmed[dispatch])
            if age is None or age < stall_s:
                continue
            return failure_for(run, state, dispatch, tasks.get(dispatch, ""), "stalled after heartbeat", False)
        save_wait_state(run, state)
        if monotonic_now() - started >= timeout_s:
            write_cursor(run, len(journal))
            return emit(wait_payload("timeout", "no message"))
        pause(WAIT_POLL_S)


def dispatch(argv):
    if argv[:1] == ["--version"]:
        print(VERSION)
        return 0
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print(USAGE)
        return 0
    command = argv[0] if argv else ""
    if command == "install":
        return cmd_install(argv[1:])
    if command == "update":
        return cmd_update(argv[1:])
    if command == "uninstall":
        return cmd_uninstall(argv[1:])
    if command == "doctor":
        return cmd_doctor(argv[1:])
    if command == "overlap":
        return cmd_overlap(argv[1:])
    if command == "charter":
        return cmd_charter(argv[1:])
    if command == "ship":
        return cmd_ship(argv[1:])
    if command == "wait":
        return cmd_wait(argv[1:])
    if command == "cleanup":
        return cmd_cleanup(argv[1:])
    if command == "restart":
        return cmd_restart(argv[1:])
    if command == "governed":
        return cmd_governed(argv[1:])
    return cmd_start(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return dispatch(argv)
    except (RegistryError, CharterError) as exc:
        print(f"gantry: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
