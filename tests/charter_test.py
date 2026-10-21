#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GANTRY = os.path.join(ROOT, "bin", "gantry")
KIT = os.path.join(ROOT, "template", "kit")
RUN = os.path.join(KIT, "gantry_run.py")
sys.path.insert(0, KIT)
import gantry_kit

KEY = "https://github.com/example-org/example-repo"
LORE = "Hey, always check Lore's JIRA tickets"
E2E = "Run the e2e suite before any PR that touches checkout/.\nSkip it for docs-only changes."
FORCE = "Never force-push a shared branch."
HEADER = "gantry charter. Standing rules from the user. Follow every one, and copy the lines a task needs onto its brief's Charter line."
BLOCK = [
    HEADER,
    "every repo:",
    "- g-071c6d Never force-push a shared branch.",
    f"{KEY}:",
    "- r-fc5233 Hey, always check Lore's JIRA tickets",
    "- r-b7b862 Run the e2e suite before any PR that touches checkout/.",
    "  Skip it for docs-only changes.",
]
FILE_EXAMPLE = (
    b'{\n'
    b'  "version": 1,\n'
    b'  "rules": {\n'
    b'    "r-fc5233": "Hey, always check Lore\'s JIRA tickets",\n'
    b'    "r-b7b862": "Run the e2e suite before any PR that touches checkout/.\\nSkip it for docs-only changes."\n'
    b'  }\n'
    b'}\n'
)
UNREADABLE = [HEADER, "every repo:", "- unreadable; run gantry doctor", f"{KEY}:", f"- r-fc5233 {LORE}"]
HINT = "; add --global for a rule that applies in every repo"
ORCA = """#!/usr/bin/env python3
import json, sys
results = {"run-current": {"run": {"id": "run_charter", "objective": "keep the rules"}}}
print(json.dumps({"ok": True, "result": results.get(sys.argv[2] if len(sys.argv) > 2 else "", {})}))
"""
BAD_ORIGIN = "gantry: origin not-a-url is not a parseable git remote. Expected https://host/owner/repo or git@host:owner/repo\n"

CLI_ROWS = [
    (["charter", "add", LORE], f"r-fc5233 saved for {KEY}: {LORE}\n", "", 0),
    (["charter", "add", LORE], f"r-fc5233 already saved for {KEY}: {LORE}\n", "", 0),
    (["charter", "add", "--global", FORCE], f"g-071c6d saved for every repo: {FORCE}\n", "", 0),
    (["charter", "list"], "\n".join(BLOCK[:5]) + "\n", "", 0),
    (["charter", "rm", "r-fc5233"], f"r-fc5233 removed from {KEY}: {LORE}\n", "", 0),
    (["charter", "rm", "r-fc5233"], "", f"gantry: no charter rule r-fc5233 for {KEY}; run gantry charter list\n", 1),
    (["charter", "rm", "fc5233"], "", "gantry: fc5233 is not a charter id; run gantry charter list\n", 1),
    (["charter", "add", "   "], "", "gantry: charter rule is empty\n", 1),
    (["charter", "add", "Always", "check"], "", "gantry: charter add takes the rule as one quoted argument\n", 1),
    (["charter", "add", "--global"], "", "gantry: charter add takes the rule as one quoted argument\n", 1),
    (["charter", "add", "x" * 1001], "", "gantry: charter rule is 1001 characters; the limit is 1000\n", 1),
    (["charter", "list", "--global"], "", "gantry: charter list takes no arguments\n", 1),
    (["charter", "frob"], "", "gantry: charter takes add, list, or rm\n", 1),
]


def outside_rows(scratch):
    return [
        (["charter", "add", "Always check the linked ticket"], "", f"gantry: {scratch} is not a git repository{HINT}\n", 1),
        (["charter", "add", "--global", FORCE], f"g-071c6d already saved for every repo: {FORCE}\n", "", 0),
        (["charter", "list"], "", f"gantry: {scratch} is not a git repository\n", 1),
        (["charter", "rm", "r-b7b862"], "", f"gantry: {scratch} is not a git repository\n", 1),
    ]


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def status(repo):
    return subprocess.check_output(["git", "-C", repo, "status", "--porcelain"], text=True)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class Fixture:
    def __init__(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(tmp, "home")
        self.repo = os.path.join(tmp, "repo")
        self.scratch = os.path.join(tmp, "scratch")
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(os.path.join(self.home, ".claude", "plugins", "cache", "x", "pstack", "0", "skills", "poteto-mode"))
        with open(os.path.join(self.home, ".claude.json"), "w") as handle:
            handle.write("{}\n")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "orca"), "w") as handle:
            handle.write(ORCA)
        os.chmod(os.path.join(self.bin, "orca"), 0o755)
        os.makedirs(self.scratch)
        self.git_repo(self.repo, f"{KEY}.git")
        self.charters = os.path.join(self.home, ".gantry", "charters")
        self.global_file = os.path.join(self.charters, "global.json")
        self.repo_file = os.path.join(self.charters, "repos", "github.com", "example-org", "example-repo.json")

    def git_repo(self, path, origin):
        os.makedirs(path)
        sh("git", "init", "-q", "-b", "main", cwd=path)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
        sh("git", "remote", "add", "origin", origin, cwd=path)
        return path

    def env(self, **extra):
        return {"HOME": self.home, "PATH": self.bin + os.pathsep + os.environ["PATH"], "TMPDIR": self.tmp, **extra}

    def run(self, *args, cwd=None):
        return subprocess.run([GANTRY, *args], cwd=cwd or self.repo, env=self.env(), capture_output=True, text=True)


