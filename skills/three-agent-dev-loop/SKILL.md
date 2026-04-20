---
name: three-agent-dev-loop
description: "Orchestrate a three-subagent coding workflow for software changes: one subagent implements the requirement and runs light self-checks, a second subagent writes or updates targeted automated tests and executes them, and a third subagent summarizes the final implementation, code locations, and test method. Use when the user asks for a repeatable implement-test-fix loop, wants implementation and test creation split across separate agents, or explicitly asks for multiple collaborating subagents."
---

# Three Agent Dev Loop

## Overview

Use this skill when the user wants true role separation instead of a single-pass edit. Two implementations are provided:

- **`scripts/run_three_agent_loop.py`** — uses `codex exec` for Codex environments
- **`scripts/run_three_agent_loop_claude.py`** — uses `claude code --print` for Claude Code environments

Both run separate agent passes for the implementer, tester, and summarizer, keeping each role focused and making the test-feedback loop explicit.

## Workflow

1. Collect the user's requirement and the target workspace.
2. Run `scripts/run_three_agent_loop.py` to launch three role-specific Codex passes.
3. Let the implementer change product code and run only lightweight self-validation.
4. Let the tester add or update targeted tests and execute them.
5. If the tests fail, feed the tester's failure summary back into the next implementer round.
6. Stop only when tests pass, the loop hits its round limit, or a role reports a blocker.
7. Let the summarizer produce the final implementation summary, code locations, and test method.

## Guardrails

- Keep the implementer focused on feature code, bug fixes, and minimal self-checks.
- Keep the tester focused on automated tests, targeted execution, and actionable failure reports.
- Do not allow the workflow to end after implementation alone; the tester must always run.
- Prefer the narrowest useful validation command instead of full-repository test runs.
- Preserve unrelated user changes and avoid rewriting code outside the stated requirement.
- Treat tester feedback as the top-priority defect list for the next implementer round.

## Script Usage

### Claude Code (this environment)

```bash
python3 scripts/run_three_agent_loop_claude.py \
  --requirement "Implement the requested change and keep looping until tests pass."
```

The `--workspace` flag defaults to the current directory (`.`). To override:
```bash
python3 scripts/run_three_agent_loop_claude.py \
  --workspace /path/to/repo \
  --requirement "..."
```

Useful flags:
- `--max-rounds N` — cap the implement-test loop (default: 3)
- `--artifact-dir DIR` — keep prompts, outputs, stdout, and stderr for every role call
- `--json` — print the final aggregate result as JSON
- `--verbose` — print each `claude code -p` command before it runs

### Codex

```bash
python3 scripts/run_three_agent_loop.py \
  --requirement "Implement the requested change and keep looping until tests pass."
```

The `--workspace` flag defaults to the current directory (`.`). To override:
```bash
python3 scripts/run_three_agent_loop.py \
  --workspace /path/to/repo \
  --requirement "..."
```

Useful flags:
- `--max-rounds N` — cap the implement-test loop
- `--artifact-dir DIR` — keep prompts, outputs, stdout, and stderr for every role call
- `--model MODEL` — force a specific model for all subagents
- `--add-dir DIR` — grant extra writable directories to child Codex runs
- `--unsafe-codex-auto` — pass `--dangerously-bypass-approvals-and-sandbox` to child `codex exec` calls. Use only in a trusted environment when unattended execution matters more than guardrails.
- `--json` — print the final aggregate result as JSON

## Manual Fallback

If the script cannot be used, preserve the same role boundaries manually:

1. First pass: implement and run light self-checks.
2. Second pass: add or update targeted tests and execute them.
3. Repeat until tests pass or a blocker is explicit.
4. Final pass: summarize the implementation, files, and test method.

## Output Requirements

The final response should include:

- Whether the loop passed or stopped with a blocker.
- The actual implementation approach.
- The code locations touched or added.
- The targeted tests and commands that were run.
- Any remaining risks if the loop stopped before a clean pass.
