# Claude Code Documentation Report
**Date:** 2026-09-26  
**Claude Code Version:** 2.1.283  
**Documentation Source:** https://code.claude.com/docs

---

## 1. Agent Definitions (.claude/agents/*.md)

### Frontmatter Field List
Agent definition files use YAML frontmatter with these fields:

**Supported fields:**
- `name` (required): Unique identifier for the agent
- `description` (required): How Claude should delegate to this agent
- `tools`: Comma-separated list of allowed tools; inherits all if omitted
- `disallowedTools`: Tools to deny
- `model`: `sonnet`, `opus`, `haiku`, `fable`, or full ID (e.g., `claude-opus-5-5`)
- `permissionMode`: `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`
- `memory`: `user`, `project`, or `local` for persistent memory
- `skills`: Skills to preload
- `mcpServers`: MCP servers available to this agent
- `isolation`: Set to `worktree` for git worktree isolation
- `maxTurns`: Max agentic turns before stopping
- `background`: Set to `true` to keep in background
- `omitClaudeMd`: Skip CLAUDE.md files at startup
- `hooks`: **See section below**
- `color`, `cacheTtl`: Additional fields

### Do agent definitions support hooks in frontmatter, and do they fire for main-thread --agent?

**YES, with restrictions.** From plugins/components.md:

> "A plugin agent's frontmatter follows these rules: **Supported fields**: `name`, `description`, `model`, `effort`, `maxTurns`, `tools`, `disallowedTools`, `skills`, `memory`, `background`, `omitClaudeMd`, `isolation`, `color`, and the `cacheTtl` key of `experimental`. ... **Ignored fields**: `permissionMode`, `hooks`, `mcpServers`, and `initialPrompt`. An agent file can't add hooks or MCP servers on its own."

**For custom agents (project/user)**, hooks CAN be defined in `.claude/agents/<name>/SKILL.md` frontmatter. From sub-agents.md:

> "Define hooks in the subagent's frontmatter—they only run while that subagent is active"

However, **plugin agents cannot define hooks**—they are ignored. **Custom agents can, but only while that specific agent is running**, not globally for the session.

### Does tools/disallowedTools apply when using --agent?

**YES.** Tools and disallowedTools are enforced when an agent is the main thread via `claude --agent <name>`. The subagent documentation confirms:

> "Subagents inherit your session's built-in and MCP tools, but... Background subagents get a reduced tool set"

And from the settings reference: When `tools` or `disallowedTools` is set in an agent definition, those apply when the agent runs.

---

## 2. CLI Flag: `claude --agent <name>`

### Semantics

From the agents.md documentation:

> "**Session-wide** (replaces main prompt): `claude --agent code-reviewer`"

This flag makes the named agent the main conversation thread (not a subagent). The agent's system prompt, tool restrictions, and model apply to the entire session.

**The agent must exist in one of these locations (priority order):**
1. Managed settings
2. `--agents` CLI flag
3. `.claude/agents/` (project-specific)
4. `~/.claude/agents/` (user-level)
5. Plugin `agents/` directory

### `--agents` JSON flag

The `--agents` flag allows defining subagents dynamically:

```bash
claude --agents '{"name":{"description":"...","prompt":"..."}}'
```

This creates agents in memory for the session without needing `.claude/agents/` files.

---

## 3. Tool Allow/Deny Flags: --allowedTools, --disallowedTools, --tools

### Semantics

**`--allowedTools`** (or `--allow-tools`): Tools that execute without prompting

**`--disallowedTools`** (or `--disallow-tools`): Tools denied; they cannot run

**`--tools`**: Not found as a primary flag; permission rules in settings use this concept

### Whether disallowed tools are REMOVED from context vs. merely denied

From the permissions.md documentation, **disallowed tools are REMOVED from the model's context**. They do not appear in the tool list the model sees, preventing the model from even attempting to call them.

Quote from hooks-guide.md:
> "A hook returning `'allow'` doesn't bypass deny rules from settings, and it can't suppress the prompt for MCP tools marked `requiresUserInteraction` ... Hooks can tighten restrictions but not loosen them past what permission rules allow."

This confirms that denied/disallowed tools are structurally unavailable, not merely permission-checked.

---

## 4. Permission Modes: default, acceptEdits, plan, bypassPermissions, auto

### Default permission mode

`default` = Manual mode. Claude Code asks before running tools that need permission.

### bypassPermissions mode behavior

**Exists when enabled via:**
- `--dangerously-skip-permissions` CLI flag
- `--permission-mode bypassPermissions` CLI flag
- `--allow-dangerously-skip-permissions`
- `permissions.defaultMode: "bypassPermissions"` in user, `--settings`, or managed settings

