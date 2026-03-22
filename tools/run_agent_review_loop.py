#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.agent_review import (  # noqa: E402
    resolve_repo_root,
    run_agent_review_loop,
)
from tools.agent_review.review_loop import (  # noqa: E402
    normalize_paths,
    read_task_text,
    resolve_executor_command,
    resolve_output_dir,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a multi-round executor + Gemini review loop against the current repository."
    )
    parser.add_argument("--profile", choices=["code", "research", "generic"], default="code")
    parser.add_argument("--task", default=None, help="direct loop task text")
    parser.add_argument("--task-file", default=None, help="path to a text/markdown file containing the loop task")
    parser.add_argument("--focus", action="append", default=[], help="focus area; repeatable")
    parser.add_argument("--include-file", action="append", default=[], help="file included in every review round")
    parser.add_argument(
        "--git-diff-mode",
        choices=["none", "worktree", "staged", "base"],
        default="worktree",
        help="which git diff each review round should include",
    )
    parser.add_argument("--git-base-ref", default=None, help="required when --git-diff-mode=base")
    parser.add_argument("--max-diff-chars", type=int, default=16000)
    parser.add_argument("--max-file-chars", type=int, default=6000)
    parser.add_argument("--cwd", default=".", help="working directory for executor and reviewer")
    parser.add_argument("--output-dir", default=None, help="output directory for loop artifacts")
    parser.add_argument(
        "--review-backend",
        choices=["auto", "api", "cli"],
        default="auto",
        dest="review_backend",
        help="review backend; auto prefers API when GEMINI_API_KEY is available",
    )
    parser.add_argument("--gemini-bin", default=None, help="Gemini CLI binary; defaults to repo-local install, then PATH")
    parser.add_argument("--model", default=None, help="optional Gemini model name")
    parser.add_argument("--review-timeout-sec", type=int, default=900, dest="review_timeout_sec")
    parser.add_argument("--gemini-extra-arg", action="append", default=[], help="extra arg passed to gemini CLI")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument(
        "--stop-on-decision",
        choices=["accept", "accept-or-reject", "never"],
        default="accept",
        help="when to stop after a review round",
    )
    parser.add_argument("--executor-cmd", default=None, help="bash command run each round as the executor step")
    parser.add_argument("--executor-cmd-file", default=None, help="file containing the executor bash command")
    parser.add_argument("--executor-timeout-sec", type=int, default=1800)
    parser.add_argument(
        "--executor-allow-nonzero",
        action="store_true",
        help="continue to the review step even if the executor command exits non-zero",
    )
    parser.add_argument(
        "--review-mock-response-file",
        action="append",
        default=[],
        dest="review_mock_response_file",
        help="mock Gemini response file; repeat to provide one per round, last value repeats",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cwd = Path(args.cwd).resolve()
    repo_root = resolve_repo_root(cwd)
    base_task = read_task_text(args.task, args.task_file, cwd=cwd, profile=args.profile)
    include_files = normalize_paths(list(args.include_file), cwd=cwd)
    mock_files = normalize_paths(list(args.review_mock_response_file), cwd=cwd)
    executor_command = resolve_executor_command(args.executor_cmd, args.executor_cmd_file, cwd=cwd)
    output_dir = resolve_output_dir(args.output_dir, cwd=cwd, repo_root=repo_root)

    summary = run_agent_review_loop(
        project_root=PROJECT_ROOT,
        cwd=cwd,
        output_dir=output_dir,
        profile=args.profile,
        base_task=base_task,
        focus=list(args.focus),
        include_files=include_files,
        git_diff_mode=args.git_diff_mode,
        git_base_ref=args.git_base_ref,
        max_diff_chars=args.max_diff_chars,
        max_file_chars=args.max_file_chars,
        gemini_bin=args.gemini_bin,
        review_backend=args.review_backend,
        model=args.model,
        gemini_extra_args=list(args.gemini_extra_arg),
        max_rounds=int(args.max_rounds),
        stop_on_decision=args.stop_on_decision,
        executor_command_text=executor_command,
        executor_timeout_sec=int(args.executor_timeout_sec),
        executor_allow_nonzero=bool(args.executor_allow_nonzero),
        review_timeout_sec=int(args.review_timeout_sec),
        review_mock_response_files=mock_files,
    )

    print(f"[agent-review-loop] output: {output_dir}")
    print(
        "[agent-review-loop] "
        f"status={summary.get('status')} "
        f"stop_reason={summary.get('stop_reason')} "
        f"completed_rounds={summary.get('completed_rounds')}"
    )
    if summary.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
