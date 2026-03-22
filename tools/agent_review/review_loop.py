from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gemini_review import resolve_gemini_binary, resolve_repo_root, write_json, write_text


@dataclass
class CommandResult:
    command: list[str]
    command_text: str
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool


class _FormatMap(dict):
    def __missing__(self, key: str) -> str:
        return ""


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def read_task_text(task: str | None, task_file: str | None, *, cwd: Path, profile: str) -> str:
    if task and task_file:
        raise ValueError("use either --task or --task-file, not both")
    if task_file:
        task_path = Path(task_file)
        if not task_path.is_absolute():
            task_path = (cwd / task_path).resolve()
        return task_path.read_text(encoding="utf-8").strip()
    if task:
        return task.strip()
    if profile == "research":
        return (
            "Review the provided research update as a skeptical reviewer. "
            "Focus on novelty risk, weak evidence, missing controls, evaluation gaps, and the minimum fixes required."
        )
    if profile == "generic":
        return (
            "Review the provided context as a skeptical external reviewer. "
            "Focus on the biggest risks, unsupported assumptions, and the minimum fixes required."
        )
    return (
        "Review the provided repository context as a skeptical senior engineer. "
        "Focus on bugs, regressions, unsafe assumptions, missing tests, and code-doc mismatch."
    )


def resolve_output_dir(output_dir: str | None, *, cwd: Path, repo_root: Path | None) -> Path:
    if output_dir:
        raw = Path(output_dir)
        resolved = raw.resolve() if raw.is_absolute() else (cwd / raw).resolve()
    else:
        base = repo_root or cwd
        resolved = (base / "outputs" / f"agent_review_loop_{now_stamp()}").resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_executor_command(command: str | None, command_file: str | None, *, cwd: Path) -> str:
    if command and command_file:
        raise ValueError("use either --executor-cmd or --executor-cmd-file, not both")
    if command_file:
        path = Path(command_file)
        if not path.is_absolute():
            path = (cwd / path).resolve()
        return path.read_text(encoding="utf-8")
    if command:
        return command
    raise ValueError("either --executor-cmd or --executor-cmd-file is required")


def normalize_paths(items: list[str], *, cwd: Path) -> list[Path]:
    resolved: list[Path] = []
    for item in items:
        path = Path(item)
        if not path.is_absolute():
            path = (cwd / path).resolve()
        else:
            path = path.resolve()
        resolved.append(path)
    return resolved


def pick_mock_response(mock_files: list[Path], round_index: int) -> Path | None:
    if not mock_files:
        return None
    if round_index <= len(mock_files):
        return mock_files[round_index - 1]
    return mock_files[-1]


