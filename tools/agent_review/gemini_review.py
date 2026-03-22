from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n[... truncated {omitted} characters ...]\n"


def load_private_env_file(env_file: Path | None = None) -> list[str]:
    target = env_file or (Path.home() / ".gemini" / ".env")
    if not target.is_file():
        return []

    loaded: list[str] = []
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


@dataclass
class FileSnippet:
    path: str
    text: str
    truncated: bool


@dataclass
class PromptInputs:
    profile: str
    task: str
    focus: list[str]
    cwd: str
    repo_root: str | None
    diff_mode: str
    diff_text: str
    file_snippets: list[FileSnippet]


@dataclass
class GeminiRunResult:
    command: list[str]
    raw_stdout: str
    raw_stderr: str
    cli_payload: dict[str, Any]
    response_text: str
    structured_review: dict[str, Any]


class GeminiCliInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        raw_stdout: str,
        raw_stderr: str,
        exit_code: int,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr
        self.exit_code = exit_code


class GeminiApiInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        model: str,
        raw_response_text: str,
        status_code: int | None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.raw_response_text = raw_response_text
        self.status_code = status_code


DEFAULT_GEMINI_API_MODEL = "gemini-2.5-flash"


def resolve_repo_root(cwd: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return Path(text).resolve() if text else None


def resolve_gemini_binary(
    gemini_bin: str | None,
    *,
    cwd: Path,
    repo_root: Path | None,
) -> str:
    if gemini_bin:
        return gemini_bin

    candidates: list[Path] = []
    local_rel = Path(".local-tools/gemini-cli/node_modules/.bin/gemini")
    candidates.append((cwd / local_rel).resolve())
    if repo_root is not None:
        repo_candidate = (repo_root / local_rel).resolve()
        if repo_candidate not in candidates:
            candidates.append(repo_candidate)

    project_root = Path(__file__).resolve().parents[2]
    project_candidate = (project_root / local_rel).resolve()
    if project_candidate not in candidates:
        candidates.append(project_candidate)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    path_hit = shutil.which("gemini")
    if path_hit:
        return path_hit

    return "gemini"


def resolve_gemini_backend(preferred_backend: str) -> str:
    if preferred_backend not in {"auto", "api", "cli"}:
        raise ValueError(f"unsupported gemini backend: {preferred_backend}")
    if preferred_backend != "auto":
        return preferred_backend
    if os.environ.get("GEMINI_API_KEY"):
        return "api"
    return "cli"


def resolve_gemini_model(model: str | None, *, backend: str) -> str | None:
    if model:
        return model
    if backend == "api":
        return DEFAULT_GEMINI_API_MODEL
    return model


def collect_git_diff(cwd: Path, mode: str, *, base_ref: str | None = None) -> str:
    if mode == "none":
        return ""

    if mode == "worktree":
        command = ["git", "diff", "--", "."]
    elif mode == "staged":
        command = ["git", "diff", "--cached", "--", "."]
    elif mode == "base":
        if not base_ref:
            raise ValueError("--git-base-ref is required when --git-diff-mode=base")
        command = ["git", "diff", f"{base_ref}...HEAD", "--", "."]
    else:
        raise ValueError(f"unsupported diff mode: {mode}")

    completed = subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RuntimeError(f"git diff failed: {stderr or 'unknown error'}")
    return completed.stdout


def collect_file_snippets(
    files: list[Path],
    *,
    cwd: Path,
    repo_root: Path | None,
    max_chars: int,
) -> list[FileSnippet]:
    snippets: list[FileSnippet] = []
    for raw_path in files:
        path = raw_path if raw_path.is_absolute() else (cwd / raw_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"include file does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        truncated = max_chars > 0 and len(text) > max_chars
        snippet_text = truncate_text(text, max_chars)
        if repo_root is not None:
            try:
                display_path = str(path.relative_to(repo_root))
            except ValueError:
                display_path = str(path)
        else:
            display_path = str(path)
        snippets.append(FileSnippet(path=display_path, text=snippet_text, truncated=truncated))
    return snippets


def build_prompt(inputs: PromptInputs) -> str:
    focus_text = "\n".join(f"- {item}" for item in inputs.focus) if inputs.focus else "- none provided"
    diff_block = inputs.diff_text.strip() or "[no git diff included]"

    snippet_sections: list[str] = []
    for snippet in inputs.file_snippets:
        truncation = " (truncated)" if snippet.truncated else ""
        snippet_sections.append(
            f"### FILE: {snippet.path}{truncation}\n```text\n{snippet.text.rstrip()}\n```"
        )
    snippet_block = "\n\n".join(snippet_sections) if snippet_sections else "[no file snippets included]"

    return (
        "You are an external reviewer.\n\n"
        f"Review profile: {inputs.profile}\n"
        f"Working directory: {inputs.cwd}\n"
        f"Repository root: {inputs.repo_root or '[unknown]'}\n"
        f"Git diff mode: {inputs.diff_mode}\n\n"
        "Your job is to be skeptical, concrete, and adversarial in a productive way.\n"
        "Use only the provided context. If a key fact is missing, say so explicitly.\n\n"
        "Return ONLY a single valid JSON object with this schema:\n"
        "{\n"
        '  "score": 1,\n'
        '  "decision": "accept | revise | reject",\n'
        '  "summary": "short overall judgment",\n'
        '  "strengths": ["..."],\n'
        '  "critical_issues": [\n'
        "    {\n"
        '      "id": "ISSUE-1",\n'
        '      "severity": "high | medium | low",\n'
        '      "title": "short issue title",\n'
        '      "evidence": ["quote or file reference"],\n'
        '      "why_it_matters": "why this issue matters",\n'
        '      "minimum_fix": "smallest useful fix"\n'
        "    }\n"
        "  ],\n"
        '  "open_questions": ["..."],\n'
        '  "next_actions": ["..."]\n'
        "}\n\n"
        "Do not wrap the JSON in markdown fences.\n\n"
        "## Review Task\n"
        f"{inputs.task}\n\n"
        "## Focus Areas\n"
        f"{focus_text}\n\n"
        "## Git Diff\n"
        f"```diff\n{diff_block}\n```\n\n"
        "## File Snippets\n"
        f"{snippet_block}\n"
    )


def _parse_cli_payload(raw_stdout: str) -> dict[str, Any]:
    stripped = raw_stdout.strip()
    if not stripped:
        raise ValueError("gemini CLI produced empty stdout")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("gemini CLI stdout is not valid JSON; use --output-format json") from exc
    if not isinstance(payload, dict):
        raise ValueError("gemini CLI JSON payload must be an object")
    return payload


def build_review_response_json_schema() -> dict[str, Any]:
    issue_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "description": "Stable issue identifier such as ISSUE-1."},
            "severity": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Issue severity.",
            },
            "title": {"type": "string", "description": "Short issue title."},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete evidence such as a file path, quote, or observation.",
            },
            "why_it_matters": {"type": "string", "description": "Why this issue matters."},
            "minimum_fix": {"type": "string", "description": "Smallest useful fix."},
        },
        "required": ["id", "severity", "title", "evidence", "why_it_matters", "minimum_fix"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Overall quality score from 1 to 10.",
            },
            "decision": {
                "type": "string",
                "enum": ["accept", "revise", "reject"],
                "description": "Overall decision.",
            },
            "summary": {"type": "string", "description": "Short overall judgment."},
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Most important strengths.",
            },
            "critical_issues": {
                "type": "array",
                "items": issue_schema,
                "description": "Critical issues that block acceptance.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Open questions or missing facts.",
            },
            "next_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recommended next actions.",
            },
        },
        "required": [
            "score",
            "decision",
            "summary",
            "strengths",
            "critical_issues",
            "open_questions",
            "next_actions",
        ],
    }