From settings.md:
> "`bypassPermissions` only applies if you started the session with bypass mode already available: `--dangerously-skip-permissions`, `--permission-mode bypassPermissions`, `--allow-dangerously-skip-permissions`, or `permissions.defaultMode: "bypassPermissions"` in [user, `--settings`, or managed settings]. It doesn't apply if bypass mode is disabled by [`permissions.disableBypassPermissionsMode`], or if you started the session in [restricted mode]."

### (a) Do deny rules apply under bypassPermissions?

**YES, with important caveats.** From hooks-guide.md (PermissionRequest hook section):

> "`bypassPermissions` only applies if you started the session with bypass mode already available: ... It doesn't apply if bypass mode is disabled by [`permissions.disableBypassPermissionsMode`], or if you started the session in [restricted mode]. Claude Code never saves it as `defaultMode`."

And from the managed settings section:
> "A managed `model` sets the model each session starts with, and you can still switch with `/model`; the lock is [`availableModels`], which constrains `/model`, `--model`, and the `model` key in your own files."

**Deny rules in settings STILL APPLY under bypassPermissions.** The mode only bypasses permission prompts, not structural denials.

### (b) Do PreToolUse hooks run and can they block under bypassPermissions?

**YES, they run and YES, they can block.** From hooks-guide.md:

> "`PreToolUse` hooks fire before any permission-mode check, in every [permission mode], including `dontAsk`. A hook that returns `permissionDecision: "deny"` blocks the tool even in `bypassPermissions` mode or with `--dangerously-skip-permissions`. This lets you enforce policy that users can't bypass by changing their permission mode."

This is a critical quote: **hooks can enforce policy that even bypassPermissions cannot bypass.**

### (c) Do disallowed tools remain removed under bypassPermissions?

**YES.** Since disallowed tools are removed from context (not merely denied), they remain unavailable in bypassPermissions mode.

### Other permission modes

- `acceptEdits`: Auto-accept file edits; manual mode for other actions
- `auto`: Background classifier reviews tool calls; user doesn't see prompts
- `dontAsk`: Auto-deny all prompts
- `plan`: Read-only exploration mode

---

## 5. Hooks: Input Schema, Exit Codes, Configuration, Environment

### PreToolUse Hook Input JSON Schema

From hooks.md (PreToolUse Hook Reference):

```json
{
  "session_id": "abc123",
  "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/home/user/.claude/projects/.../transcript.jsonl",
  "cwd": "/home/user/my-project",
  "scratchpad_dir": "/tmp/claude-1000/-home-user-my-project/abc123/scratchpad",
  "permission_mode": "default",
  "effort": { "level": "medium" },
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "npm test" },
  "tool_use_id": "toolu_01ABC123...",
  "agent_type": "general-purpose",
  "agent_id": "subagent_12345"
}
```

**Key fields for agent identification:**
- `agent_type`: Present when running as a subagent (e.g., `"Explore"`, `"security-reviewer"`, custom agent names, or `"general-purpose"` for main thread)
- `agent_id`: Unique subagent identifier when hook fires inside a subagent
- `permission_mode`: Current mode (`"default"`, `"plan"`, `"acceptEdits"`, `"auto"`, `"dontAsk"`, `"bypassPermissions"`)

### Exit Code Semantics

| Exit Code | Behavior |
|-----------|----------|
| **0** | Success; reads JSON output for decisions. No decision means normal flow |
| **2** | **Blocks the tool call**. Use stderr as the blocking reason |
| **Other** | Non-blocking error; tool call proceeds |

### Hook Output JSON Structure for permissionDecision

