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

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

VERSION = "0.2.1"
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


def require_tools():
    if shutil.which("orca") is None:
        die("orca CLI not on PATH")
    if not glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/pstack/*/skills/poteto-mode")):
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


def gather(repo, planned, intent):
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
    data = apply_classification(gather(repo, files, opts.get("intent") or ""))
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
