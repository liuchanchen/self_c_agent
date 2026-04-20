#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

# Claude Code three-agent loop using the `claude` CLI for subagent invocation.


IMPLEMENTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "iteration",
        "status",
        "change_summary",
        "files_changed",
        "verification_commands",
        "verification_results",
        "remaining_risks",
        "handoff_to_tester",
    ],
    "properties": {
        "iteration": {"type": "integer", "minimum": 1},
        "status": {"type": "string", "enum": ["implemented", "blocked"]},
        "change_summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification_commands": {"type": "array", "items": {"type": "string"}},
        "verification_results": {"type": "array", "items": {"type": "string"}},
        "remaining_risks": {"type": "string"},
        "handoff_to_tester": {"type": "string"},
    },
}

TESTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "iteration",
        "status",
        "test_files_changed",
        "test_commands",
        "failure_summary",
        "raw_error_excerpt",
        "handoff_to_implementer",
        "handoff_to_summarizer",
    ],
    "properties": {
        "iteration": {"type": "integer", "minimum": 1},
        "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
        "test_files_changed": {"type": "array", "items": {"type": "string"}},
        "test_commands": {"type": "array", "items": {"type": "string"}},
        "failure_summary": {"type": "string"},
        "raw_error_excerpt": {"type": "string"},
        "handoff_to_implementer": {"type": "string"},
        "handoff_to_summarizer": {"type": "string"},
    },
}

SUMMARIZER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "implementation_plan",
        "code_locations",
        "test_method",
        "final_verification",
        "notes",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["complete", "blocked"]},
        "implementation_plan": {"type": "string"},
        "code_locations": {"type": "array", "items": {"type": "string"}},
        "test_method": {"type": "string"},
        "final_verification": {"type": "string"},
        "notes": {"type": "string"},
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a three-pass Claude Code agent workflow: implement, test, then summarize. "
            "The tester feeds failures back into the implementer until tests pass "
            "or the round limit is reached."
        )
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Target workspace for the agent runs. Defaults to the current directory.",
    )
    parser.add_argument(
        "--requirement",
        help="Requirement text. Omit to read from --requirement-file or stdin.",
    )
    parser.add_argument(
        "--requirement-file",
        help="Read the requirement text from a file.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="Maximum implement-test iterations before stopping.",
    )
    parser.add_argument(
        "--artifact-dir",
        help="Persist prompts, responses, stdout, and stderr under this directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final aggregate result as JSON.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each agent command before it runs.",
    )
    args = parser.parse_args()
    if args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    return args


def load_requirement(args: argparse.Namespace) -> str:
    if args.requirement:
        return args.requirement.strip()
    if args.requirement_file:
        return Path(args.requirement_file).read_text(encoding="utf-8").strip()
    if sys.stdin.isatty():
        raise SystemExit("Provide --requirement, --requirement-file, or pipe requirement text on stdin.")
    return sys.stdin.read().strip()