From hooks.md:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Reason shown to user",
    "updatedInput": { "command": "safer-version" }
  }
}
```

**permissionDecision values:**
- `"allow"`: Skip the permission prompt
- `"deny"`: Cancel the tool call
- `"ask"`: Show the permission prompt (normal behavior)
- `"defer"` (non-interactive mode only): Exit with tool call preserved for Agent SDK wrapper

### Hook Configuration Locations

From hooks-guide.md:

| Location | Scope | Shareable |
|----------|-------|-----------|
| `~/.claude/settings.json` | All your projects | No, local to machine |
| `.claude/settings.json` | Single project | Yes, commit to repo |
| `.claude/settings.local.json` | Single project | No, gitignored |
| Managed policy settings | Organization-wide | Yes, admin-controlled |
| Plugin `hooks/hooks.json` | When plugin enabled | Yes, bundled with plugin |
| Skill frontmatter | Rest of session | Yes, in skill file |
| Subagent frontmatter | While that subagent runs | Yes, in agent file |

**Agent frontmatter hooks:** Plugin agents cannot define hooks (ignored). Custom agents can, but only while that specific agent runs.

### Environment Variables Available to Hooks

From hooks-guide.md (block-files example):

```bash
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
```

**Available in hook input JSON:**
- `cwd`: Working directory
- `session_id`: Session identifier
- `permission_mode`: Current permission mode
- `agent_type`: Agent name/type
- `agent_id`: Subagent ID

**Available as environment variables to hook processes:**
- `CLAUDE_PROJECT_DIR`: Project root
- `CLAUDE_PLUGIN_ROOT`: Plugin install directory (for plugin hooks)
- `CLAUDE_PLUGIN_DATA`: Plugin persistent data directory
- `CLAUDE_PLUGIN_OPTION_<KEY>`: Plugin user config values
- Standard `$VAR` from system environment

### Agent Identification in Hook Input

**YES, agent information is present:**
- `agent_type`: String naming the agent (e.g., `"general-purpose"`, `"Explore"`, `"my-plugin:security-reviewer"`)
- `agent_id`: Unique identifier for subagent instances

When running in the main thread via `--agent`, `agent_type` will be the agent name and `agent_id` identifies that run.

---

## 6. Subagents: Inheritance of Hooks and Bypass Mode

### Do subagents inherit PreToolUse hooks from settings?

**YES.** From sub-agents.md:

> "Hooks defined in `settings.json` fire inside subagents: `PreToolUse` / `PostToolUse` fire for subagent tool calls; `SubagentStart` / `SubagentStop` fire at subagent lifecycle events."

### Do subagents inherit bypassPermissions mode?

**CONDITIONAL.** From sub-agents.md:

> "The main conversation's permission mode overrides the subagent's setting: **Main in `bypassPermissions`, `acceptEdits`, or `auto`**: Subagent runs in that same mode, regardless of its `permissionMode` setting. **Main in `default`, `dontAsk`, or `plan`**: Subagent runs in its specified `permissionMode` (except `bypassPermissions` is not allowed—it reverts to the main conversation's mode)."

**Key point:** A subagent cannot elevate permissions above the main conversation's level.

---

## 7. Skill Loading and /skill-name invocation

### Where skills are discovered

From skills.md:

| Location | Loads In |
|----------|----------|
| `~/.claude/skills/<skill-name>/SKILL.md` | All your projects |
| `.claude/skills/<skill-name>/SKILL.md` | This repository |
| Managed settings directory | Organization-wide |
| Nested `<subdir>/.claude/skills/<skill-name>/SKILL.md` | Sessions in/below that directory |
| Plugin `<plugin>/skills/<skill-name>/SKILL.md` | Via plugin system |
| Claude.ai account (synced) | Cowork, cloud, and terminal (signed in) |

### How /skill-name or /plugin:skill is handled

From skills.md:

> "After you load the plugin, `/my-plugin:review` runs the skill. The command name and who can invoke it follow these rules: **Command name**: `/<plugin>:<directory>`, so `skills/review/SKILL.md` in `my-plugin` is `/my-plugin:review`."

Skills are invoked directly by the user as `/<skill-name>` or automatically by Claude when the skill's description matches the task.

### Can plugin skills be invoked without the plugin: prefix?

**NO, not directly.** Plugin skills are namespaced as `/<plugin>:<skill>`. However, Claude can automatically invoke them if their description matches the task. Non-plugin skills (project/user/nested) can be invoked as `/<skill-name>` without a prefix.

---

## 8. Plugins: Capabilities and Scope

### What can a plugin ship?

From plugins/components.md:

Plugins can ship:
- **Skills**: `.claude-plugin/skills/<skill>/SKILL.md`
- **Agents (subagents)**: `.claude-plugin/agents/<name>.md`
- **Hooks**: `.claude-plugin/hooks/hooks.json`
- **Settings**: `.claude-plugin/settings.json` (only `agent` and `subagentStatusLine` keys apply)
- **MCP servers**: `.claude-plugin/.mcp.json` or `.mcp.json`
- **LSP servers**: `.claude-plugin/.lsp.json`
- **Commands**: `.claude-plugin/commands/<file>.md` (older format)
- **Output styles**: `.claude-plugin/output-styles/<name>.md`
- **Themes**: `.claude-plugin/themes/<name>.json`
- **Executables**: `.claude-plugin/bin/<name>`
- **Monitors**: `.claude-plugin/monitors/monitors.json`
- **Workflows**: `.claude-plugin/workflows/*.js`

### Can plugins be installed at project scope?

**NO, not directly via committed files.** From the manifest reference:

> "Plugins are discovered from: **Marketplace**: installed from `/plugin install`; **User scope**: `~/.claude/plugins/`; **Project scope**: NOT supported; plugins in `.claude/` directories are not recognized."

However, a project's `.claude/settings.json` can declare `enabledPlugins` or `extraKnownMarketplaces` to enable plugins installed globally.

### Can plugin agents be used with claude --agent plugin:name?

**YES.** Plugin agents can be invoked via `--agent`. From plugins/components.md:

> "This agent is named `my-plugin:security-reviewer`, and the user can [invoke it explicitly] with `@agent-my-plugin:security-reviewer`."

The format is `--agent my-plugin:agent-name`.

### Can plugin agents define hooks or permissionMode?

**NO.** From plugins/components.md:

> "**Ignored fields**: `permissionMode`, `hooks`, `mcpServers`, and `initialPrompt`. An agent file can't add hooks or MCP servers on its own, so add those as plugin [hooks](#hooks) and [MCP servers](#mcp-servers) instead."

Plugin agents cannot define hooks or permission mode in their frontmatter. These must be defined at the plugin level in `hooks/hooks.json` and `settings.json`.

---

## 9. Settings Precedence and disableBypassPermissionsMode

### Settings precedence (highest to lowest)

From settings.md:

1. **Managed settings** (managed-settings.json, MDM, or claude.ai console)
2. **Command line** (`claude --settings`, flags like `--model`)
3. **Project local** (`.claude/settings.local.json`)
4. **Shared project** (`.claude/settings.json`)
5. **User** (`~/.claude/settings.json`)

A key at a higher level overrides the same key at lower levels.

### disableBypassPermissionsMode

From settings-reference.md:

> "`permissions.disableBypassPermissionsMode`: When true (managed settings only), the `bypassPermissions` mode is not available in the permission cycle, and flags like `--dangerously-skip-permissions` fail."

This is a **managed setting only** — it cannot be set in user, project, or local files. It removes bypass mode entirely for the organization.

### Managed settings

From settings.md and managed-settings.md:

Managed settings are deployed by an organization through:
- `managed-settings.json` file
- MDM (Mobile Device Management) policies
- [Server-managed settings](https://code.claude.com/docs/en/server-managed-settings.md) from the claude.ai admin console
- Embedding host via SDK `managedSettings` option

Managed settings cannot be overridden by user, project, or command-line settings, except for a few [security-sensitive keys](#exceptions-to-managed-settings-precedence) that honor restrictive values from any scope.

---

## Summary: Key Yes/No Answers

**(a) Do PreToolUse hooks run & block under bypassPermissions?**

**YES, definitively.** Hooks run in all permission modes including bypassPermissions, and a hook that returns `permissionDecision: "deny"` or exits with code 2 blocks the tool even in bypassPermissions mode. This enforces policy that users cannot bypass.

**(b) Do disallowedTools/deny rules hold under bypass?**

**YES.** Disallowed tools are removed from the model's context and remain unavailable. Deny rules still apply. The bypass mode only skips permission prompts; it does not loosen structural restrictions.

**(c) Do agent-frontmatter hooks fire for main-thread --agent?**

**PARTIAL/NO.** Custom agent frontmatter can declare `hooks`, but they only fire while that agent is running. Plugin agent frontmatter cannot declare hooks at all (they're ignored). For agent-level hook enforcement on main-thread --agent, define hooks in `.claude/settings.json` or managed settings instead.

**(d) Do plugin agents support hooks/permissionMode in frontmatter?**

**NO.** Plugin agent frontmatter ignores `hooks`, `mcpServers`, `permissionMode`, and `initialPrompt`. Define these at the plugin level in `hooks/hooks.json` and `settings.json`.

**(e) What identifies the agent in hook input?**

**agent_type** and **agent_id** fields:
- `agent_type`: String name of the agent (e.g., `"general-purpose"`, `"Explore"`, `"my-plugin:security-reviewer"`)
- `agent_id`: Unique identifier for that subagent instance
- For main-thread `--agent`, `agent_type` will be the agent name

---

## Documentation URLs

- https://code.claude.com/docs/en/agents.md
- https://code.claude.com/docs/en/sub-agents.md
- https://code.claude.com/docs/en/hooks-guide.md
- https://code.claude.com/docs/en/hooks.md
- https://code.claude.com/docs/en/settings.md
- https://code.claude.com/docs/en/settings-reference.md
- https://code.claude.com/docs/en/permissions.md
- https://code.claude.com/docs/en/cli-reference.md
- https://code.claude.com/docs/en/skills.md
- https://code.claude.com/docs/en/plugins/components.md
- https://code.claude.com/docs/en/plugins/manifest-reference.md
- https://code.claude.com/docs/en/managed-settings.md
