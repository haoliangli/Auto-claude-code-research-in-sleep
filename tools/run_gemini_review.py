#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.agent_review import (  # noqa: E402
    DEFAULT_GEMINI_API_MODEL,
    GeminiApiInvocationError,
    PromptInputs,
    build_prompt,
    bundle_to_dict,
    collect_file_snippets,
    collect_git_diff,
    GeminiCliInvocationError,
    load_private_env_file,
    render_review_markdown,
    resolve_gemini_backend,
    resolve_gemini_binary,
    resolve_gemini_model,
    resolve_repo_root,
    run_gemini_api,
    run_gemini_cli,
    write_json,
    write_text,
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Gemini as a structured external reviewer for code or research updates."
    )
    parser.add_argument("--profile", choices=["code", "research", "generic"], default="code")
    parser.add_argument("--task", default=None, help="direct review task text")
    parser.add_argument("--task-file", default=None, help="path to a text/markdown file containing the review task")
    parser.add_argument("--focus", action="append", default=[], help="focus area; repeatable")
    parser.add_argument("--include-file", action="append", default=[], help="file to include in the review context")
    parser.add_argument(
        "--git-diff-mode",
        choices=["none", "worktree", "staged", "base"],
        default="worktree",
        help="which git diff to include",
    )
    parser.add_argument("--git-base-ref", default=None, help="required when --git-diff-mode=base")
    parser.add_argument("--max-diff-chars", type=int, default=16000)
    parser.add_argument("--max-file-chars", type=int, default=6000)
    parser.add_argument("--cwd", default=".", help="working directory for git diff and gemini execution")
    parser.add_argument("--output-dir", default=None, help="directory for request and response artifacts")
    parser.add_argument(
        "--backend",
        choices=["auto", "api", "cli"],
        default="auto",
        help="Gemini backend; auto prefers API when GEMINI_API_KEY is available",
    )
    parser.add_argument(
        "--gemini-bin",
        default=None,
        help="Gemini CLI binary; defaults to repo-local .local-tools install, then PATH",
    )
    parser.add_argument("--model", default=None, help="optional Gemini model name")
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--gemini-extra-arg", action="append", default=[], help="extra arg passed to gemini CLI")
    parser.add_argument("--dry-run", action="store_true", help="only build and save the prompt bundle")
    parser.add_argument(
        "--mock-response-file",
        default=None,
        help="path to a mock Gemini response file for offline validation; bypasses the real CLI call",
    )
    return parser.parse_args()


def default_task(profile: str) -> str:
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


def resolve_task(args: argparse.Namespace, cwd: Path) -> str:
    if args.task and args.task_file:
        raise ValueError("use either --task or --task-file, not both")
    if args.task_file:
        task_path = Path(args.task_file)
        if not task_path.is_absolute():
            task_path = (cwd / task_path).resolve()
        return task_path.read_text(encoding="utf-8").strip()
    if args.task:
        return str(args.task).strip()
    return default_task(args.profile)


def resolve_output_dir(args: argparse.Namespace, cwd: Path, repo_root: Path | None) -> Path:
    if args.output_dir:
        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = (cwd / output_dir).resolve()
        else:
            output_dir = output_dir.resolve()
    else:
        base = repo_root or cwd
        output_dir = (base / "outputs" / f"gemini_review_{now_stamp()}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def write_run_metadata(
    *,
    output_dir: Path,
    command: list[str] | None,
    cwd: Path,
    repo_root: Path | None,
    profile: str,
    model: str | None,
    gemini_bin: str,
    backend: str,
    error: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "command": command,
        "cwd": str(cwd),
        "repo_root": None if repo_root is None else str(repo_root),
        "profile": profile,
        "model": model,
        "gemini_bin": gemini_bin,
        "backend": backend,
        "output_dir": str(output_dir),
    }
    if error is not None:
        payload["error"] = error
    write_json(output_dir / "run_metadata.json", payload)