def row_checks(fx, rows, cwd):
    checks = []
    for argv, out, err, code in rows:
        result = fx.run(*argv, cwd=cwd)
        got = (result.stdout, result.stderr, result.returncode)
        checks.append((got == (out, err, code), f"{argv} in {cwd}: {got!r}"))
        checks.append((status(fx.repo) == "", f"porcelain empty after {argv}"))
    return checks


def case_cli_table(fx):
    checks = row_checks(fx, CLI_ROWS, fx.repo)
    checks += row_checks(fx, outside_rows(fx.scratch), fx.scratch)
    odd = fx.git_repo(os.path.join(fx.tmp, "odd"), "not-a-url")
    checks += row_checks(fx, [
        (["charter", "add", "Always check the linked ticket"], "", BAD_ORIGIN, 1),
        (["charter", "list"], "", BAD_ORIGIN, 1),
    ], odd)
    checks.append((status(odd) == "", "porcelain empty in the unparseable-origin repo"))
    return checks


def case_file_bytes(fx):
    fx.run("charter", "add", LORE)
    fx.run("charter", "add", E2E)
    fx.run("charter", "add", "--global", FORCE)
    listed = fx.run("charter", "list")
    return [
        (read(fx.repo_file) == FILE_EXAMPLE, read(fx.repo_file).decode()),
        (read(fx.global_file) == b'{\n  "version": 1,\n  "rules": {\n    "g-071c6d": "Never force-push a shared branch."\n  }\n}\n', read(fx.global_file).decode()),
        (listed.stdout == "\n".join(BLOCK) + "\n" and listed.returncode == 0, listed.stdout),
        (status(fx.repo) == "", "porcelain empty"),
    ]


def case_idempotent(fx):
    fx.run("charter", "add", "--global", FORCE)
    fx.run("charter", "add", LORE)
    checks = []
    for path, argv in ((fx.global_file, ["--global", FORCE]), (fx.repo_file, [LORE])):
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        before = (read(path), os.stat(path).st_mtime_ns)
        again = fx.run("charter", "add", *argv)
        checks.append((again.returncode == 0 and " already saved for " in again.stdout, again.stdout + again.stderr))
        checks.append(((read(path), os.stat(path).st_mtime_ns) == before, f"{path} bytes and mtime unchanged"))
    return checks