def ensure_workspace(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Workspace does not exist: {path}")
    if not path.is_dir():
        raise SystemExit(f"Workspace must be a directory: {path}")
    return path


def make_artifact_dir(path_text: str | None) -> tuple[Path, bool]:
    if path_text:
        path = Path(path_text).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path, True
    path = Path(tempfile.mkdtemp(prefix="three-agent-dev-loop-claude-"))
    return path, False


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def validate_schema(data: dict[str, Any], schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
    """Basic schema validation."""
    required = schema.get("required", [])
    for field in required:
        if field not in data:
            raise ValueError(
                f"{schema_name} is missing required field '{field}'. "
                f"Available fields: {list(data.keys())}"
            )

    if "status" in schema.get("properties", {}):
        allowed = schema["properties"]["status"].get("enum", [])
        if "status" in data and data["status"] not in allowed:
            raise ValueError(
                f"{schema_name} has invalid status '{data['status']}'. "
                f"Must be one of: {allowed}"
            )

    return data


def find_claude_cli() -> str:
    """Locate the claude CLI binary or bun (for npx-style invocation)."""
    # Check if the ccb/claude binary is directly available
    for name in ["claude-code-best", "ccb", "claude"]:
        path = shutil.which(name)
        if path:
            return path

    # Check known install paths
    known_paths = [
        Path.home() / ".nvm/versions/node/v24.12.0/bin/ccb",
        Path.home() / ".nvm/versions/node/v24.12.0/bin/claude-code-best",
        Path.home() / ".local/bin/ccb",
    ]
    for p in known_paths:
        if p.exists():
            return str(p)

    # Fall back to bun (works with global npm installs via bunx)
    if shutil.which("bun"):
        return "bun"

    raise SystemExit(
        "Could not find claude CLI. Install via:\n"
        "  npm install -g @anthropic-ai/claude-code\n"
        "or\n"
        "  bun install -g @anthropic-ai/claude-code"
    )


def build_claude_cmd(
    cli: str,
    schema: dict[str, Any],
    verbose: bool,
) -> list[str]:
    """Build the claude CLI command for a subagent run (prompt via stdin)."""
    if cli == "bun":
        cmd = ["bun", "x", "--bun", "-y", "claude-code-best"]
    else:
        cmd = [cli]

    cmd.extend([
        "-p",
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--json-schema", json.dumps(schema),
    ])

    if verbose:
        cmd.append("--verbose")

    return cmd


def extract_structured_output(stdout: str) -> dict[str, Any] | None:
    """Extract structured_output from claude CLI response.

    Handles two formats:
    - Simple (no tools used): single JSON object with top-level "structured_output"
    - Tool-using session: <persisted-output> wrapped JSON array; find the message
      with type="result" and extract its "structured_output"
    """
    import re

    # Strip <persisted-output> wrapper if present (tool-using sessions)
    m = re.search(r"<persisted-output>(.*?)</persisted-output>", stdout, re.DOTALL)
    if m:
        content = m.group(1).strip()
    else:
        content = stdout.strip()

    parsed = json.loads(content)

    if isinstance(parsed, list):
        # Tool-using session: find the result message
        for msg in reversed(parsed):
            if isinstance(msg, dict) and msg.get("type") == "result":
                so = msg.get("structured_output")
                if so is not None:
                    return so
        return None
    else:
        # Simple session: structured_output is at the top level
        return parsed.get("structured_output")


def agent_exec(
    *,
    role: str,
    iteration: int,
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    artifact_dir: Path,
    workspace: Path,
    verbose: bool,
) -> dict[str, Any]:
    """Run a Claude Code subagent via the claude CLI. Prompt is piped via stdin."""
    prompt_path = artifact_dir / f"{role}-iter-{iteration}.prompt.txt"
    write_text(prompt_path, prompt)

    cli = find_claude_cli()
    cmd = build_claude_cmd(cli, schema, verbose)

    if verbose:
        preview = cmd[:4] + ["[prompt via stdin]"]
        print(f"[{role} iter {iteration}] {' '.join(str(x) for x in preview)}", file=sys.stderr)

    # Filter out CLAUDE_* env vars to avoid session interference between subagent calls
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_") or k == "ANTHROPIC_API_KEY"
    }

    completed = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=str(workspace),
        check=False,
        env=env,
    )

    stdout = completed.stdout
    stderr = completed.stderr

    write_text(artifact_dir / f"{role}-iter-{iteration}.stdout.txt", stdout)
    if stderr:
        write_text(artifact_dir / f"{role}-iter-{iteration}.stderr.txt", stderr)

    if completed.returncode != 0:
        tail = "\n".join(
            line for line in (stdout + "\n" + stderr).splitlines()[-40:]
            if line.strip()
        )
        raise RuntimeError(
            f"{role} iteration {iteration} failed (exit {completed.returncode}):\n{tail}"
        )

    try:
        structured = extract_structured_output(stdout)
        if structured is None:
            raise RuntimeError(
                f"{role} iteration {iteration} did not return structured_output."
            )
        validated = validate_schema(structured, schema, schema_name)
        return validated
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"{role} iteration {iteration} returned invalid response: {exc}"
        ) from exc


