# Ground truth

Recorded 2026-09-26 on macOS (Darwin 25.2.0). Each fact names its source: a command run here, an installed file, or a runtime experiment. Raw captures are in `docs/raw/`.

## Versions

| Tool | Version | Source |
| --- | --- | --- |
| Orca | 1.4.214 | `orca --version`, `Orca.app` `CFBundleShortVersionString` |
| Claude Code | 2.1.283 | `claude --version` |
| GitHub CLI | 2.92.0 | `gh --version` |
| pstack | 0.9.45, user scope, enabled | `claude plugin list`, `.claude-plugin/plugin.json` |
| Graphite (`gt`) | not installed | `which gt` |
| Python | macOS `python3` | used by the guard hook and launcher |

## Orca

`orca status --json` reports the app running, runtime `ready`, graph `ready`, local target, and capabilities including `orchestration.contract.v1`, `orchestration.worker-launch-preferences.v1`, and `terminal.prompt-delivery.v1`.

`orca orchestration --help` lists run-create, run-use, run-current, run-list, run-show, send, check, reply, inbox, task-create, task-list, task-update, worker-start, worker-show, worker-read, worker-stop, worker-abandon, worker-release, worker-retain, worker-list, dispatch, request-show, dispatch-show, ask, gate-create, gate-resolve, gate-list, and reset. `coordinator-start` and `coordinator-stop` are retired.

The orchestration skill has two parts:

- Installed skill: `~/.claude/skills/orchestration` is a symlink to `~/.agents/skills/orchestration/SKILL.md` (3,671 bytes, written 2026-09-26 21:27). It is a discovery stub with no version field. It tells the agent to fetch the guide from the binary so the guide never drifts from the CLI.
- Version-matched guide: `orca skills get orchestration --full` from Orca 1.4.214. The full text is in `docs/raw/orca-orchestration-skill.md` (795 lines: the kernel plus seven bundled references).

Facts from the guide that shaped the design:

- A dispatched worker must run `orchestration check --terminal <handle>` at checkpoints and before `worker_done` to read coordinator follow-ups. See the worker obligations, item 3.
- `worker-start --spec` creates the Task and its Dispatch in one call. `task-create --deps` plus `worker-start --task` handles planned DAGs.
- `check` returns the oldest FIFO Delivery and replays it until `--ack`. The type filter controls only when a wait wakes.
- Absence never authorizes stop, abandon, retry, or release. Only positive proof of exit does.
- "Do not create a gate merely to answer a worker's `ask`."

Worker launch settings live in the Orca profile at `~/Library/Application Support/orca/profiles/local-default/orca-data.json`: `defaultTuiAgent: claude`, and `agentDefaultArgs.claude: --dangerously-skip-permissions`. Every Orca-launched Claude worker already runs with permissions bypassed. `worker-start` has no flag for agent argv. Its help says the launch "follows the user's own setting for new agent tabs."

Measured on this machine:

- The injected prompt is the Orca preamble first, then the task spec under `=== TASK ===`. A spec that starts with `/poteto-mode` does not reach the TUI as a slash command. Source: `dispatch --dry-run --return-preamble`.
- The preamble says "Don't post to Slack, GitHub, or other channels during the run." gantry's worker rules override this for the PR, which is the task's deliverable.
- The preamble sets the heartbeat cadence at every 5 minutes and uses the bare `orca` executable.
- New-child worktrees land under `~/orca/workspaces/<repo>/<name>`.
- A decision gate moves its Task to `blocked`. `gate-resolve` moves it to `ready` and sends no inbox message. The coordinator must poll `gate-list`.
- Worktree placement: `current` reuses the coordinator's worktree. `new-child` creates a stacked child worktree and accepts `--base-branch`.
- Role signal for the guard: Orca launches each worker in its own terminal with `ORCA_TERMINAL_HANDLE=term_...` in its env. Claude Code runs the PreToolUse hook as a child of the `claude` process, so the hook inherits the handle and a Bash call cannot change it. Every Orca terminal has a handle, including the operator's own chat, so the handle alone does not mark a worker. A worker is a terminal whose handle is some row's `agentTerminalHandle` in `worker-list --json`. Rows carry `agentTerminalHandle`, `taskId`, `runId`, `terminalState`, and `dispatchStatus`. `task-list --run <runId> --json` rows carry `id` and `task_title`. Without `--run`, `worker-list` covers the calling terminal's bound Run, or every Run when it has none. Worker terminals have no bound Run (`run-current --from <worker handle>` returns `run: null`), so the lookup sees every Run, newest first, paged at 100. A `new-child` worker reads `terminalState: retained`, `retainedReason: user_takeover` while it still runs (recorded 2026-09-27), so the guard ignores terminal state. The coordinator sets `--task-title gantry-ship` on ship and landing tasks and `gantry-cleanup` on cleanup tasks. Workers cannot set titles, because the guard limits them to `orchestration send|ask|check`.