def build_executor_instructions(
    *,
    base_task: str,
    focus: list[str],
    round_index: int,
    max_rounds: int,
    cwd: Path,
    previous_review: dict[str, Any] | None,
    previous_review_json: Path | None,
    previous_review_md: Path | None,
) -> str:
    lines = [
        "# Agent Executor Instructions",
        "",
        f"- Round: {round_index}/{max_rounds}",
        f"- Working directory: {cwd}",
        "",
        "## Base Task",
        base_task.strip(),
        "",
        "## Required Behavior",
        "- Make the minimum useful repository changes to address the review feedback.",
        "- Prefer concrete fixes over explanation-only output.",
        "- Assume a fresh review pass will run after you finish.",
        "- Do not spend effort on self-review prose inside the executor step.",
        "",
        "## Focus Areas",
    ]
    if focus:
        lines.extend(f"- {item}" for item in focus)
    else:
        lines.append("- none provided")

    lines.extend(["", "## Previous Review"])
    if not previous_review:
        lines.extend(
            [
                "No previous review exists yet. This is the first executor round.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"- Previous decision: {previous_review.get('decision')}",
                f"- Previous score: {previous_review.get('score')}",
                f"- Previous summary: {str(previous_review.get('summary', '')).strip()}",
                "",
                "### Previous Open Issues",
            ]
        )
        issues = previous_review.get("critical_issues") or []
        if issues:
            for issue in issues:
                lines.append(
                    f"- {issue.get('id', 'ISSUE')} | {issue.get('severity', 'unknown')} | "
                    f"{issue.get('title', '')}: {issue.get('minimum_fix', '')}"
                )
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "### Previous Review Files",
                f"- JSON: {previous_review_json or '[none]'}",
                f"- Markdown: {previous_review_md or '[none]'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_review_task(
    *,
    base_task: str,
    round_index: int,
    max_rounds: int,
    previous_review: dict[str, Any] | None,
) -> str:
    lines = [
        f"Re-review the repository after executor round {round_index}/{max_rounds}.",
        "",
        "Base objective:",
        base_task.strip(),
        "",
        "Your job in this round:",
        "- Judge the current repository state, not the executor intent.",
        "- Check whether previously identified issues were resolved.",
        "- Call out any new regressions or unsupported claims introduced in this round.",
        "- Keep the review decision strict.",
    ]
    if previous_review:
        lines.extend(
            [
                "",
                "Previous review context:",
                f"- Previous decision: {previous_review.get('decision')}",
                f"- Previous summary: {str(previous_review.get('summary', '')).strip()}",
                "",
                "Previously open issues:",
            ]
        )
        issues = previous_review.get("critical_issues") or []
        if issues:
            for issue in issues:
                lines.append(
                    f"- {issue.get('id', 'ISSUE')} | {issue.get('severity', 'unknown')} | "
                    f"{issue.get('title', '')}: {issue.get('why_it_matters', '')}"
                )
        else:
            lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def capture_git_snapshot(*, cwd: Path, output_dir: Path, prefix: str) -> None:
    commands = {
        f"{prefix}_git_status.txt": ["git", "status", "--short"],
        f"{prefix}_git_diff_names.txt": ["git", "diff", "--name-only", "--", "."],
        f"{prefix}_git_diff_stat.txt": ["git", "diff", "--stat", "--", "."],
    }
    for filename, command in commands.items():
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            text = completed.stdout
        else:
            text = (
                f"command failed: {' '.join(command)}\n"
                f"returncode: {completed.returncode}\n"
                f"stderr:\n{completed.stderr}"
            )
        write_text(output_dir / filename, text)


def run_bash_command(
    *,
    command_text: str,
    cwd: Path,
    env: dict[str, str],
    timeout_sec: int,
) -> CommandResult:
    command = ["bash", "-lc", command_text]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
        timed_out = False
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    duration_sec = time.monotonic() - started
    return CommandResult(
        command=command,
        command_text=command_text,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_sec=duration_sec,
        timed_out=timed_out,
    )