def _extract_cli_error_message(raw_stdout: str, raw_stderr: str) -> str:
    def parse_message(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        if not isinstance(payload, dict):
            return stripped

        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        response = payload.get("response")
        if isinstance(response, str) and response.strip():
            return response.strip()

        return stripped

    for text in (raw_stdout, raw_stderr):
        message = parse_message(text)
        if message:
            return message

    return "unknown error"


def _extract_api_response_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            texts: list[str] = []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        texts.append(text)
            if texts:
                return "\n".join(texts).strip()

    prompt_feedback = payload.get("promptFeedback")
    if isinstance(prompt_feedback, dict):
        block_reason = prompt_feedback.get("blockReason")
        if isinstance(block_reason, str) and block_reason:
            raise ValueError(f"Gemini API response blocked: {block_reason}")

    raise ValueError("Gemini API response does not contain candidate text")


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", stripped)
        stripped = re.sub(r"\n```$", "", stripped)
    return stripped.strip()


def parse_structured_review(response_text: str) -> dict[str, Any]:
    cleaned = _strip_fences(response_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("model response does not contain a JSON object")
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise ValueError("structured review must be a JSON object")

    required = ["score", "decision", "summary", "critical_issues"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"structured review missing keys: {missing}")
    return payload


def run_gemini_api(
    *,
    prompt: str,
    model: str,
    timeout_sec: int,
) -> GeminiRunResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise GeminiApiInvocationError(
            "Gemini API backend requires GEMINI_API_KEY in the environment",
            model=model,
            raw_response_text="",
            status_code=None,
        )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request_payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": build_review_response_json_schema(),
            "temperature": 0.1,
        },
    }
    raw_request = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw_request,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw_stdout = response.read().decode("utf-8")
            status_code = getattr(response, "status", None)
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        message = raw_text.strip()
        try:
            error_payload = json.loads(raw_text)
            if isinstance(error_payload, dict):
                error = error_payload.get("error")
                if isinstance(error, dict):
                    api_message = error.get("message")
                    if isinstance(api_message, str) and api_message.strip():
                        message = api_message.strip()
        except json.JSONDecodeError:
            pass
        raise GeminiApiInvocationError(
            f"Gemini API failed with HTTP {exc.code}: {message or 'unknown error'}",
            model=model,
            raw_response_text=raw_text,
            status_code=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise GeminiApiInvocationError(
            f"Gemini API request failed: {exc.reason}",
            model=model,
            raw_response_text="",
            status_code=None,
        ) from exc

    try:
        api_payload = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise GeminiApiInvocationError(
            "Gemini API response is not valid JSON",
            model=model,
            raw_response_text=raw_stdout,
            status_code=status_code,
        ) from exc
    if not isinstance(api_payload, dict):
        raise GeminiApiInvocationError(
            "Gemini API response JSON must be an object",
            model=model,
            raw_response_text=raw_stdout,
            status_code=status_code,
        )

    response_text = _extract_api_response_text(api_payload)
    structured = parse_structured_review(response_text)
    return GeminiRunResult(
        command=["gemini-api", model],
        raw_stdout=raw_stdout,
        raw_stderr="",
        cli_payload=api_payload,
        response_text=response_text,
        structured_review=structured,
    )


def run_gemini_cli(
    *,
    prompt: str,
    gemini_bin: str,
    cwd: Path,
    model: str | None,
    timeout_sec: int,
    extra_args: list[str],
    mock_response_file: Path | None = None,
) -> GeminiRunResult:
    if mock_response_file is not None:
        raw_stdout = mock_response_file.read_text(encoding="utf-8")
        raw_stderr = ""
        if raw_stdout.strip().startswith("{"):
            parsed = _parse_cli_payload(raw_stdout)
            cli_payload = parsed if "response" in parsed else {"response": raw_stdout}
        else:
            cli_payload = {"response": raw_stdout}
        response_text = str(cli_payload.get("response", ""))
        structured = parse_structured_review(response_text)
        return GeminiRunResult(
            command=["mock-response", str(mock_response_file)],
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            cli_payload=cli_payload,
            response_text=response_text,
            structured_review=structured,
        )

    command = [gemini_bin, "-p", prompt, "--output-format", "json"]
    if model:
        command.extend(["-m", model])
    command.extend(extra_args)

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"gemini CLI not found: {gemini_bin}. Install Gemini CLI first or pass --gemini-bin."
        ) from exc

    raw_stdout = completed.stdout
    raw_stderr = completed.stderr
    if completed.returncode != 0:
        message = _extract_cli_error_message(raw_stdout, raw_stderr)
        raise GeminiCliInvocationError(
            f"gemini CLI failed with exit code {completed.returncode}: {message}",
            command=command,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=completed.returncode,
        )

    cli_payload = _parse_cli_payload(raw_stdout)
    response_text = str(cli_payload.get("response", "")).strip()
    if not response_text:
        raise ValueError("gemini CLI JSON payload does not contain a non-empty response field")
    structured = parse_structured_review(response_text)
    return GeminiRunResult(
        command=command,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        cli_payload=cli_payload,
        response_text=response_text,
        structured_review=structured,
    )


