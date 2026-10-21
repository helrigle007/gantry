#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from functools import cached_property

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gantry_kit as kit

_BAD_CONFIG = object()
COORDINATOR_AGENT = "gantry-coordinator"
SHIP_TASK_TITLE = "gantry-ship"
CLEANUP_TASK_TITLE = "gantry-cleanup"
SETTLED = {"completed", "failed"}

class Deny(Exception):
    pass

def need(ok, reason):
    if not ok:
        raise Deny(reason)

def deny(reason):
    raise Deny(reason)

ANY = lambda action: True
NONE = lambda action: False
only = lambda *xs: lambda action: action in xs
but = lambda *xs: lambda action: action not in xs
base = os.path.basename

NO_ORIGIN = "origin is missing or not a parseable git remote (expected https://host/owner/repo or git@host:owner/repo), so network git and gh are blocked"
COORD_NO = "gantry coordinator never writes code or edits files; dispatch a worker"
SUBAGENT_MSG = "gantry coordinator never spawns subagents (Agent or Task); parallel work belongs to Orca workers"
SUBAGENT_TOOLS = {"Agent", "Task"}
ORCH_MSG = "Orca is the only orchestrator; never route to pstack Orchestrate"
PROTECTED_MSG = "gantry guard, mailbox, run hook, settings, config, coordinator agent and .git/ are off limits; ask the operator to change them"
ORCH_RE = re.compile(r"skills/poteto-mode/playbooks/orchestrate\.md|/skills/poteto-mode/scripts/orch/")
PROTECTED_FILES = r"(?:\.claude/(?:hooks/gantry-guard\.py|bin/gantry-mailbox|bin/gantry_run\.py|bin/gantry_run/.*|settings(?:\.local)?\.json|gantry\.json|agents/gantry-coordinator\.md)|\.gantry(?:/|$)|agents/gantry-coordinator\.md)"
PROTECTED_PATH = re.compile(rf"/{PROTECTED_FILES}$|/\.git(?:/|$)")
PROTECTED_TOKEN = re.compile(rf"{PROTECTED_FILES}|gantry-guard|(?:^|/)\.git(?:/|$)")
PROTECTED_ENV = re.compile(r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GH_HOST|GH_REPO|GIT_DIR|GIT_SSH_COMMAND|GIT_CONFIG\w*|GANTRY_\w*)$")
BAD_CONFIG = ("url.", "credential.", "core.sshcommand", "core.hookspath", "remote.", "alias.")
REMOTE_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/(?P<o1>[^/]+)/(?P<r1>[^/]+)"
    r"|(?:[^@]+)@(?P<h2>[^:]+):(?P<o2>[^/]+)/(?P<r2>[^/]+))/?$", re.I)

def parse_remote(url):
    m = REMOTE_RE.match((url or "").strip())
    if not m:
        return None
    host = (m["h1"] or m["h2"]).lower()
    owner, repo = m["o1"] or m["o2"], m["r1"] or m["r2"]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if host == "www.github.com":
        host = "github.com"
    if not owner or not repo or owner in (".", "..") or repo in (".", ".."):
        return None
    slug = f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"
    return {"host": host, "owner": owner, "repo": repo, "slug": slug}

def same_repo(a, b):
    return bool(a and b and a["host"] == b["host"] and a["owner"].lower() == b["owner"].lower() and a["repo"].lower() == b["repo"].lower())

def is_url(arg):
    return "://" in arg or re.match(r"^[\w.-]+@[\w.-]+:", arg) or arg.startswith(("/", ".", "~"))