def run_command(
    *,
    command: list[str],
    cwd: Path,
    timeout_sec: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
        timed_out = False
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    duration_sec = time.monotonic() - started
    return CommandResult(
        command=command,
        command_text=" ".join(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_sec=duration_sec,
        timed_out=timed_out,
    )


def should_stop(decision: str | None, policy: str) -> bool:
    normalized = (decision or "").strip().lower()
    if policy == "accept":
        return normalized == "accept"
    if policy == "accept-or-reject":
        return normalized in {"accept", "reject"}
    return False


def render_loop_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Agent Review Loop Summary",
        "",
        f"- Status: {summary.get('status')}",
        f"- Stop reason: {summary.get('stop_reason')}",
        f"- Completed rounds: {summary.get('completed_rounds')}",
        f"- Max rounds: {summary.get('max_rounds')}",
        f"- Output dir: {summary.get('output_dir')}",
        "",
        "## Rounds",
    ]
    rounds = summary.get("rounds") or []
    if not rounds:
        lines.append("- none")
    else:
        for round_data in rounds:
            review = round_data.get("review", {})
            executor = round_data.get("executor", {})
            lines.append(
                f"- Round {round_data.get('round_index')}: executor_rc={executor.get('returncode')} "
                f"review_status={review.get('status')} decision={review.get('decision')}"
            )
    lines.append("")
    return "\n".join(lines)


def run_agent_review_loop(
    *,
    project_root: Path,
    cwd: Path,
    output_dir: Path,
    profile: str,
    base_task: str,
    focus: list[str],
    include_files: list[Path],
    git_diff_mode: str,
    git_base_ref: str | None,
    max_diff_chars: int,
    max_file_chars: int,
    gemini_bin: str | None,
    review_backend: str,
    model: str | None,
    gemini_extra_args: list[str],
    max_rounds: int,
    stop_on_decision: str,
    executor_command_text: str,
    executor_timeout_sec: int,
    executor_allow_nonzero: bool,
    review_timeout_sec: int,
    review_mock_response_files: list[Path],
) -> dict[str, Any]:
    repo_root = resolve_repo_root(cwd)
    resolved_gemini_bin = resolve_gemini_binary(gemini_bin, cwd=cwd, repo_root=repo_root)

    loop_config: dict[str, Any] = {
        "cwd": str(cwd),
        "repo_root": None if repo_root is None else str(repo_root),
        "output_dir": str(output_dir),
        "profile": profile,
        "base_task": base_task,
        "focus": focus,
        "include_files": [str(path) for path in include_files],
        "git_diff_mode": git_diff_mode,
        "git_base_ref": git_base_ref,
        "max_diff_chars": max_diff_chars,
        "max_file_chars": max_file_chars,
        "gemini_bin": resolved_gemini_bin,
        "review_backend": review_backend,
        "model": model,
        "gemini_extra_args": gemini_extra_args,
        "max_rounds": max_rounds,
        "stop_on_decision": stop_on_decision,
        "executor_command_text": executor_command_text,
        "executor_timeout_sec": executor_timeout_sec,
        "executor_allow_nonzero": executor_allow_nonzero,
        "review_timeout_sec": review_timeout_sec,
        "review_mock_response_files": [str(path) for path in review_mock_response_files],
    }
    write_json(output_dir / "loop_config.json", loop_config)

    loop_summary: dict[str, Any] = {
        "status": "running",
        "stop_reason": None,
        "completed_rounds": 0,
        "max_rounds": max_rounds,
        "cwd": str(cwd),
        "repo_root": None if repo_root is None else str(repo_root),
        "output_dir": str(output_dir),
        "profile": profile,
        "gemini_bin": resolved_gemini_bin,
        "review_backend": review_backend,
        "model": model,
        "git_diff_mode": git_diff_mode,
        "rounds": [],
    }

    previous_review: dict[str, Any] | None = None
    previous_review_json: Path | None = None
    previous_review_md: Path | None = None

    for round_index in range(1, max_rounds + 1):
        round_dir = output_dir / f"round_{round_index:02d}"
        executor_dir = round_dir / "executor"
        review_dir = round_dir / "review"
        executor_dir.mkdir(parents=True, exist_ok=True)
        review_dir.mkdir(parents=True, exist_ok=True)

        executor_instructions = build_executor_instructions(
            base_task=base_task,
            focus=focus,
            round_index=round_index,
            max_rounds=max_rounds,
            cwd=cwd,
            previous_review=previous_review,
            previous_review_json=previous_review_json,
            previous_review_md=previous_review_md,
        )
        executor_context = {
            "round_index": round_index,
            "max_rounds": max_rounds,
            "cwd": str(cwd),
            "repo_root": None if repo_root is None else str(repo_root),
            "base_task": base_task,
            "focus": focus,
            "previous_review_json": None if previous_review_json is None else str(previous_review_json),
            "previous_review_md": None if previous_review_md is None else str(previous_review_md),
            "previous_review": previous_review,
        }
        executor_task_file = executor_dir / "executor_instructions.md"
        executor_context_file = executor_dir / "executor_context.json"
        write_text(executor_task_file, executor_instructions)
        write_json(executor_context_file, executor_context)
        capture_git_snapshot(cwd=cwd, output_dir=executor_dir, prefix="before")

        env = os.environ.copy()
        env.update(
            {
                "AGENT_REVIEW_LOOP_DIR": str(output_dir),
                "AGENT_REVIEW_ROUND_DIR": str(round_dir),
                "AGENT_REVIEW_ROUND_INDEX": str(round_index),
                "AGENT_REVIEW_EXECUTOR_DIR": str(executor_dir),
                "AGENT_REVIEW_DIR": str(review_dir),
                "AGENT_REVIEW_EXECUTOR_TASK_FILE": str(executor_task_file),
                "AGENT_REVIEW_EXECUTOR_CONTEXT_FILE": str(executor_context_file),
                "AGENT_REVIEW_PREVIOUS_JSON": "" if previous_review_json is None else str(previous_review_json),
                "AGENT_REVIEW_PREVIOUS_MD": "" if previous_review_md is None else str(previous_review_md),
            }
        )
        format_vars = _FormatMap(
            {
                "loop_dir": str(output_dir),
                "round_dir": str(round_dir),
                "round_index": str(round_index),
                "executor_dir": str(executor_dir),
                "review_dir": str(review_dir),
                "executor_task_file": str(executor_task_file),
                "executor_context_file": str(executor_context_file),
                "previous_review_json": "" if previous_review_json is None else str(previous_review_json),
                "previous_review_md": "" if previous_review_md is None else str(previous_review_md),
                "cwd": str(cwd),
                "repo_root": "" if repo_root is None else str(repo_root),
            }
        )
        expanded_executor_command = executor_command_text.format_map(format_vars)
        write_text(executor_dir / "executor_command.sh", expanded_executor_command)
        executor_result = run_bash_command(
            command_text=expanded_executor_command,
            cwd=cwd,
            env=env,
            timeout_sec=executor_timeout_sec,
        )
        write_text(executor_dir / "executor_stdout.txt", executor_result.stdout)
        write_text(executor_dir / "executor_stderr.txt", executor_result.stderr)
        write_json(
            executor_dir / "executor_metadata.json",
            {
                "command": executor_result.command,
                "command_text": executor_result.command_text,
                "returncode": executor_result.returncode,
                "duration_sec": executor_result.duration_sec,
                "timed_out": executor_result.timed_out,
            },
        )
        capture_git_snapshot(cwd=cwd, output_dir=executor_dir, prefix="after")

        round_summary: dict[str, Any] = {
            "round_index": round_index,
            "executor": {
                "command": executor_result.command,
                "command_text": executor_result.command_text,
                "returncode": executor_result.returncode,
                "duration_sec": executor_result.duration_sec,
                "timed_out": executor_result.timed_out,
                "output_dir": str(executor_dir),
            },
        }

        if executor_result.returncode != 0 and not executor_allow_nonzero:
            round_summary["review"] = {
                "status": "skipped",
                "reason": "executor_failed",
                "output_dir": str(review_dir),
            }
            loop_summary["rounds"].append(round_summary)
            loop_summary["status"] = "failed"
            loop_summary["stop_reason"] = "executor_failed"
            loop_summary["completed_rounds"] = round_index
            write_json(round_dir / "round_summary.json", round_summary)
            write_json(output_dir / "loop_summary.json", loop_summary)
            write_text(output_dir / "loop_summary.md", render_loop_summary(loop_summary))
            return loop_summary

        review_task_text = build_review_task(
            base_task=base_task,
            round_index=round_index,
            max_rounds=max_rounds,
            previous_review=previous_review,
        )
        review_task_file = review_dir / "review_task.md"
        write_text(review_task_file, review_task_text)

        review_command = [
            sys.executable,
            str((project_root / "tools" / "run_gemini_review.py").resolve()),
            "--profile",
            profile,
            "--task-file",
            str(review_task_file),
            "--git-diff-mode",
            git_diff_mode,
            "--max-diff-chars",
            str(max_diff_chars),
            "--max-file-chars",
            str(max_file_chars),
            "--cwd",
            str(cwd),
            "--output-dir",
            str(review_dir),
            "--backend",
            review_backend,
            "--gemini-bin",
            resolved_gemini_bin,
            "--timeout-sec",
            str(review_timeout_sec),
        ]
        if git_base_ref:
            review_command.extend(["--git-base-ref", git_base_ref])
        if model:
            review_command.extend(["--model", model])
        for item in focus:
            review_command.extend(["--focus", item])
        for path in include_files:
            review_command.extend(["--include-file", str(path)])
        for item in gemini_extra_args:
            review_command.extend(["--gemini-extra-arg", item])
        mock_response = pick_mock_response(review_mock_response_files, round_index)
        if mock_response is not None:
            review_command.extend(["--mock-response-file", str(mock_response)])

        review_result = run_command(
            command=review_command,
            cwd=cwd,
            timeout_sec=review_timeout_sec + 30,
        )
        write_text(review_dir / "loop_driver_stdout.txt", review_result.stdout)
        write_text(review_dir / "loop_driver_stderr.txt", review_result.stderr)
        write_json(
            review_dir / "loop_driver_metadata.json",
            {
                "command": review_result.command,
                "command_text": review_result.command_text,
                "returncode": review_result.returncode,
                "duration_sec": review_result.duration_sec,
                "timed_out": review_result.timed_out,
            },
        )

        review_summary: dict[str, Any] = {
            "status": "success" if review_result.returncode == 0 else "failed",
            "returncode": review_result.returncode,
            "duration_sec": review_result.duration_sec,
            "timed_out": review_result.timed_out,
            "output_dir": str(review_dir),
            "decision": None,
            "score": None,
            "issues": None,
        }

        review_json_path = review_dir / "review.json"
        if review_json_path.exists():
            review_payload = json.loads(review_json_path.read_text(encoding="utf-8"))
            previous_review = review_payload
            previous_review_json = review_json_path
            previous_review_md = review_dir / "review.md"
            review_summary["decision"] = review_payload.get("decision")
            review_summary["score"] = review_payload.get("score")
            review_summary["issues"] = len(review_payload.get("critical_issues") or [])
        else:
            previous_review = None
            previous_review_json = None
            previous_review_md = None
            metadata_path = review_dir / "run_metadata.json"
            if metadata_path.exists():
                metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                review_summary["error"] = metadata_payload.get("error")

        round_summary["review"] = review_summary
        loop_summary["rounds"].append(round_summary)
        loop_summary["completed_rounds"] = round_index

        if review_result.returncode != 0:
            loop_summary["status"] = "failed"
            loop_summary["stop_reason"] = "review_failed"
            write_json(round_dir / "round_summary.json", round_summary)
            write_json(output_dir / "loop_summary.json", loop_summary)
            write_text(output_dir / "loop_summary.md", render_loop_summary(loop_summary))
            return loop_summary

        if should_stop(str(review_summary.get("decision")), stop_on_decision):
            loop_summary["status"] = "completed"
            loop_summary["stop_reason"] = f"decision:{review_summary.get('decision')}"
            write_json(round_dir / "round_summary.json", round_summary)
            write_json(output_dir / "loop_summary.json", loop_summary)
            write_text(output_dir / "loop_summary.md", render_loop_summary(loop_summary))
            return loop_summary

        write_json(round_dir / "round_summary.json", round_summary)

    loop_summary["status"] = "completed"
    loop_summary["stop_reason"] = "max_rounds_reached"
    write_json(output_dir / "loop_summary.json", loop_summary)
    write_text(output_dir / "loop_summary.md", render_loop_summary(loop_summary))
    return loop_summary