Agent Dashboard (`experimentalAgentDashboardMode: in-window`) and Cloud VM are experimental. gantry reads neither.

Cloud VM and remote hosts: `worker-start --on <saved-environment>` targets a connected remote Orca runtime saved with `orca environment add <pairing code>` (a host running `orca serve`). A remote worker needs `--worktree new-top-level --repo <exact remote selector>`, because remote `current` and `new-child` are invalid. The CLI cannot provision a Cloud VM. Its only VM command is `orca vm recipe doctor`. `orca environment list` shows no saved environments. v1 provisions nothing.

### Machine fix: the `orca` launcher

`/usr/local/bin/orca` is a root-owned symlink with mode `lrwx------`. macOS applies symlink permissions to `readlink`, so the launcher script's path resolution fails for a non-root user with "Unable to determine Orca.app path from symlink", even though executing through the link works. Workers call bare `orca`, so every worker call would fail. The fix is a user-owned symlink `~/.local/bin/orca -> /Applications/Orca.app/Contents/Resources/bin/orca`. `~/.local/bin` precedes `/usr/local/bin` on PATH. Revert with `rm ~/.local/bin/orca`. The permanent fix is `sudo ln -sfh /Applications/Orca.app/Contents/Resources/bin/orca /usr/local/bin/orca`.

### Claude workspace trust

`worker-start --worktree new-child` first failed at `agent_readiness` with `terminal_handle_stale`. The worker terminal showed Claude Code's workspace-trust dialog for the new worktree path. `--dangerously-skip-permissions` does not skip that dialog in an interactive TUI. For a linked worktree, Claude Code reads the primary checkout's `projects` entry in `~/.claude.json`. A trusted ancestor such as `~/src` is not enough. `gantry install` sets `hasTrustDialogAccepted` on the primary checkout's entry. That is the same write the dialog makes when you accept it. After that write, the same start succeeded.

## Claude Code

Official docs, fetched 2026-09-26 and summarized with quotes in `docs/raw/claude-code-docs.md`:

- Agent definitions (`.claude/agents/<name>.md`) support `tools`, `disallowedTools`, `model`, `permissionMode`, `skills` (preload), `hooks`, and other fields. `claude --agent <name>` makes the agent the main thread for the session.
- Plugin agents ignore `permissionMode`, `hooks`, `mcpServers`, and `initialPrompt`: "An agent file can't add hooks or MCP servers on its own."
- Hooks: "`PreToolUse` hooks fire before any permission-mode check, in every permission mode ... A hook that returns `permissionDecision: "deny"` blocks the tool even in `bypassPermissions` mode or with `--dangerously-skip-permissions`." Exit code 2 blocks and returns stderr to the model. Any other nonzero exit is a non-blocking error, so a hook must fail closed on its own.
- `--disallowedTools` removes tools from the model's context.

Runtime checks in a scratch repo (`claude -p --model haiku`, 2.1.283):