def merged_on_github(slug, branch):
    r = subprocess.run(["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "merged", "--json", "number", "--jq", "length"],
                       capture_output=True, text=True, timeout=20)
    return r.returncode == 0 and r.stdout.strip().isdigit() and int(r.stdout) > 0

def orca(*args):
    r = subprocess.run(["orca", "orchestration", *args, "--json"], capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(f"orca orchestration {args[0]} exited {r.returncode}")
    return json.loads(r.stdout)["result"]

def orca_workers(*args):
    rows, cursor = [], []
    for _ in range(20):
        page = orca("worker-list", *args, *cursor)
        rows += page.get("workers", [])
        nxt = (page.get("page") or {}).get("nextCursor")
        if not nxt:
            break
        cursor = ["--cursor", nxt]
    return rows

def orca_worker(handle):
    # Any terminal state counts: a new-child worker reads "retained" (user_takeover) while it runs.
    return next((w for w in orca_workers() if w.get("agentTerminalHandle") == handle), None)

def orca_task_title(worker):
    return next((t.get("task_title") for t in orca("task-list", "--run", worker["runId"]).get("tasks", []) if t.get("id") == worker["taskId"]), None)

AGENT_FLAG = re.compile(rf"(?:^|\s)--agent(?:=|\s+){COORDINATOR_AGENT}(?:\s|$)")
WALK_CAP = 32

def argv_is_coordinator(argv):
    for i, arg in enumerate(argv):
        if arg == f"--agent={COORDINATOR_AGENT}" or (arg == "--agent" and i + 1 < len(argv) and argv[i + 1] == COORDINATOR_AGENT):
            return True
    return False

def command_marks_coordinator(command):
    return bool(AGENT_FLAG.search(command))

def env_marks_coordinator():
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    return bool(handle) and os.environ.get("GANTRY_COORDINATOR_HANDLE") == handle

def proc_argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [p.decode("utf-8", "surrogateescape") for p in f.read().split(b"\0") if p]
    except OSError:
        return None

def proc_ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        return int(stat[stat.rfind(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None

def proc_row(pid):
    argv, ppid = proc_argv(pid), proc_ppid(pid)
    if argv is None or ppid is None:
        return None
    return ppid, argv_is_coordinator(argv)

def parse_ps_table(text):
    rows = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s*(.*)$", line)
        if m:
            rows[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return rows

def process_table():
    try:
        r = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return parse_ps_table(r.stdout.decode("utf-8", "surrogateescape"))

def ps_row(pid, table):
    item = table.get(pid)
    if item is None:
        return None
    ppid, command = item
    return ppid, command_marks_coordinator(command)

def walk_rows(start_pid, row_of):
    pid, seen = start_pid, set()
    for _ in range(WALK_CAP):
        if not isinstance(pid, int) or pid in seen:
            return None
        if pid <= 1:
            return False
        seen.add(pid)
        row = row_of(pid)
        if row is None:
            return None
        ppid, marks = row
        if marks:
            return True
        pid = ppid
    return None

def ancestor_launched_coordinator():
    start = os.getppid()
    if proc_argv(start) is not None and proc_ppid(start) is not None:
        return walk_rows(start, proc_row)
    table = process_table()
    if table is None:
        return None
    return walk_rows(start, lambda pid: ps_row(pid, table))

def role_of(payload):
    if payload.get("agent_type") == COORDINATOR_AGENT or env_marks_coordinator():
        return "coordinator", None
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    try:
        worker = orca_worker(handle) if handle else None
    except Exception:
        found = ancestor_launched_coordinator()
        if found is True:
            return "coordinator", None
        if found is False:
            return "worker", None
        return "unconfirmed", None
    if worker:
        return "worker", worker
    found = ancestor_launched_coordinator()
    if found is True:
        return "coordinator", None
    if found is False:
        return "operator", None
    return "unidentified", None

def git_common_dir(start):
    r = subprocess.run(
        ["git", "-C", start, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None

def resolve_config(cwd):
    try:
        governed = kit.governing(os.environ.get("CLAUDE_PROJECT_DIR"), cwd)
    except kit.RegistryError:
        return _BAD_CONFIG
    if governed is None:
        return {}
    if governed.config is kit.BAD:
        return _BAD_CONFIG
    return governed.config

class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None

    @cached_property
    def config(self):
        return resolve_config(self.path)

    @cached_property
    def auto_merge(self):
        value = self.config.get("autoMerge", True)
        return value if isinstance(value, bool) else True

    @cached_property
    def forbidden(self):
        if "forbidden" not in self.config:
            return []
        value = self.config["forbidden"]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            deny("forbidden in .git/gantry.json must be a list of strings")
        return [item.strip() for item in value if item.strip()]

    def remote_url(self, name):
        # Stored URL. `remote get-url` applies url.insteadOf and can hide the owner.
        return self.git("config", "--get", f"remote.{name}.url")

    @cached_property
    def remote(self):
        return parse_remote(self.remote_url("origin"))

    @cached_property
    def slug(self):
        return self.remote["slug"] if self.remote else None

    @cached_property
    def default(self):
        ref = self.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return ref.removeprefix("origin/") if ref else "main"

    @cached_property
    def current(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    def is_own(self, target):
        parsed = parse_remote(target) if is_url(target) else parse_remote(self.remote_url(target))
        return same_repo(self.remote, parsed)

    def owns_slug(self, text):
        if not self.remote:
            return False
        parsed = parse_remote(text)
        if parsed:
            return same_repo(self.remote, parsed)
        parts = text.strip().removesuffix(".git").strip("/").split("/")
        owner, repo = self.remote["owner"].lower(), self.remote["repo"].lower()
        if len(parts) == 2:
            return parts[0].lower() == owner and parts[1].lower() == repo
        if len(parts) == 3:
            return parts[0].lower() == self.remote["host"] and parts[1].lower() == owner and parts[2].lower() == repo
        return False

    def merged(self, branch):
        return branch != self.default and bool(self.slug) and merged_on_github(self.slug, branch)

    def on_origin(self, branch):
        r = subprocess.run(["git", "-C", self.path, "ls-remote", "--exit-code", "--heads", "origin", branch], capture_output=True, text=True, timeout=20)
        need(r.returncode in (0, 2), f"could not ask origin about {branch}; retry")
        return r.returncode == 0

@dataclass
class Ctx:
    role: str
    cwd: str
    worker: dict = None
    repos: dict = field(default_factory=dict)

    def repo(self, path=""):
        path = os.path.realpath(os.path.join(self.cwd, path))
        return self.repos.setdefault(path, Repo(path))

    @cached_property
    def task_title(self):
        try:
            return orca_task_title(self.worker) if self.worker else None
        except Exception:
            return None

def parse_opts(args, valued):
    opts, pos, it = [], [], iter(args)
    for a in it:
        if a == "--":
            pos += list(it)
        elif a.startswith("--") and "=" in a:
            opts.append(tuple(a.split("=", 1)))
        elif a in valued:
            opts.append((a, next(it, "")))
        elif re.match(r"^-[A-Za-z]", a) and a[:2] in valued:
            opts.append((a[:2], a[2:]))
        elif a.startswith("-") and a != "-":
            opts.append((a, None))
        else:
            pos.append(a)
    return opts, pos

def short_letters(opts):
    return "".join(k[1:] for k, _ in opts if re.match(r"^-[^-]", k))


@dataclass
class Seg:
    argv: list
    redirects: list

PUNCT = set("();<>|&\n")
WRAPPERS = {"env": {"-u", "--unset", "-C", "--chdir"}, "command": set(), "nohup": set(), "exec": {"-a"}, "builtin": set(),
            "time": {"-f", "--format", "-o", "--output"}, "sudo": {"-u", "-g"}, "nice": {"-n"}, "timeout": {"-s", "--signal", "-k", "--kill-after"}}
SHELLS = {"bash", "sh", "zsh", "dash"}

def is_redirect(tok):
    return set(tok) <= PUNCT and bool(set(tok) & set("<>")) and not set(tok) & set("();\n")

def split_segments(cmd, depth=0):
    need(depth < 6, "command nests too deeply; run the inner commands directly")
    cmd = cmd.replace("\\\n", " ")
    lx = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
    lx.whitespace, lx.whitespace_split, lx.commenters = " \t\r", True, ""
    try:
        toks = list(lx)
    except ValueError:
        deny("could not parse the command; simplify quoting and retry")
    segs, cur = [], []
    for t in toks:
        if t and set(t) <= PUNCT and not is_redirect(t):
            segs.append(cur)
            cur = []
            continue
        cur.append(t)
    segs.append(cur)
    for body in substitutions(cmd):
        segs += split_segments(body, depth + 1)
    return [s for s in segs if s]

def substitutions(cmd):
    # Single quotes keep $( and ` literal, as in `ssh host '... $(...)'`, which runs on the remote host.
    out, i, quote = [], 0, None
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c == '"':
            quote = None if quote else '"'
        elif c == "'" and not quote:
            quote = "'"
        elif cmd.startswith("$(", i):
            level, j = 1, i + 2
            while j < len(cmd) and level:
                level += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            out.append(cmd[i + 2:j - 1] if level == 0 else cmd[i + 2:])
            i = j
            continue
        elif c == "`":
            j = cmd.find("`", i + 1)
            j = len(cmd) if j < 0 else j
            out.append(cmd[i + 1:j])
            i = j + 1
            continue
        i += 1
    return out

def check_assignment(tok):
    m = re.match(r"^([A-Za-z_]\w*)=", tok)
    need(not (m and PROTECTED_ENV.match(m[1])), f"do not set {m and m[1]}; gantry uses the ambient gh/git auth and repo")
    return bool(m)

def strip_prefix(argv):
    i = 0
    while i < len(argv):
        if check_assignment(argv[i]):
            i += 1
            continue
        name = base(argv[i])
        if name not in WRAPPERS:
            break
        i += 1
        while i < len(argv) and argv[i].startswith("-"):
            need(not (name == "env" and argv[i].startswith(("-S", "--split"))), "env -S hides the command; run it directly")
            i += 2 if argv[i] in WRAPPERS[name] else 1
        i += name == "timeout"
    return argv[i:]

COMPOUND_LEAD = {
    "if", "then", "elif", "else", "fi", "while", "until", "do", "done",
    "for", "case", "esac", "select", "function", "{", "}", "!", "[[", "]]",
}
GIT_MOVING = {
    "push", "merge", "pull", "rebase", "reset", "branch", "checkout", "switch",
    "worktree", "remote", "update-ref", "send-pack", "symbolic-ref", "fetch", "clone",
}
GH_MOVING = {"create", "new", "merge"}


def unresolved(token):
    return "$" in token or "`" in token


def expand_compound(argv):
    """Peel if/for/while/case/select/function and braces. Each simple command is returned."""
    found = []

    def walk(argv):
        while argv and argv[0] in COMPOUND_LEAD:
            argv = argv[1:]
        if not argv:
            return
        if "{" in argv:
            i = argv.index("{")
            head, rest = argv[:i], argv[i + 1:]
            if head and os.path.basename(head[0]) in {"git", "gh", "orca", "gantry"}:
                deny("could not parse the brace group; simplify the command and retry")
            walk(head)
            if "}" in rest:
                j = rest.index("}")
                walk(rest[:j])
                walk(rest[j + 1:])
            else:
                walk(rest)
            return
        found.append(argv)

    walk(list(argv))
    return found


def reject_dynamic_exec(argv):
    i = 0
    while i < len(argv) and check_assignment(argv[i]):
        i += 1
    if i < len(argv) and base(argv[i]) == "exec" and any(unresolved(t) for t in argv[i + 1:]):
        deny("exec with a shell expansion can't be checked; run the command directly")


def shell_script(argv):
    i = 1
    while i < len(argv) and argv[i][:1] in "-+":
        if argv[i] in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if not argv[i].startswith("--") and "c" in argv[i][1:]:
            need(i + 1 < len(argv), "shell -c needs a command string")
            return argv[i + 1]
        i += 1
    need(i < len(argv), "piping or heredoc-feeding a shell hides the commands; run them directly")
    return None

def commands(cmd, depth=0):
    out = []
    for toks in split_segments(cmd, depth):
        argv, redirects, it = [], [], iter(toks)
        for t in it:
            if is_redirect(t):
                fd = argv.pop() if argv and argv[-1].isdigit() else ""
                redirects.append((fd + t, next(it, "")))
            else:
                argv.append(t)
        pieces = expand_compound(argv)
        if not pieces:
            if redirects and not argv:
                out.append(Seg([], redirects))
            continue
        for piece in pieces:
            reject_dynamic_exec(piece)
            piece = strip_prefix(piece)
            script = shell_script(piece) if piece and base(piece[0]) in SHELLS else None
            if script is not None:
                out += commands(script, depth + 1) + [Seg([], redirects)]
            else:
                out.append(Seg(piece, redirects))
    return out


def git_split(args):
    i, dirs, configs = 0, [], []
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        need(not a.startswith(("--git-dir", "--work-tree")), "do not use --git-dir/--work-tree; cd into the worktree instead")
        if a in ("-C", "-c", "--config-env"):
            (dirs if a == "-C" else configs).append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if a.startswith("--config-env="):
            configs.append(a.split("=", 1)[1])
        i += 1
    for kv in configs:
        need(not kv.lower().startswith(BAD_CONFIG), f"git -c {kv.split('=')[0]} is blocked; use the repo's normal remote and hooks")
    return (args[i] if i < len(args) else ""), args[i + 1:], dirs, configs

def branch_name(repo, ref):
    return (repo.current if ref == "HEAD" else ref or "").removeprefix("refs/heads/")

def git_push(repo, args):
    need(repo.slug, NO_ORIGIN)
    opts, pos = parse_opts(args, {"-o", "--push-option"})
    longs, shorts = {k for k, _ in opts}, short_letters(opts)
    need(not longs & {"--force", "--mirror", "--all", "--prune", "--no-verify", "--repo", "--receive-pack", "--exec"} and "f" not in shorts,
         "force/mirror/all/prune/no-verify pushes are blocked; push one task branch, with --force-with-lease if you rewrote it")
    need("--force-if-includes" not in longs or "--force-with-lease" in longs, "--force-if-includes needs --force-with-lease")
    remote, specs = (pos[0], pos[1:]) if pos else ("origin", [])
    need(repo.is_own(remote), f"push only to origin ({repo.slug})")
    deleting = "--delete" in longs or "d" in shorts
    for spec in specs or ([] if deleting else ["HEAD"]):
        need(not spec.startswith("+"), "+refspec force-pushes; use --force-with-lease instead")
        if deleting or spec.startswith(":"):
            b = branch_name(repo, spec.lstrip(":"))
            need(repo.merged(b), f"delete remote branch {b} only after its PR merged")
            continue
        src, _, dst = spec.partition(":")
        need(branch_name(repo, dst or src) != repo.default, f"never push to {repo.default}; push a task branch and open a PR")

def git_branch(repo, args):
    opts, pos = parse_opts(args, set())
    longs, shorts = {k for k, _ in opts}, short_letters(opts)
    delete = "d" in shorts or "D" in shorts or "--delete" in longs
    force = "D" in shorts or delete and ("f" in shorts or "--force" in longs)
    for b in pos if delete else []:
        need(b != repo.default, f"never delete {repo.default}")
        need(not force or repo.merged(b), f"force-delete {b} only after its PR merged; otherwise keep it")
    if "m" in shorts or "M" in shorts or "--move" in longs:
        need((pos[0] if len(pos) > 1 else repo.current) != repo.default, f"never rename {repo.default}")

def git_remote(repo, args):
    need(args[:1] not in (["add"], ["set-url"], ["rename"], ["remove"], ["rm"], ["set-head"]), "remotes are fixed; use origin as is")

def git_config(repo, args):
    opts, pos = parse_opts(args, {"--file", "-f", "--blob", "--type", "--default"})
    names = {k for k, _ in opts}
    need(not names & {"--edit", "-e"} and pos[:1] != ["edit"], "git config --edit is blocked; set single keys")
    reading = names & {"--get", "--get-all", "--get-regexp", "--list", "-l"} or pos[:1] in (["get"], ["list"])
    writing = not reading and (len(pos) >= 2 or pos[:1] in (["set"], ["unset"]) or any(n.startswith(("--add", "--unset", "--replace-all", "--rename-section", "--remove-section")) for n in names))
    need(not writing or not any(p.lower().startswith(BAD_CONFIG) for p in pos), "remote/url/credential/hooks/alias git config is fixed; do not change it")

NET_VALUED = {"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "-j", "--jobs", "-o", "--server-option", "--sort",
              "-b", "--branch", "--origin", "--reference", "--filter", "--separate-git-dir", "--template", "-c", "--config", "-s", "--strategy", "-X"}

def git_net(repo, args, clone=False):
    need(repo.slug, NO_ORIGIN)
    opts, pos = parse_opts(args, NET_VALUED)
    names = {k for k, _ in opts}
    need(not names & {"--upload-pack", "--receive-pack", "--exec", "--all", "--multiple"} and not (clone and "-u" in names),
         f"fetch only from origin ({repo.slug})")
    for k, v in opts:
        need(k not in ("-c", "--config") or not v.lower().startswith(BAD_CONFIG), "that clone config is blocked")
    need(not clone or pos, "clone needs a URL")
    need(not pos or repo.is_own(pos[0]), f"only talk to origin ({repo.slug})")

def git_update_ref(repo, args):
    need("-d" not in args and "--stdin" not in args, "update-ref -d/--stdin is blocked; use git branch -d or push --delete")
    need(f"refs/heads/{repo.default}" not in args, f"never move {repo.default}")

def main_checkout(path):
    common = Repo(path).git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    common = os.path.realpath(common)
    return os.path.dirname(common) if os.path.basename(common) == ".git" else None

WORKER_PATH_KEYS = ("worktreePath", "worktree", "cwd", "path")

def worker_path(row):
    for key in WORKER_PATH_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value:
            return os.path.realpath(os.path.expanduser(value))
    return None

def live_worker_at(path):
    target = os.path.realpath(path)
    return next((w for w in orca_workers() if w.get("dispatchStatus") not in SETTLED and worker_path(w) == target), None)

def removable_worktree(path):
    path = os.path.realpath(path)
    need(not live_worker_at(path), f"{path} is the worktree of a worker that is still running; keep it")
    need(path != main_checkout(path), f"{path} is the main checkout; keep it")
    wt = Repo(path)
    need(wt.git("status", "--porcelain") == "", f"{path} has uncommitted changes or is not a worktree; keep it")
    b = wt.git("symbolic-ref", "--short", "-q", "HEAD")
    need(b and b != wt.default, f"{path} is not on a task branch; keep it")
    if wt.merged(b):
        return
    need(not wt.on_origin(b), f"remove {path} only after {b} merged or was deleted on origin")
    ahead = wt.git("rev-list", "--count", f"origin/{wt.default}..HEAD")
    need(ahead == "0", f"{path} has commits that are not on origin; keep it")

def git_worktree(repo, args):
    if args[:1] != ["remove"]:
        return
    opts, pos = parse_opts(args[1:], set())
    need(not {k for k, _ in opts} & {"--force", "-f"} and len(pos) == 1, "remove one worktree by path, never --force")
    path = os.path.realpath(os.path.join(repo.path, pos[0]))
    need(path != os.path.realpath(repo.path), "never remove the worktree you are running in")
    removable_worktree(path)

def git_denied(_repo, _args):
    deny("that git command can move refs outside the push rules; use git push")


GIT_RULES = {"push": git_push, "worktree": git_worktree, "branch": git_branch, "remote": git_remote, "config": git_config, "update-ref": git_update_ref,
             "fetch": git_net, "pull": git_net, "ls-remote": git_net, "clone": lambda repo, args: git_net(repo, args, clone=True),
             "send-pack": git_denied, "symbolic-ref": git_denied}

def check_resolved(name, args):
    if unresolved(name):
        deny("command name is not a literal; run it directly")
    if name == "git":
        sub, rest, dirs, _configs = git_split(args)
        if unresolved(sub) or any(unresolved(d) for d in dirs):
            deny("git subcommand is not a literal; run it directly")
        if sub in GIT_MOVING and any(unresolved(t) for t in rest):
            deny("git arguments are not literal; the guard cannot check this ref or flag")
    elif name == "gh":
        _opts, pos = parse_opts(args, GH_VALUED)
        sub, action = (pos + ["", ""])[:2]
        if unresolved(sub) or unresolved(action):
            deny("gh command is not a literal; run it directly")
        if (action in GH_MOVING or sub == "api") and any(unresolved(t) for t in args):
            deny("gh arguments are not literal; the guard cannot check this command")
    elif name in {"orca", "gantry"} or name == "gantry_kit.py":
        words = [t for t in args if not t.startswith("-")]
        if any(unresolved(t) for t in words[:2]):
            deny("command is not a literal; run it directly")


def check_git(ctx, args):
    sub, rest, dirs, _ = git_split(args)
    rule = GIT_RULES.get(sub)
    if rule:
        need(not any("$" in d or "`" in d for d in dirs), "git -C with a shell variable can't be checked; cd into the directory or pass a literal path")
        rule(ctx.repo(os.path.join(*dirs) if dirs else ""), rest)


GH_VALUED = {"-R", "--repo", "--hostname", "-X", "--method", "-f", "-F", "--field", "--raw-field", "--input", "-H", "--header",
             "-q", "--jq", "-t", "--template", "-p", "--preview", "--cache", "--json", "--match-head-commit", "-L", "--limit",
             "--head", "-B", "--base", "-b", "--body", "--body-file", "--title"}
GH_POLICY = {
    "auth": (only("status"), "only `gh auth status`; never change gh auth"),
    "repo": (only("view", "list", "clone"), "gh repo is limited to view/list/clone; ask the operator for repo changes"),
    "secret": (NONE, "secrets are operator-managed"),
    "variable": (NONE, "variables are operator-managed"),
    "ssh-key": (NONE, "keys are operator-managed"),
    "gpg-key": (NONE, "keys are operator-managed"),
    "ruleset": (only("list", "view", "check"), "rulesets are read-only for agents"),
    "workflow": (but("enable", "disable"), "workflow enable/disable is operator-only"),
    "alias": (only("list"), "gh aliases are fixed; run the full gh command"),
}
SENSITIVE_API = ("/branches/", "protection", "/rulesets", "/hooks", "/collaborators", "/keys", "/secrets", "/variables", "/environments",
                 "/actions/permissions", "/pages", "/transfer", "/topics", "/vulnerability-alerts", "/merge", "/contents/")
GRAPHQL_DENY = re.compile(r"delete|branchprotection|ruleset|updaterepository|archiverepository|transferrepository|updateref|collaborator|mergepullrequest|automerge|createpullrequest", re.I)
FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}

def graphql_text(ctx, opts):
    def read(p):
        with open(os.path.join(ctx.cwd, p)) as f:
            return f.read()
    parts = []
    for k, v in opts:
        if k in FIELD_FLAGS - {"--input"} and v.startswith("query="):
            q = v[len("query="):]
            parts.append(read(q[1:]) if q.startswith("@") else q)
        if k == "--input":
            need(v != "-", "pass the graphql query with -f query=...")
            parts.append(read(v))
    return "\n".join(parts)

def gh_api(ctx, repo, opts, pos):
    method = next((v for k, v in opts if k in ("-X", "--method")), None) or ("POST" if any(k in FIELD_FLAGS for k, _ in opts) else "GET")
    method = method.upper()
    path = re.sub(r"^(https?://[^/]+)?/+", "", pos[1] if len(pos) > 1 else "")
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if method == "POST" and re.fullmatch(r"repos/[^/]+/[^/]+/pulls", path):
        need(ctx.role == "worker", "coordinator never opens PRs; a worker opens the PR after the pre-pr overlap check")
        need(pr_create_allowed(ctx), "run gantry overlap and clear the pre-pr check before opening a PR")
        return
    if path == "graphql":
        return need(not GRAPHQL_DENY.search(graphql_text(ctx, opts)), "that graphql mutation is blocked; use gh pr/issue commands")
    m = re.match(r"repos/([^/]+)/([^/?#]+)", path)
    need(not m or m[0] == "repos/{owner}/{repo}" or repo.owns_slug(f"{m[1]}/{m[2]}"), f"gh api may only touch {repo.slug}")
    if method == "GET":
        return
    need(not re.match(r"(users|orgs|user)(/|$)", path), "account and org settings are operator-only")
    if method == "DELETE":
        ref = re.fullmatch(r"repos/[^/]+/[^/]+/git/refs/heads/(.+)", path)
        return need(ref and repo.merged(ref[1]), "API deletes are limited to merged task branch refs")
    need(not re.fullmatch(r"repos/[^/]+/[^/?#]+/?", path) and not any(s in path for s in SENSITIVE_API)
         and not path.endswith(f"/git/refs/heads/{repo.default}"), "repo settings, protection and merges are not writable via gh api; use gh pr commands")

def pr_create_allowed(ctx):
    if ctx.role != "worker" or not isinstance(ctx.worker, dict):
        return False
    task = ctx.worker.get("taskId")
    run = ctx.worker.get("runId")
    if not task or not run:
        return False
    return kit.cleared(run, task, "pre-pr")

def gh_pr(ctx, repo, opts, pos):
    if len(pos) > 1 and pos[1] in ("create", "new"):
        need(ctx.role == "worker", "coordinator never opens PRs; a worker opens the PR after the pre-pr overlap check")
        need(pr_create_allowed(ctx), "run gantry overlap and clear the pre-pr check before opening a PR")
        return
    if pos[1:2] != ["merge"]:
        return
    need(ctx.role == "worker", f"coordinator never merges; the {SHIP_TASK_TITLE} worker merges per Shipping")
    need(ctx.task_title == SHIP_TASK_TITLE,
         f"only the {SHIP_TASK_TITLE} worker merges; do not merge, report the PR as ready in worker_done")
    need(repo.auto_merge, "autoMerge is false in .git/gantry.json: bring PRs to merge-ready and stop")
    names = {k for k, _ in opts}
    need(not names & {"--admin", "--auto"}, "no --admin/--auto; merge only when checks pass")
    need(any(k == "--match-head-commit" and v for k, v in opts), "merge with --match-head-commit <sha> (Shipping's guarded merge)")

def gh_repo(ctx, repo, opts, pos):
    need(pos[1:2] != ["clone"] or repo.owns_slug((pos + [""])[2]), f"clone only {repo.slug}")

GH_HANDLERS = {"api": gh_api, "pr": gh_pr, "repo": gh_repo}

def check_gh(ctx, args):
    opts, pos = parse_opts(args, GH_VALUED)
    sub, action = (pos + ["", ""])[:2]
    if (sub, action) == ("auth", "status"):
        return
    repo = ctx.repo()
    need(repo.slug, NO_ORIGIN)
    for k, v in opts:
        need(k not in ("-R", "--repo") or repo.owns_slug(v), f"gh -R must be {repo.slug}")
        need(k != "--hostname" or v.lower() == repo.remote["host"], f"gh --hostname must be {repo.remote['host']}")
    need(action != "delete" or sub == "api", f"gh {sub} delete is blocked; ask the operator")
    allowed, why = GH_POLICY.get(sub, (ANY, ""))
    need(allowed(action), why)
    if sub in GH_HANDLERS:
        GH_HANDLERS[sub](ctx, repo, opts, pos)


ORCA_ALLOW = {
    "worker": {"status": ANY, "skills": ANY, "orchestration": only("send", "ask", "check")},
    "coordinator": {"status": ANY, "skills": ANY, "orchestration": but("reset", "check"), "worktree": only("list", "show", "ps"),
                    "terminal": only("list", "show", "read", "wait", "send")},
}
ORCA_DENY = {
    "worker": "workers talk only to the coordinator (orca orchestration send|ask|check); fan out with Claude Code subagents, never Orca Runs/workers",
    "coordinator": "coordinator may use orca status, skills, orchestration (not reset or check: wait with gantry-mailbox), worktree list|show|ps, terminal list|show|read|wait, and terminal send --terminal <handle> only to a worker of this run",
}

def run_terminal_worker(run_id, handle):
    if not run_id or not handle:
        return None
    return next((w for w in orca_workers("--run", run_id) if w.get("agentTerminalHandle") == handle), None)

def coordinator_run_id():
    result = orca("run-current")
    if not isinstance(result, dict):
        return None
    run = result.get("run")
    if isinstance(run, dict):
        return run.get("id")
    if isinstance(run, str):
        return run
    return result.get("id")

def orca_terminal_send(ctx, opts):
    handle = opts.get("--terminal")
    need(handle, "terminal send needs --terminal <handle>; it never targets the active terminal")
    need(handle != os.environ.get("ORCA_TERMINAL_HANDLE"), "terminal send never targets the coordinator's own terminal")
    run_id = coordinator_run_id()
    need(run_id, "terminal send needs the coordinator's bound run")
    need(run_terminal_worker(run_id, handle), "terminal send only reaches a worker of this run")

def orca_terminal_close(ctx, opts):
    handle = opts.get("--terminal")
    need(handle and not {"--all", "--worktree"} & opts.keys(), "cleanup closes one terminal: orca terminal close --terminal <handle>")
    # This worker's terminal has no bound run, so worker-list without --run is every run.
    w = next((w for w in orca_workers() if w.get("agentTerminalHandle") == handle), None)
    need(w and handle != ctx.worker.get("agentTerminalHandle") and w.get("dispatchStatus") in SETTLED,
         "cleanup closes only a settled gantry worker terminal from any run, never its own and never one that is still running")

def orca_worktree_rm(ctx, opts):
    sel = opts.get("--worktree") or ""
    need(sel.startswith("path:") and not {"--force", "--allow-failed-archive-hook"} & opts.keys(),
         "cleanup removes worktrees with orca worktree rm --worktree path:<path>, never --force")
    path = os.path.realpath(os.path.join(ctx.cwd, os.path.expanduser(sel[len("path:"):])))
    need(path != os.path.realpath(ctx.cwd), "cleanup never removes the worktree it is running in")
    removable_worktree(path)

CLEANUP_ORCA = {("terminal", "close"): orca_terminal_close, ("worktree", "rm"): orca_worktree_rm}

def check_orca(ctx, args):
    sub, action = ([a for a in args if not a.startswith("-")] + ["", ""])[:2]
    cleanup = CLEANUP_ORCA.get((sub, action))
    if cleanup and ctx.role == "worker" and ctx.task_title == CLEANUP_TASK_TITLE:
        return cleanup(ctx, dict(parse_opts(args, {"--terminal", "--worktree"})[0]))
    need(ORCA_ALLOW[ctx.role].get(sub, NONE)(action), ORCA_DENY[ctx.role])
    if ctx.role == "coordinator" and (sub, action) == ("terminal", "send"):
        orca_terminal_send(ctx, dict(parse_opts(args, {"--terminal", "--text"})[0]))


INDIRECT_RE = re.compile(r"(^|[\s/;&|`(])(git|gh|orca|gt)(\s|$)")

def check_indirect(ctx, args):
    need(not any(INDIRECT_RE.search(a) for a in args), "run git/gh/orca directly, not through xargs/parallel/find -exec")

def deny_source(_ctx, _args):
    deny("source and . run a file the guard does not read; run the commands directly")


def check_gantry(ctx, args):
    need(ctx.role == "coordinator" and args[:1] == ["overlap"], "only the coordinator runs gantry, and only gantry overlap")

def launcher_args(argv):
    for index, token in enumerate(argv):
        if os.path.basename(token) in ("gantry", "gantry_kit.py"):
            return argv[index + 1:]
    return None

SEGMENT_RULES = {
    "git": check_git, "gh": check_gh, "orca": check_orca, "gantry": check_gantry,
    "source": deny_source, ".": deny_source,
    "gt": lambda ctx, args: deny("Graphite (gt) is not installed; use gh"),
    "eval": lambda ctx, args: deny("eval hides the command; run it directly"),
    "xargs": check_indirect, "parallel": check_indirect,
    "find": lambda ctx, args: check_indirect(ctx, args) if {"-exec", "-execdir", "-ok", "-okdir"} & set(args) else None,
    **{k: lambda ctx, args: [check_assignment(a) for a in args] for k in ("export", "declare", "typeset", "local", "readonly")},
}
READONLY = {"cat", "head", "tail", "less", "grep", "rg", "wc", "ls"}
GIT_READONLY = {"diff", "log", "show", "status", "blame"}
FILTERS = {"cd", "jq", "head", "tail", "grep", "wc", "cat", "ls", "pwd", "echo", "date", "true", "test", "["}
BRANCH_FILTERS = {"--list", "-l", "--contains", "--merged", "--no-merged"}
BRANCH_LIST = BRANCH_FILTERS | {"-a", "-r", "-v", "-vv", "--show-current", "--all", "--remotes"}

def branch_listing(args):
    flags = {x for x in args if x.startswith("-")}
    # a bare positional without a filter flag creates a branch
    return flags <= BRANCH_LIST and (len(flags) == len(args) or bool(flags & BRANCH_FILTERS))

COORD_GIT = {
    **{s: ANY for s in ("status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "merge-base", "cat-file", "describe", "shortlog", "blame", "grep")},
    "reflog": lambda a: a[:1] in ([], ["show"]),
    "branch": branch_listing,
    "remote": lambda a: a in ([], ["-v"]) or a[:1] == ["get-url"],
    "worktree": lambda a: a[:1] == ["list"],
    "config": lambda a: bool(set(a) & {"--get", "--get-all", "--list", "-l"}) and not any(x.startswith(("--add", "--unset", "--replace", "--rename", "--remove", "--edit", "-e")) for x in a),
}
COORD_GH = {("pr", "view"), ("pr", "checks"), ("pr", "list"), ("auth", "status")}
FETCH_FLAGS = {"-q", "--quiet", "-v", "--verbose", "--no-tags", "--dry-run", "--ff-only"}
FF_FLAGS = {"--ff-only", "-q", "--quiet", "-v", "--verbose"}

def is_main_checkout(repo):
    root = main_checkout(repo.path)
    return bool(root) and os.path.realpath(repo.path) == os.path.realpath(root)

def ref_is_default(ref, default):
    return ref in (default, f"refs/heads/{default}")

def origin_default(repo):
    upstream = repo.git("rev-parse", "--abbrev-ref", "@{upstream}")
    return upstream == f"origin/{repo.default}"

def require_ff_checkout(repo):
    need(is_main_checkout(repo), "fast-forward the default branch only in the main checkout")
    need(repo.current == repo.default, f"main checkout is on {repo.current or 'no branch'}, not {repo.default}; skip the pull and base workers on origin/{repo.default}")
    need(repo.git("status", "--porcelain") == "", f"main checkout is dirty; skip the pull and base workers on origin/{repo.default}")

def coord_fetch(repo, rest):
    opts, pos = parse_opts(rest, NET_VALUED)
    names = {k for k, _ in opts}
    need(names <= FETCH_FLAGS, "coordinator fetch is from origin only, with no prune or force")
    need(pos and repo.is_own(pos[0]), f"fetch only from origin ({repo.slug})")
    default = repo.default
    for spec in pos[1:]:
        need(not spec.startswith("+"), "fetch will not force-update a ref; omit +")
        src, sep, dst = spec.partition(":")
        need(ref_is_default(src, default), f"fetch only {default} from origin")
        if not sep:
            continue
        need(ref_is_default(dst, default), f"fetch may only fast-forward local {default}")
        need(is_main_checkout(repo), "fast-forward the default branch only in the main checkout")
        if repo.current == default:
            need(repo.git("status", "--porcelain") == "", f"main checkout is dirty; skip the local update and base workers on origin/{default}")

def coord_ff(repo, sub, rest):
    opts, pos = parse_opts(rest, NET_VALUED | {"-m", "--message", "-s", "--strategy", "-X", "--strategy-option"})
    names = {k for k, _ in opts}
    need(names <= FF_FLAGS and "--ff-only" in names, "update the default branch only with --ff-only, never rebase, reset, or a merge commit")
    require_ff_checkout(repo)
    default = repo.default
    if sub == "pull":
        if not pos:
            need(origin_default(repo), f"pull --ff-only only when upstream is origin/{default}")
        else:
            need(repo.is_own(pos[0]) and len(pos) <= 2 and (len(pos) == 1 or pos[1] == default), f"pull only origin {default}")
            if len(pos) == 1:
                need(origin_default(repo), f"pull --ff-only origin only when upstream is origin/{default}")
    else:
        need(pos == [f"origin/{default}"] or pos == [f"refs/remotes/origin/{default}"], f"merge only --ff-only origin/{default}")
    require_ff_checkout(repo)

def coord_git_cmd(ctx, args):
    sub, rest, dirs, configs = git_split(args)
    need(not configs and not any(a.startswith(("--output", "--open-files-in-pager", "-O")) for a in rest),
         "coordinator runs only read-only git/gh, orca and filters, not `git`; dispatch a worker")
    repo = ctx.repo(os.path.join(*dirs) if dirs else "")
    if sub == "fetch":
        return coord_fetch(repo, rest)
    if sub in ("pull", "merge"):
        return coord_ff(repo, sub, rest)
    need(COORD_GIT.get(sub, NONE)(rest), "coordinator runs only read-only git/gh, orca and filters, not `git`; dispatch a worker")

COORD_BASH = {
    **{f: ANY for f in FILTERS},
    "sort": lambda a: not any(x.startswith(("-o", "--output")) for x in a),
    "uniq": lambda a: len([x for x in a if not x.startswith("-")]) <= 1,
    "orca": ANY, "gantry-mailbox": ANY,
    "gantry": lambda a: a[:1] == ["overlap"],
    "gh": lambda a: tuple(parse_opts(a, GH_VALUED)[1][:2]) in COORD_GH,
}

def redirect_ok(op, target):
    if op.lstrip("0123456789") == "<":
        return True
    return "<" not in op and (target == "/dev/null" or op.endswith(">&") and target.isdigit())

def check_segment(ctx, seg):
    tokens = seg.argv + [t for _, t in seg.redirects]
    need(not any(ORCH_RE.search(t) for t in tokens), ORCH_MSG)
    if ctx.role == "coordinator":
        need(all(redirect_ok(op, t) for op, t in seg.redirects), "coordinator may not write files or use heredocs; only 2>&1, >&2 or >/dev/null")
    if ctx.role == "worker":
        need(not any(PROTECTED_TOKEN.search(t) for op, t in seg.redirects if ">" in op), PROTECTED_MSG)
    if not seg.argv:
        return
    launched = launcher_args(seg.argv)
    if launched is not None:
        check_gantry(ctx, launched)
        return
    name, args = base(seg.argv[0]), seg.argv[1:]
    check_resolved(name, args)
    if ctx.role == "worker" and not (name in READONLY or name == "git" and git_split(args)[0] in GIT_READONLY):
        need(not any(PROTECTED_TOKEN.search(t) for t in tokens), PROTECTED_MSG)
    SEGMENT_RULES.get(name, lambda ctx, args: None)(ctx, args)
    if ctx.role == "coordinator":
        if name == "git":
            coord_git_cmd(ctx, args)
        else:
            need(COORD_BASH.get(name, NONE)(args), f"coordinator runs only read-only git/gh, orca and filters, not `{name}`; dispatch a worker")


def check_bash(ctx, inp):
    cmd = inp["command"]
    if ctx.repo().config is _BAD_CONFIG:
        deny(".git/gantry.json must be a JSON object; refusing commands until it parses")
    forbidden = ctx.repo().forbidden
    low = cmd.lower()
    for item in forbidden:
        if item.lower() in low:
            deny(f"{item} is listed in .git/gantry.json forbidden; never touch it from gantry")
    parsed = list(commands(cmd))
    for item in forbidden:
        needle = item.lower()
        if any(needle in t.lower() for seg in parsed for t in seg.argv):
            deny(f"{item} is listed in .git/gantry.json forbidden; never touch it from gantry")
    for seg in parsed:
        check_segment(ctx, seg)

def check_write(ctx, inp):
    path = os.path.realpath(os.path.join(ctx.cwd, inp.get("file_path") or inp["notebook_path"]))
    need(not PROTECTED_PATH.search(path), PROTECTED_MSG)

def check_read(ctx, inp):
    need(not ORCH_RE.search(inp["file_path"]), ORCH_MSG)

TOOL_RULES = {
    "Bash": check_bash, "Monitor": check_bash, "Read": check_read,
    **{t: check_write for t in ("Write", "Edit", "MultiEdit", "NotebookEdit")},
    "RemoteTrigger": lambda ctx, inp: deny("RemoteTrigger is disabled in gantry; use orca orchestration"),
}
COORD_TOOLS = {"Bash", "Read", "Skill", "ToolSearch", "AskUserQuestion", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Glob", "Grep"}

def decide(payload):
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    cwd = payload.get("cwd") or project or os.getcwd()
    governed = kit.governing(project, cwd, warn=True)
    if governed is None:
        return True, ""
    role, worker = role_of(payload)
    tool = payload["tool_name"]
    if role in ("unidentified", "unconfirmed"):
        if tool in SUBAGENT_TOOLS:
            return False, SUBAGENT_MSG
        role = "worker"
    if role == "operator":
        return True, ""
    ctx = Ctx(role, payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), worker)
    try:
        if role != "worker" and tool in SUBAGENT_TOOLS:
            deny(SUBAGENT_MSG)
        need(role == "worker" or tool in COORD_TOOLS, COORD_NO)
        TOOL_RULES.get(tool, lambda ctx, inp: None)(ctx, payload.get("tool_input") or {})
    except Deny as e:
        return False, str(e)
    return True, ""

def main():
    try:
        allow, reason = decide(json.load(sys.stdin))
    except Exception as e:
        allow, reason = False, f"guard failed closed ({type(e).__name__}: {e}); retry with a simpler command"
    if not allow:
        print(f"gantry guard: {reason}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