def implementer_prompt(
    requirement: str,
    iteration: int,
    max_rounds: int,
    tester_feedback: str | None,
) -> str:
    feedback_block = tester_feedback or "No tester feedback yet."
    return textwrap.dedent(
        f"""You are the implementer subagent in a three-agent coding loop.

Your job:
- Read the workspace and implement the user's requirement.
- If this is not the first iteration, treat tester feedback as the highest-priority bug list.
- Run only lightweight self-validation that is directly relevant to the touched code.
- Preserve unrelated user changes.
- Do not stop at analysis; make the code change unless you are truly blocked.
- Do not write broad test strategy notes; a separate tester subagent will handle test creation and execution.

Requirement:
{requirement}

Current iteration: {iteration} of {max_rounds}

Tester feedback from the previous round:
{feedback_block}

Return a JSON object (the --json-schema flag enforces the schema). Fill in all required fields:
- iteration: {iteration}
- status: "implemented" if the code is ready for tester review, otherwise "blocked".
- files_changed: exact file paths you touched, one per item when possible.
- verification_commands: only commands you actually ran.
- verification_results: short results matching the commands you actually ran.
- handoff_to_tester: explain what changed and what the tester should validate.
- change_summary: brief description of what you implemented.
- remaining_risks: any risks or blockers if status is "blocked", otherwise empty string.
"""
    ).strip()


def tester_prompt(
    requirement: str,
    iteration: int,
    max_rounds: int,
    implementer_output: dict[str, Any],
) -> str:
    return textwrap.dedent(
        f"""You are the tester subagent in a three-agent coding loop.

Your job:
- Add or update targeted automated tests for the requirement if needed.
- Run the relevant test commands after the implementer has finished.
- Keep changes focused on tests, fixtures, and minimal test harness setup.
- Do not rewrite product code unless a tiny harness adjustment is absolutely required for the test to run.
- If tests fail because the implementation is wrong, stop and produce an actionable bug report for the implementer.
- If tests pass, provide a clean handoff for the summarizer.

Requirement:
{requirement}

Current iteration: {iteration} of {max_rounds}

Implementer handoff:
{json.dumps(implementer_output, indent=2)}

Return a JSON object (the --json-schema flag enforces the schema). Fill in all required fields:
- iteration: {iteration}
- status: "passed", "failed", or "blocked".
- test_files_changed: exact test or fixture paths you touched.
- test_commands: only commands you actually ran.
- failure_summary: concise and actionable when status is "failed" or "blocked"; empty string is acceptable on success.
- raw_error_excerpt: include the most useful failing output excerpt; keep it brief.
- handoff_to_implementer: the exact next-step defect list when status is "failed".
- handoff_to_summarizer: what passed and how it was validated when status is "passed".
"""
    ).strip()


def summarizer_prompt(
    requirement: str,
    implementer_rounds: list[dict[str, Any]],
    tester_rounds: list[dict[str, Any]],
    final_status: str,
) -> str:
    # Pre-serialize to avoid f-string brace-escaping issues with nested dicts
    impl_json = json.dumps({"rounds": implementer_rounds}, indent=2)
    tester_json = json.dumps({"rounds": tester_rounds}, indent=2)
    return textwrap.dedent(
        f"""You are the summarizer subagent in a three-agent coding loop.

Your job:
- Summarize what was actually implemented.
- Point to the code locations that changed.
- Describe the test method and exact commands used.
- Clearly state whether the loop passed or stopped early.
- If the loop did not pass, describe the remaining blocker without inventing fixes that were not attempted.

Requirement:
{requirement}

Final loop status before summary: {final_status}

Implementer rounds:
{impl_json}

Tester rounds:
{tester_json}

Return a JSON object (the --json-schema flag enforces the schema). Fill in all required fields:
- status: "complete" if the workflow ended with a usable summary, otherwise "blocked".
- implementation_plan: summarize the final implementation approach, not the planning process.
- code_locations: list the files or code areas that matter most.
- test_method: summarize how tests were added and run.
- final_verification: say whether tests passed, which commands proved it, and whether the loop hit its limit.
- notes: include only remaining risks, blockers, or important caveats.
"""
    ).strip()