| Question | Result |
| --- | --- |
| Does a project PreToolUse hook run under `--dangerously-skip-permissions`? | Yes. Hook input had `permission_mode: bypassPermissions`. |
| Does exit 2 block under bypass? | Yes. Both `touch` calls appear in `permission_denials`, and no file was created. |
| Does `--agent` frontmatter `disallowedTools` remove tools under bypass? | Yes. The `init` tool list for `--agent c` lacked Edit, Write, NotebookEdit, and Agent. |
| Does hook input name the main-thread agent? | Yes. `agent_type: "c"` for `claude --agent c`. |
| What remains after removing the four edit tools? | Workflow, Monitor, CronCreate, RemoteTrigger, SendMessage, and others. Workflow and Monitor run agents or commands outside the Bash hook, so the coordinator gets a `tools:` allowlist as well. |

Skill loading: pstack is a user-scope plugin, so its skills load as `pstack:<name>`. Its SessionStart hook tells every session to invoke `pstack:poteto-mode` for multi-file work.

## GitHub

Branch protection and rulesets on a private repo on the free plan return HTTP 403 "Upgrade to GitHub Pro or make this repository public to enable this feature." No server-side rule can protect `main` there. gantry enforces the limits with the Claude guard hook plus a git `pre-push` hook.

## pstack 0.9.45

Path: `~/.claude/plugins/cache/pstack-claude/pstack/0.9.45/skills/poteto-mode/`.

- `poteto-mode`: routes each task to a playbook. It names "Opening a PR" as the end of every playbook.
- verify: pstack has no skill named `verify`. poteto-mode says the Claude Code bundled `/verify` "runs only when the user types `/verify`". For UIs it uses the project `.claude/skills/verify/` (generated by `/create-verification-skill`) and falls back to `run`.
- Opening a PR (`playbooks/opening-a-pr.md`): gh by default, Origin if its CLI resolves the repo, "Do not require Graphite (`gt`)". It supports stacks as a base-branch chain: "Create a child with ... `gh pr create --base <parent-branch>`". PRs open ready, never draft. Opening a PR "does not start a babysit".
- Babysit (`playbooks/babysit.md`): modes `drive`, `background`, `threads-only`, and `check`. `drive` "ends at merge-ready". It never merges: "Babysitting never authorizes merging." It answers review threads with `gh api --method POST repos/<owner>/<repo>/pulls/<pr>/comments/<id>/replies`.
- Shipping (`playbooks/shipping.md`): "GitHub CLI (`gh`) is the default ... Never require Graphite (`gt`)." It lands stacks: "Capture the affected chain bottom-to-top", then lands "the contiguous verified run from the root". Step 2 runs one independent verifier subagent per PR, which posts PASS, PASS+NOTES, or FAIL on the PR. Step 7 merges with `gh pr merge <pr> --repo <owner/repo> --squash --match-head-commit <verified-head>`. Step 8 forbids `--auto` without durable repository gates and never uses `--admin`. The restack pushes with `git push --force-with-lease="refs/heads/$child:$captured_remote_head"` (`references/merge-safety.md`).
- Orchestrate (`playbooks/orchestrate.md`): a standing coordinator chat that fans out subagents over days. It uses `skills/poteto-mode/scripts/orch/`. gantry workers must never route there.

Shipping needs `gh` only. Graphite is neither required nor used, so its absence changes nothing. Shipping supports stacked PRs, so a dependent task stacks on its parent's branch instead of waiting for the parent to merge.

## Found during the proof run

- A spec whose `=== TASK ===` block starts with `/poteto-mode` works. Every worker in the run called the Skill tool with `pstack:poteto-mode` as its first skill.
- `gate-resolve` takes no `--run` and resolves only gates in the caller's bound Run. A human resolves a gantry gate by telling the coordinator, or from the Agent Dashboard, not from another terminal.
- The prompt race did not reproduce: 0 of 8 `new-child` starts raced. First heartbeats arrived 16 to 19 seconds after dispatch.
- A user-level Claude Code hook runs in every gantry session, because user hooks load everywhere. A pattern `push --force` also matches `push --force-with-lease`, and the same hook can block `reset --hard`. In the proof run that blocked Shipping's lease push on the stacked PR. Shipping landed the child anyway by retargeting it to `main`, because the parent's squash tree matched. A stack whose parent squash differs from its original commits would stall there. gantry merges its own hooks into user settings and leaves every other hook in place. The pattern `push (--force|-f)([^-]|$)` allows the lease form.
- The guard cannot expand shell variables, so `git -C $D/x push` fails closed. The hook now says to pass a literal path.