def case_concurrent(fx):
    texts = [f"Distinct rule number {n}." for n in range(8)] + ["One shared rule."] * 4
    env = fx.env()
    procs = [subprocess.Popen([GANTRY, "charter", "add", text], cwd=fx.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for text in texts]
    results = [(proc.communicate(), proc.returncode) for proc in procs]
    rules = json.loads(read(fx.repo_file))["rules"]
    shared = [out for (out, _err), _code in results[8:]]
    ids = {line.split(" ", 1)[0] for line in shared}
    return [
        (all(code == 0 for _out, code in results), f"every add exits 0: {results}"),
        (len(rules) == 9, f"9 rules: {rules}"),
        (sorted(rules.values()) == sorted(set(texts)), "every text stored once"),
        (len(ids) == 1 and ids <= set(rules), f"one shared id: {shared}"),
        (sum(" already saved for " in line for line in shared) == 3, f"one copy saved, three already saved: {shared}"),
        (status(fx.repo) == "", "porcelain empty"),
    ]


CRASH = """
import os, sys
sys.path.insert(0, sys.argv[1])
import gantry_kit
def boom(src, dst):
    raise OSError("crash before replace")
os.replace = boom
try:
    gantry_kit._charter_add(gantry_kit.GLOBAL, "Crash in the middle of a write.")
except OSError:
    sys.exit(3)
"""


def case_crash_before_replace(fx):
    fx.run("charter", "add", "--global", FORCE)
    before = read(fx.global_file)
    crash = subprocess.run([sys.executable, "-c", CRASH, KIT], env=fx.env(), capture_output=True, text=True)
    after_crash = read(fx.global_file)
    listed = fx.run("charter", "list")
    retry = fx.run("charter", "add", "--global", "Crash in the middle of a write.")
    return [
        (crash.returncode == 3, f"crash injected: {crash.returncode} {crash.stderr}"),
        (after_crash == before, "old bytes kept"),
        (json.loads(before)["rules"] == {"g-071c6d": FORCE}, "old file still parses"),
        (listed.returncode == 0 and "Crash in the middle" not in listed.stdout, listed.stdout),
        (retry.returncode == 0 and " saved for every repo: " in retry.stdout, retry.stdout + retry.stderr),
    ]


def case_corrupt(fx):
    fx.run("charter", "add", "--global", FORCE)
    fx.run("charter", "add", LORE)
    with open(fx.global_file, "w") as handle:
        handle.write("{not json")
    before = read(fx.global_file)
    add = fx.run("charter", "add", "--global", "Another rule.")
    rm = fx.run("charter", "rm", "g-071c6d")
    listed = fx.run("charter", "list")
    doctor = fx.run("doctor", fx.repo)
    repo_add = fx.run("charter", "add", E2E)
    return [
        (add.returncode == 1 and add.stderr.startswith("gantry: ") and "global.json" in add.stderr and add.stdout == "", add.stderr),
        (rm.returncode == 1 and "global.json" in rm.stderr, rm.stderr),
        (read(fx.global_file) == before, "corrupt bytes kept"),
        (listed.returncode == 1, f"list exits 1: {listed.returncode}"),
        (listed.stdout == "\n".join(UNREADABLE) + "\n", listed.stdout),
        ("global.json" in listed.stderr, listed.stderr),
        (doctor.returncode == 1, f"doctor exits 1: {doctor.returncode}"),
        ("charter: every repo unreadable, this repo 1\n" in doctor.stdout, doctor.stdout),
        (repo_add.returncode == 0, "the readable scope still takes an add"),
    ]


def case_run_fail_open(fx):
    fx.run("install", fx.repo)
    fx.run("charter", "add", LORE)
    with open(fx.global_file, "w") as handle:
        handle.write("{not json")
    write = subprocess.run([sys.executable, RUN, "write"], cwd=fx.repo, env=fx.env(), capture_output=True, text=True)
    payload = {"hook_event_name": "SessionStart", "session_id": "s", "transcript_path": "", "cwd": fx.repo, "agent_type": "gantry-coordinator"}
    sessions = {
        source: subprocess.run([sys.executable, RUN], input=json.dumps({**payload, "source": source}), cwd=fx.repo, env=fx.env(), capture_output=True, text=True)
        for source in ("startup", "compact")
    }
    checks = [
        (write.returncode == 0, f"gantry_run.py write exits 0: {write.returncode} {write.stderr}"),
        ("\n".join(UNREADABLE) + "\nNext: " in write.stdout, write.stdout),
    ]
    for source, proc in sessions.items():
        injected = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout else ""
        checks.append((proc.returncode == 0, f"SessionStart {source} exits 0: {proc.stderr}"))
        checks.append(("\n".join(UNREADABLE) in injected, f"SessionStart {source} carries the unreadable line: {proc.stdout}"))
    checks.append((status(fx.repo) == "", "porcelain empty"))
    return checks


def case_doctor(fx):
    empty = fx.run("doctor", fx.repo)
    fx.run("charter", "add", "--global", FORCE)
    fx.run("charter", "add", LORE)
    fx.run("charter", "add", E2E)
    healthy = fx.run("doctor", fx.repo)
    outside = fx.run("doctor", cwd=fx.scratch)
    return [
        (empty.returncode == 0 and "charter: every repo 0, this repo 0\n" in empty.stdout, empty.stdout),
        (healthy.returncode == 0 and "charter: every repo 1, this repo 2\n" in healthy.stdout, healthy.stdout),
        (outside.returncode == 0 and "charter: every repo 1, this repo none\n" in outside.stdout, outside.stdout),
    ]


def case_install_lifecycle(fx):
    installed = fx.run("install", fx.repo)
    fx.run("charter", "add", "--global", FORCE)
    fx.run("charter", "add", LORE)
    before = (read(fx.global_file), read(fx.repo_file))
    with open(os.path.join(fx.home, ".gantry", "registry.json"), "w") as handle:
        handle.write("{")
    repaired = fx.run("install", "--repair", fx.repo)
    after_repair = (read(fx.global_file), read(fx.repo_file))
    gone = fx.run("uninstall", fx.repo)
    after_uninstall = (read(fx.global_file), read(fx.repo_file))
    again = fx.run("install", fx.repo)
    listed = fx.run("charter", "list")
    removed = fx.run("uninstall", "--all")
    return [
        (installed.returncode == 0 and repaired.returncode == 0 and gone.returncode == 0, installed.stderr + repaired.stderr + gone.stderr),
        (after_repair == before, "install --repair keeps the charter"),
        (after_uninstall == before, "per-repo uninstall keeps the charter"),
        (again.returncode == 0 and f"- r-fc5233 {LORE}" in listed.stdout, "installing again brings the rules back"),
        (removed.returncode == 0 and not os.path.exists(fx.charters), "uninstall --all removes the charter"),
        (status(fx.repo) == "", "porcelain empty"),
    ]


def write_rules(path, rules):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump({"version": 1, "rules": rules}, handle, indent=2)
        handle.write("\n")


def block_for(fx):
    saved = os.environ.get("HOME")
    os.environ["HOME"] = fx.home
    try:
        return gantry_kit.charter_block(None, fx.repo)
    finally:
        os.environ["HOME"] = saved


def ids_in(lines):
    return [line.split(" ")[1] for line in lines if line.startswith("- ") and line.split(" ")[1][:2] in ("g-", "r-")]


def case_block_cap(fx):
    checks = []
    checks.append((block_for(fx) == [], "no charter files give no block"))
    write_rules(fx.global_file, {f"g-{n:06x}": f"Global rule {n}." for n in range(3)})
    write_rules(fx.repo_file, {f"r-{n:06x}": f"Repo rule {n} " + "x" * 70 for n in range(60)})
    block = block_for(fx)
    kept = ids_in(block)
    repo_kept = [rule_id for rule_id in kept if rule_id.startswith("r-")]
    newest = [f"r-{n:06x}" for n in range(60 - len(repo_kept), 60)]
    checks += [
        (len("\n".join(block)) <= 3000, f"lopsided block is {len(chr(10).join(block))} characters"),
        (kept[:3] == ["g-000000", "g-000001", "g-000002"], "the smaller scope keeps every rule"),
        (repo_kept == newest, f"the larger scope drops its oldest rules first: {repo_kept[:2]}"),
        (block[-1] == f"- {60 - len(repo_kept)} more rules; run gantry charter list", block[-1]),
    ]
    write_rules(fx.global_file, {f"g-{n:06x}": f"Global {n} " + "y" * 940 for n in range(1, 4)})
    write_rules(fx.repo_file, {f"r-{n:06x}": f"Repo {n} " + "z" * 942 for n in range(1, 4)})
    block = block_for(fx)
    checks += [
        (ids_in(block) == ["g-000003", "r-000003"], f"a tie drops the oldest global rule first: {ids_in(block)}"),
        (block[-1] == "- 4 more rules; run gantry charter list", block[-1]),
        (len("\n".join(block)) <= 3000, "tie block fits"),
    ]
    tall = "a" + "\na" * 499
    write_rules(fx.global_file, {"g-000001": tall})
    write_rules(fx.repo_file, {"r-000001": tall.replace("a", "b")})
    block = block_for(fx)
    checks += [
        (ids_in(block) == ["r-000001"], f"two tall rules keep the repo rule: {ids_in(block)}"),
        (block[:2] == [HEADER, f"{KEY}:"] and block[-1] == "- 1 more rules; run gantry charter list", f"{block[:2]} {block[-1]}"),
        (block[3:-1] == ["  b"] * 499, "continuation lines stay whole"),
        (len("\n".join(block)) <= 3000, "one tall rule fits"),
    ]
    with open(fx.global_file, "w") as handle:
        handle.write("[]")
    block = block_for(fx)
    checks.append((block[:3] == [HEADER, "every repo:", "- unreadable; run gantry doctor"], f"unreadable global: {block[:3]}"))
    checks.append((ids_in(block) == ["r-000001"], "the readable repo scope still renders"))
    return checks


CASES = [
    case_cli_table, case_file_bytes, case_idempotent, case_concurrent, case_crash_before_replace,
    case_corrupt, case_run_fail_open, case_doctor, case_install_lifecycle, case_block_cap,
]


def main():
    failures = 0
    total = 0
    for case in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            checks = case(Fixture(os.path.realpath(tmp)))
        for ok, label in checks:
            total += 1
            if not ok:
                failures += 1
                print(f"FAIL {case.__name__}: {label}")
    print(f"{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