def main() -> None:
    args = parse_args()
    load_private_env_file()
    cwd = Path(args.cwd).resolve()
    repo_root = resolve_repo_root(cwd)
    gemini_bin = resolve_gemini_binary(args.gemini_bin, cwd=cwd, repo_root=repo_root)
    backend = resolve_gemini_backend(args.backend)
    model = resolve_gemini_model(args.model, backend=backend)
    task = resolve_task(args, cwd)
    output_dir = resolve_output_dir(args, cwd, repo_root)

    raw_diff = collect_git_diff(cwd, args.git_diff_mode, base_ref=args.git_base_ref)
    diff_text = raw_diff if args.max_diff_chars <= 0 else raw_diff[: args.max_diff_chars]
    if args.max_diff_chars > 0 and len(raw_diff) > args.max_diff_chars:
        omitted = len(raw_diff) - args.max_diff_chars
        diff_text += f"\n\n[... truncated {omitted} characters ...]\n"

    include_paths = [Path(item) for item in args.include_file]
    file_snippets = collect_file_snippets(
        include_paths,
        cwd=cwd,
        repo_root=repo_root,
        max_chars=args.max_file_chars,
    )

    inputs = PromptInputs(
        profile=args.profile,
        task=task,
        focus=list(args.focus),
        cwd=str(cwd),
        repo_root=None if repo_root is None else str(repo_root),
        diff_mode=args.git_diff_mode,
        diff_text=diff_text,
        file_snippets=file_snippets,
    )
    prompt = build_prompt(inputs)

    write_json(output_dir / "review_bundle.json", bundle_to_dict(inputs))
    write_text(output_dir / "prompt.md", prompt)

    if args.dry_run:
        print(f"[gemini-review] dry run complete: {output_dir}")
        return

    mock_response_file = None
    if args.mock_response_file:
        mock_response_file = Path(args.mock_response_file)
        if not mock_response_file.is_absolute():
            mock_response_file = (cwd / mock_response_file).resolve()

    try:
        if backend == "api" and mock_response_file is None:
            if not model:
                raise ValueError("API backend requires a model name")
            result = run_gemini_api(
                prompt=prompt,
                model=model,
                timeout_sec=int(args.timeout_sec),
            )
        else:
            result = run_gemini_cli(
                prompt=prompt,
                gemini_bin=gemini_bin,
                cwd=cwd,
                model=model,
                timeout_sec=int(args.timeout_sec),
                extra_args=list(args.gemini_extra_arg),
                mock_response_file=mock_response_file,
            )
    except (GeminiCliInvocationError, GeminiApiInvocationError) as exc:
        raw_stdout = getattr(exc, "raw_stdout", "")
        raw_stderr = getattr(exc, "raw_stderr", "")
        if isinstance(exc, GeminiApiInvocationError):
            raw_stdout = exc.raw_response_text
            raw_stderr = ""
        write_text(output_dir / "gemini_stdout.txt", raw_stdout)
        write_text(output_dir / "gemini_stderr.txt", raw_stderr)
        write_text(output_dir / "run_error.txt", str(exc))
        error_type = type(exc).__name__
        error_payload: dict[str, object] = {
            "type": error_type,
            "message": str(exc),
        }
        exit_code = getattr(exc, "exit_code", None)
        if exit_code is not None:
            error_payload["exit_code"] = exit_code
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            error_payload["status_code"] = status_code
        write_run_metadata(
            output_dir=output_dir,
            command=getattr(exc, "command", ["gemini-api", model or DEFAULT_GEMINI_API_MODEL]),
            cwd=cwd,
            repo_root=repo_root,
            profile=args.profile,
            model=model,
            gemini_bin=gemini_bin,
            backend=backend,
            error=error_payload,
        )
        print(f"[gemini-review] output: {output_dir}", file=sys.stderr)
        print(f"[gemini-review] error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    write_text(output_dir / "gemini_stdout.txt", result.raw_stdout)
    write_text(output_dir / "gemini_stderr.txt", result.raw_stderr)
    write_json(output_dir / "gemini_cli_payload.json", result.cli_payload)
    write_text(output_dir / "response_text.txt", result.response_text)
    write_json(output_dir / "review.json", result.structured_review)
    write_text(output_dir / "review.md", render_review_markdown(result.structured_review))
    write_run_metadata(
        output_dir=output_dir,
        command=result.command,
        cwd=cwd,
        repo_root=repo_root,
        profile=args.profile,
        model=model,
        gemini_bin=gemini_bin,
        backend=backend,
    )

    issues = result.structured_review.get("critical_issues") or []
    print(f"[gemini-review] output: {output_dir}")
    print(
        "[gemini-review] "
        f"score={result.structured_review.get('score')} "
        f"decision={result.structured_review.get('decision')} "
        f"issues={len(issues)}"
    )


if __name__ == "__main__":
    main()