### Message pointer into the coordinator terminal

Orca 1.4.214 delivers a coordinator's mail by typing into its terminal when no live waiter covers the message. In the app bundle (`out/main/index.js`), `deliverPendingMessages` writes `\nYou have N orchestration messages. Run \`orca orchestration check --run <run>\`.\n` to the PTY. 500 ms later it writes `\r`. It skips a message when `typeHasLiveWaiter` finds a waiting `check --wait` whose `--types` filter includes that type, or that has no filter. No setting turns the pointer off. The `\r` submits whatever the operator has typed, together with the pointer line.

Measured 2026-10-02 with a Claude Code TUI bound to a scratch Run, the draft `DRAFT-DO-NOT-SEND` typed and unsent, and one `heartbeat` sent to `run:<id>`:

| Waiter in flight | Result |
| --- | --- |
| none | Submitted prompt `DRAFT-DO-NOT-SEND` + `You have 1 orchestration message...`; the agent started a turn |
| `check --wait --types worker_done,question,escalation` (the old coordinator loop) | Same submission |
| `check --wait` with no `--types` | No pointer; the waiter got the heartbeat; a manual Enter later submitted exactly `DRAFT-DO-NOT-SEND` |

A coordinator `check --wait` is exclusive: a second waiter on the Run gets `waiter_exists`. So gantry runs one daemon per Run, `~/.gantry/current/kit/gantry-mailbox`, that loops `check --wait --timeout-ms 120000 --ack <previous delivery>` with no filter. Each call acks and re-waits in one request. The daemon appends every message to `$TMPDIR/gantry-mailbox/<run>/journal.jsonl`. The coordinator never calls `check`; its background `gantry-mailbox wait` reads the journal. The daemon exits when `terminal show` reports the coordinator terminal `orphaned` or the terminal binds another Run.

Proof with the real `gantry-coordinator` agent: it started the mailbox as a background Bash call. Six heartbeats, a status, and three heartbeats after it ended its turn with no wait in flight produced no pointer. A `handoff` completed its wait, and it replied, while the draft `SECOND-DRAFT-KEEP` stayed in the input. A manual Enter then submitted exactly that draft. The daemon logged its exit 85 seconds after the terminal closed. Orca rewrites an `escalation` or `question` from a terminal with no active Dispatch into a `status` "Rejected escalation" message, so such probes need `handoff` or `merge_ready`.

Remaining window: between one daemon `check` returning and the next starting, about 0.25 s of CLI startup (measured for `worker-list`). A message that arrives inside that window still gets the pointer.

## Deviations from the prompt

1. Workers may run `orchestration check` as well as `send` and `ask`. The Orca worker contract requires `check` to read coordinator follow-ups before `worker_done`.
2. The coordinator loses more than the four edit tools. Agent, Workflow, and Monitor also go, because each can edit or execute outside the Bash hook.
3. Branch protection is unavailable on a free-plan private repo (HTTP 403). A git `pre-push` hook backs up the guard hook.
4. `gantry install` writes one key to `~/.claude.json` (not `~/.claude/`): trust for the target repo's primary checkout. Without it every new-child worker wedges on the trust dialog.
5. With `autoMerge: false`, landing requires changing the setting to `true`, because the guard blocks every merge while it is `false`. The decision gate says so.
6. The Orca guide says not to gate a worker's `ask`. gantry gates only questions that need the human, as the prompt asks, and replies to the `ask` after the gate resolves.
7. A user hook matching `push --force` blocks the `--force-with-lease` restack that the prompt allows. gantry does not override user hooks. See "Found during the proof run".
8. The Orca guide says to process a batch before `--ack`. The gantry mailbox daemon acks each batch on its next `check` after writing it to the journal, so the journal, not Orca's FIFO, is the coordinator's durable record.
