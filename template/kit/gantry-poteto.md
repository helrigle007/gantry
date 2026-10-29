# Gantry adapter for poteto-mode

Read this file once per session, after `<pstack>/docs/harness/claude-code.md` and before you follow `<pstack>/skills/poteto-mode/SKILL.md`. `<pstack>` is the absolute path in `GANTRY_PSTACK`.

This file maps poteto-mode onto Orca the way `docs/harness/claude-code.md` maps it onto Claude Code. Where this file differs from that adapter or from poteto-mode, this file wins. Do not edit pstack. Read it where it is installed.

## Where pstack is

The launcher sets `GANTRY_PSTACK` to the newest installed copy that contains `skills/poteto-mode/SKILL.md`. Newest is the dotted numeric directory name. The usual path is `~/.claude/plugins/cache/cursor-plugins/pstack/<version>/`. Another folder under `~/.claude/plugins/cache/*/pstack/<version>/` wins when its version is newer. A tie goes to `cursor-plugins`.

When `echo "$GANTRY_PSTACK"` is empty, print `! blocked run · pstack not installed · stopped` and stop. Do not run `run-create`. Do not name a playbook yourself. This kit has no second classifier.

This release was tested against pstack `0.15.36`, recorded as `PSTACK_TESTED` in the kit. When the resolved copy is newer, `GANTRY_PSTACK_NOTICE` holds `! pstack <installed> newer than tested 0.15.36 · continuing`. Print that line once and continue. An equal or older version leaves the variable empty. Print nothing in that case.

## Who owns the playbook

This coordinator chat is the chat that received `/pstack:poteto-mode`. You own the playbook, the todo list, and every spawn. Match the playbook and choose skills by following poteto-mode. Gantry does not classify them.

A worker is a poteto-agent. It reads `SKILL.md` at the absolute path in its brief and does that brief. It does not run `/poteto-mode` and it does not match a playbook. Poteto-mode's rule, never spawn a poteto-agent to run the playbook itself, is this split.

If the user types `/pstack:poteto-mode`, or the Skill tool refuses that skill, you are still this chat. Read the three files and continue.

## Spawn

One poteto-mode spawn is one Orca worker. Then one `gantry wait`, as the coordinator prompt's wave says.

| poteto-mode | Gantry |
| --- | --- |
| Spawn a `poteto-agent`, or call `Task` or `Agent` | `orca orchestration worker-start` with the brief below. Never the Agent tool, Task, Workflow, or Monitor. The guard denies those four in this chat. A denial means you dispatch a worker. Do not retry the tool. |
| `subagent_type` `poteto-agent` or `pstack:poteto-agent` | The first lines of the worker brief. There is no Agent call to put a subagent type on. |
| `environment: "cloud"` | `--worktree new-child` and the `--base-branch` the wave section names. |
| `environment: "local"`, or a read-only unit you cannot finish yourself | `--worktree current`. No fetch and no `--base-branch`. |
| A routed skill that names its own `subagent_type` (`how`, `why`, `interrogate`, `reflect`, `swarm`) | Still one Orca worker. The brief names the skill's `SKILL.md` to Read. Do not override that skill, and do not call Agent to honor its `subagent_type`. |
| A model slug | Omit it. The worker is `--agent claude`. |
| Open a todolist | `TodoWrite` when the session offers it. Copy the matched playbook's steps in verbatim. A skipped step stays, with `skip: <reason>`. |
| Run a pstack skill (`/name` or "the **name** skill") | `Read <pstack>/skills/<name>/SKILL.md` and follow it. The Skill tool refuses most pstack skills. |
| Land, ship, or `gh pr merge` | Read `~/.gantry/current/kit/gantry-ship.md` and follow it. Do not merge, push, or remove a worktree from this chat. |

## Orchestrate

Do not open `skills/poteto-mode/playbooks/orchestrate.md` and do not run anything under `skills/poteto-mode/scripts/orch/`. The guard denies both the Read and a shell command that names those paths. A program-scale objective stays in this chat. Match the narrower playbook, or Read `<pstack>/skills/figure-it-out/SKILL.md` when poteto-mode says no bundled playbook fits. Every spawn is still one Orca worker. Orchestrate's `Task` call is not a way around Orca.

## Skills that need the diff

You have no edit tools. A poteto-mode trigger that reads or changes the diff, the commit, or the PR body runs in the worker. Name that skill on the Skills line. When the trigger matches, that includes deslop before commit, no-comments before review, unslop for prose the worker writes, and technical-writing for the PR description. `/interrogate` goes on the same line when its trigger matches. Do not name a skill whose trigger did not match. Do not drop one that did. The worker Reads that skill's `SKILL.md` under `<pstack>` before it acts.

## What every brief still carries

Poteto-mode does not know the charter, the branch, or which paths this worker may edit. Every brief has the poteto-agent lines, the Skills line, Charter, the gantry worker rules, Outcome, Constraints, Ownership, Base, and Proof. Copy every charter rule. Write `Charter: none` only when there are no rules. Write `Skills: none` only when no trigger and no playbook step names a skill for this worker.

Inside a worker, Claude Code's Agent tool stays allowed for parallel work on that one task. The worker rules forbid passing `/poteto-mode` to those helpers, and they forbid spawning a poteto-agent to run the playbook. They do not forbid the Agent tool. A worker never creates an Orca Run, task, or worker.

Overlap is unchanged. `--checkpoint scoped` before the first `worker-start` for a task. `--checkpoint pre-pr` before that worker opens the pull request. The playbook does not replace either check.

## Say

The coordinator prompt's Say section is the whole user-facing report. It overrides poteto-mode's first-message restatement and every playbook's reply lines. Do not restate the goal, the plan, or the playbook name. One line when the state changes.
