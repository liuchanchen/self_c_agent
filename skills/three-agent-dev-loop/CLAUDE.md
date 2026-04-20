# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a **skill repository** that provides a three-agent coding loop orchestration system. It launches separate Codex subagents in a implement → test → summarize cycle, with test failures feeding back into the implementer for a retry loop.

The core is `scripts/run_three_agent_loop.py` — a Python orchestrator that calls `codex exec` three times per round: implementer, tester, summarizer.

## Running the Loop

Two implementations are provided:

**Claude Code version** (uses `claude code -p` subagents):
```bash
python3 scripts/run_three_agent_loop_claude.py --workspace /path/to/repo \
  --requirement "Implement the requested change."

# Useful flags
--max-rounds N      # cap implement-test iterations (default: 3)
--artifact-dir DIR  # persist all prompts/outputs/stderr
--json              # output final result as JSON
--verbose           # print each agent command
```

**Codex version** (uses `codex exec` subagents):
```bash
python3 scripts/run_three_agent_loop.py --workspace /path/to/repo \
  --requirement "Implement the requested change."
```

## Architecture

- `scripts/run_three_agent_loop.py` — orchestrator; manages the loop state, invokes `codex exec` with structured prompts and JSON schemas for each role, and produces a final report
- `agents/openai.yaml` — skill interface definition (display name, default prompt hook)
- `SKILL.md` — skill metadata and usage documentation

Each subagent receives a role-specific prompt and returns a structured JSON response constrained by a schema (`IMPLEMENTER_SCHEMA`, `TESTER_SCHEMA`, `SUMMARIZER_SCHEMA`). The tester output drives the loop continuation logic — if tests fail, `handoff_to_implementer` becomes the next implementer prompt's `tester_feedback`.

The loop stops when: tests pass, the implementer reports blocked, the tester reports blocked, or max rounds are reached. The summarizer always runs last regardless of outcome.

## Sandbox Behavior

By default (`--sandbox workspace-write`), child Codex runs get `--full-auto` so they can write to the workspace without per-command prompts. `--add-dir` can grant access to additional writable directories.