def render_review_markdown(review: dict[str, Any]) -> str:
    strengths = review.get("strengths") or []
    issues = review.get("critical_issues") or []
    questions = review.get("open_questions") or []
    actions = review.get("next_actions") or []

    lines = [
        "# Gemini Review",
        "",
        f"- Score: {review.get('score')}",
        f"- Decision: {review.get('decision')}",
        "",
        "## Summary",
        str(review.get("summary", "")).strip(),
        "",
        "## Strengths",
    ]
    if strengths:
        lines.extend(f"- {item}" for item in strengths)
    else:
        lines.append("- none")

    lines.extend(["", "## Critical Issues"])
    if issues:
        for issue in issues:
            lines.extend(
                [
                    f"### {issue.get('id', 'ISSUE')} | {issue.get('severity', 'unknown')} | {issue.get('title', '')}",
                    "",
                    f"- Why it matters: {issue.get('why_it_matters', '')}",
                    f"- Minimum fix: {issue.get('minimum_fix', '')}",
                ]
            )
            evidence = issue.get("evidence") or []
            if evidence:
                lines.append("- Evidence:")
                lines.extend(f"  - {item}" for item in evidence)
            lines.append("")
    else:
        lines.append("- none")
        lines.append("")

    lines.append("## Open Questions")
    if questions:
        lines.extend(f"- {item}" for item in questions)
    else:
        lines.append("- none")

    lines.extend(["", "## Next Actions"])
    if actions:
        lines.extend(f"- {item}" for item in actions)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def bundle_to_dict(inputs: PromptInputs) -> dict[str, Any]:
    data = asdict(inputs)
    data["file_snippets"] = [asdict(item) for item in inputs.file_snippets]
    return data