def render_text_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        f"Final status: {result['final_status']}",
        f"Rounds used: {result['rounds_completed']}/{result['max_rounds']}",
        "",
        "Implementation",
        summary["implementation_plan"],
        "",
        "Code locations",
    ]
    for item in summary["code_locations"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "Test method",
            summary["test_method"],
            "",
            "Final verification",
            summary["final_verification"],
        ]
    )
    if summary["notes"]:
        lines.extend(["", "Notes", summary["notes"]])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    workspace = ensure_workspace(args.workspace)
    requirement = load_requirement(args)
    artifact_dir, keep_artifacts = make_artifact_dir(args.artifact_dir)

    implementer_rounds: list[dict[str, Any]] = []
    tester_rounds: list[dict[str, Any]] = []
    tester_feedback: str | None = None
    final_status = "blocked"
    failure_reason = ""

    # Save the requirement
    write_text(artifact_dir / "requirement.txt", requirement)

    try:
        for iteration in range(1, args.max_rounds + 1):
            try:
                implementer_output = agent_exec(
                    role="implementer",
                    iteration=iteration,
                    prompt=implementer_prompt(
                        requirement, iteration, args.max_rounds, tester_feedback
                    ),
                    schema=IMPLEMENTER_SCHEMA,
                    schema_name="IMPLEMENTER_SCHEMA",
                    artifact_dir=artifact_dir,
                    workspace=workspace,
                    verbose=args.verbose,
                )
            except Exception as e:
                implementer_output = {
                    "iteration": iteration,
                    "status": "blocked",
                    "change_summary": f"Implementer failed: {e}",
                    "files_changed": [],
                    "verification_commands": [],
                    "verification_results": [],
                    "remaining_risks": str(e),
                    "handoff_to_tester": "",
                }

            implementer_rounds.append(implementer_output)
            write_json(artifact_dir / f"implementer-iter-{iteration}.json", implementer_output)

            if implementer_output["status"] == "blocked":
                failure_reason = implementer_output.get("remaining_risks") or implementer_output.get("change_summary", "")
                final_status = "blocked"
                break

            try:
                tester_output = agent_exec(
                    role="tester",
                    iteration=iteration,
                    prompt=tester_prompt(
                        requirement, iteration, args.max_rounds, implementer_output
                    ),
                    schema=TESTER_SCHEMA,
                    schema_name="TESTER_SCHEMA",
                    artifact_dir=artifact_dir,
                    workspace=workspace,
                    verbose=args.verbose,
                )
            except Exception as e:
                tester_output = {
                    "iteration": iteration,
                    "status": "blocked",
                    "test_files_changed": [],
                    "test_commands": [],
                    "failure_summary": f"Tester failed: {e}",
                    "raw_error_excerpt": str(e),
                    "handoff_to_implementer": "",
                    "handoff_to_summarizer": "",
                }

            tester_rounds.append(tester_output)
            write_json(artifact_dir / f"tester-iter-{iteration}.json", tester_output)

            if tester_output["status"] == "passed":
                final_status = "passed"
                break
            if tester_output["status"] == "blocked":
                failure_reason = tester_output.get("failure_summary") or tester_output.get("raw_error_excerpt", "")
                final_status = "blocked"
                break

            tester_feedback = tester_output.get("handoff_to_implementer") or tester_output.get("failure_summary", "")
            final_status = "failed"
        else:
            failure_reason = "Reached the maximum round limit before tests passed."
            final_status = "failed"

        try:
            summarizer_output = agent_exec(
                role="summarizer",
                iteration=max(1, len(implementer_rounds)),
                prompt=summarizer_prompt(
                    requirement, implementer_rounds, tester_rounds, final_status
                ),
                schema=SUMMARIZER_SCHEMA,
                schema_name="SUMMARIZER_SCHEMA",
                artifact_dir=artifact_dir,
                workspace=workspace,
                verbose=args.verbose,
            )
        except Exception as e:
            summarizer_output = {
                "status": "blocked",
                "implementation_plan": "The summarizer could not complete.",
                "code_locations": [],
                "test_method": "No reliable test result was produced.",
                "final_verification": "The three-agent loop did not finish successfully.",
                "notes": str(e),
            }
            final_status = "blocked"

    except Exception as exc:
        summarizer_output = {
            "status": "blocked",
            "implementation_plan": "The orchestrator encountered an unexpected error.",
            "code_locations": [],
            "test_method": "No test result was produced.",
            "final_verification": f"Error: {exc}",
            "notes": str(exc),
        }
        final_status = "blocked"
        failure_reason = str(exc)

    result = {
        "requirement": requirement,
        "workspace": str(workspace),
        "final_status": final_status,
        "rounds_completed": max(len(implementer_rounds), len(tester_rounds)),
        "max_rounds": args.max_rounds,
        "failure_reason": failure_reason,
        "implementer_rounds": implementer_rounds,
        "tester_rounds": tester_rounds,
        "summary": summarizer_output,
        "artifacts_dir": str(artifact_dir),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render_text_report(result))
        if keep_artifacts:
            print(f"\nArtifacts: {artifact_dir}")

    if not keep_artifacts:
        shutil.rmtree(artifact_dir, ignore_errors=True)

    return 0 if final_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())