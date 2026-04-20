#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any


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


class CodexRunError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a three-pass Codex workflow: implement, test, then summarize. "
            "The tester feeds failures back into the implementer until tests pass "
            "or the round limit is reached."
        )
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Target workspace for the child Codex runs. Defaults to the current directory.",
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
        "--model",
        help="Optional model override passed to each child codex exec run.",
    )
    parser.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox mode for child codex exec runs when not using --unsafe-codex-auto.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Additional writable directory for child codex exec runs. Repeat as needed.",
    )
    parser.add_argument(
        "--artifact-dir",
        help="Persist prompts, responses, stdout, and stderr under this directory.",
    )
    parser.add_argument(
        "--unsafe-codex-auto",
        action="store_true",
        help=(
            "Pass --dangerously-bypass-approvals-and-sandbox to child codex exec runs. "
            "Use only in a trusted environment."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final aggregate result as JSON.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each child codex exec command before it runs.",
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
    path = Path(tempfile.mkdtemp(prefix="three-agent-dev-loop-"))
    return path, False


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def command_preview(parts: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in parts)


def codex_exec(
    *,
    role: str,
    iteration: int,
    prompt: str,
    schema: dict[str, Any],
    workspace: Path,
    artifact_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    schema_path = artifact_dir / f"{role}-schema.json"
    prompt_path = artifact_dir / f"{role}-iter-{iteration}.prompt.txt"
    output_path = artifact_dir / f"{role}-iter-{iteration}.response.json"
    stdout_path = artifact_dir / f"{role}-iter-{iteration}.stdout.txt"
    stderr_path = artifact_dir / f"{role}-iter-{iteration}.stderr.txt"

    write_json(schema_path, schema)
    write_text(prompt_path, prompt)

    cmd = [
        shutil.which("codex") or "codex",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "--color",
        "never",
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.unsafe_codex_auto:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--sandbox", args.sandbox])
        if args.sandbox == "workspace-write":
            cmd.append("--full-auto")
    for extra_dir in args.add_dir:
        cmd.extend(["--add-dir", str(Path(extra_dir).expanduser().resolve())])
    cmd.append("-")

    if args.verbose:
        print(f"[{role} iter {iteration}] {command_preview(cmd)}", file=sys.stderr)

    completed = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=workspace,
        check=False,
    )

    write_text(stdout_path, completed.stdout)
    write_text(stderr_path, completed.stderr)

    if completed.returncode != 0:
        tail = "\n".join(
            line
            for line in (completed.stdout + "\n" + completed.stderr).splitlines()[-40:]
            if line.strip()
        )
        raise CodexRunError(
            f"{role} iteration {iteration} failed with exit code {completed.returncode}.\n{tail}"
        )

    if not output_path.exists():
        raise CodexRunError(f"{role} iteration {iteration} did not produce {output_path.name}.")

    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CodexRunError(
            f"{role} iteration {iteration} returned invalid JSON in {output_path.name}: {exc}"
        ) from exc


def format_json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def implementer_prompt(
    requirement: str,
    iteration: int,
    max_rounds: int,
    tester_feedback: str | None,
) -> str:
    feedback_block = tester_feedback or "No tester feedback yet."
    return textwrap.dedent(
        f"""
        You are the implementer subagent in a three-agent coding loop.

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

        Return JSON only and obey this field intent:
        - status: "implemented" if the code is ready for tester review, otherwise "blocked".
        - files_changed: exact file paths you touched, one per item when possible.
        - verification_commands: only commands you actually ran.
        - verification_results: short results matching the commands you actually ran.
        - handoff_to_tester: explain what changed and what the tester should validate.
        """
    ).strip()


def tester_prompt(
    requirement: str,
    iteration: int,
    max_rounds: int,
    implementer_output: dict[str, Any],
) -> str:
    return textwrap.dedent(
        f"""
        You are the tester subagent in a three-agent coding loop.

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
        {format_json_block(implementer_output)}

        Return JSON only and obey this field intent:
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
    return textwrap.dedent(
        f"""
        You are the summarizer subagent in a three-agent coding loop.

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
        {format_json_block({"rounds": implementer_rounds})}

        Tester rounds:
        {format_json_block({"rounds": tester_rounds})}

        Return JSON only and obey this field intent:
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

    try:
        for iteration in range(1, args.max_rounds + 1):
            implementer_output = codex_exec(
                role="implementer",
                iteration=iteration,
                prompt=implementer_prompt(requirement, iteration, args.max_rounds, tester_feedback),
                schema=IMPLEMENTER_SCHEMA,
                workspace=workspace,
                artifact_dir=artifact_dir,
                args=args,
            )
            implementer_rounds.append(implementer_output)
            if implementer_output["status"] == "blocked":
                failure_reason = implementer_output["remaining_risks"] or implementer_output["change_summary"]
                final_status = "blocked"
                break

            tester_output = codex_exec(
                role="tester",
                iteration=iteration,
                prompt=tester_prompt(requirement, iteration, args.max_rounds, implementer_output),
                schema=TESTER_SCHEMA,
                workspace=workspace,
                artifact_dir=artifact_dir,
                args=args,
            )
            tester_rounds.append(tester_output)

            if tester_output["status"] == "passed":
                final_status = "passed"
                break
            if tester_output["status"] == "blocked":
                failure_reason = tester_output["failure_summary"] or tester_output["raw_error_excerpt"]
                final_status = "blocked"
                break

            tester_feedback = tester_output["handoff_to_implementer"] or tester_output["failure_summary"]
            final_status = "failed"
        else:
            failure_reason = "Reached the maximum round limit before tests passed."
            final_status = "failed"

        summary_output = codex_exec(
            role="summarizer",
            iteration=max(1, len(implementer_rounds)),
            prompt=summarizer_prompt(requirement, implementer_rounds, tester_rounds, final_status),
            schema=SUMMARIZER_SCHEMA,
            workspace=workspace,
            artifact_dir=artifact_dir,
            args=args,
        )
    except CodexRunError as exc:
        summary_output = {
            "status": "blocked",
            "implementation_plan": "The orchestrator could not complete the child Codex run.",
            "code_locations": [],
            "test_method": "No reliable test result was produced because one of the child Codex runs failed.",
            "final_verification": "The three-agent loop did not finish successfully.",
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
        "summary": summary_output,
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
