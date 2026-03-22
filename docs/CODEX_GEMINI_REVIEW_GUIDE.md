# Codex + Gemini Reviewer Guide

Use **Codex CLI as the executor** and **Gemini as a structured local reviewer**.

[中文版本](CODEX_GEMINI_REVIEW_GUIDE_CN.md)

This path is useful when you want:

- Codex to do the implementation work
- Gemini to act as an external reviewer
- a **repo-local** review runner instead of GitHub Actions or a hosted service
- saved artifacts for every review round: prompt, raw response, parsed JSON, markdown summary

This guide adds a local toolchain alongside ARIS. It does **not** replace `skills/skills-codex/`.

## Why This Path

- Codex CLI is a practical low-friction local executor for many users.
- Gemini is often the easiest external reviewer to reach through API access, CLI access, or existing student plans.
- Pairing the two keeps the executor/reviewer split while lowering both cost and setup friction for ARIS-style workflows.

## What This Adds

Two local entrypoints:

- `tools/run_gemini_review.py`
  - single structured Gemini review over a git diff + selected files
- `tools/run_agent_review_loop.py`
  - multi-round executor/reviewer loop
  - each round:
    1. run an executor command
    2. collect the new repo state
    3. ask Gemini for a strict structured review
    4. stop on `accept` or continue

Both tools use only Python standard library plus your local Gemini access.

## Review Modes

The reviewer supports three profiles:

- `code`
- `research`
- `generic`

All profiles force Gemini to return a strict JSON object with:

- `score`
- `decision`
- `summary`
- `strengths`
- `critical_issues`
- `open_questions`
- `next_actions`

## Requirements

You need one of these reviewer backends:

1. **Gemini API**
   - set `GEMINI_API_KEY`
2. **Gemini CLI**
   - install `gemini` locally
   - authenticated CLI session or API-key-backed CLI config

The tools choose reviewer backend like this:

- `--backend auto`
  - prefer API when `GEMINI_API_KEY` exists
  - otherwise fall back to CLI
- `--backend api`
- `--backend cli`

## Optional Local Setup

Recommended credential file:

```bash
mkdir -p ~/.gemini
cat > ~/.gemini/.env <<'EOF'
GEMINI_API_KEY="your-key"
EOF
```

The runners auto-load `~/.gemini/.env` if present.

If you want a repo-local Gemini CLI install:

```bash
mkdir -p .local-tools/gemini-cli
cd .local-tools/gemini-cli
npm install @google/gemini-cli
```

The runner resolves the Gemini binary in this order:

1. `--gemini-bin`
2. `.local-tools/gemini-cli/node_modules/.bin/gemini`
3. `PATH`

## Single Review

Review the current worktree as a strict engineering reviewer:

```bash
python3 tools/run_gemini_review.py \
  --profile code \
  --task "Review the current changes as a skeptical senior engineer." \
  --git-diff-mode worktree \
  --include-file README.md
```

Review a research update:

```bash
python3 tools/run_gemini_review.py \
  --profile research \
  --task-file /path/to/review_task.md \
  --git-diff-mode base \
  --git-base-ref origin/main
```

Default output:

```text
outputs/gemini_review_<timestamp>/
```

Artifacts include:

- `review_bundle.json`
- `prompt.md`
- `gemini_stdout.txt`
- `gemini_stderr.txt`
- `gemini_cli_payload.json`
- `response_text.txt`
- `review.json`
- `review.md`
- `run_metadata.json`

## Multi-Round Executor + Review Loop

The loop runner is for “implement, then review, then iterate”.

Minimal example:

```bash
python3 tools/run_agent_review_loop.py \
  --profile code \
  --task "Fix the highest-priority issues in the current repository." \
  --executor-cmd 'pytest -q || true' \
  --review-backend auto \
  --max-rounds 3
```

Practical Codex-style example:

```bash
python3 tools/run_agent_review_loop.py \
  --profile research \
  --task "Address the reviewer-critical issues with the minimum useful repository changes." \
  --executor-cmd 'printf "%s\n" "Use Codex in this round, then save notes into $AGENT_REVIEW_EXECUTOR_DIR/notes.txt"' \
  --include-file README.md \
  --max-rounds 2
```

Loop output:

```text
outputs/agent_review_loop_<timestamp>/
```

Each round gets:

- `executor/`
- `review/`
- `round_summary.json`

The loop root also stores:

- `loop_config.json`
- `loop_summary.json`
- `loop_summary.md`

## Useful Executor Environment Variables

During each executor round, the loop exports:

- `AGENT_REVIEW_LOOP_DIR`
- `AGENT_REVIEW_ROUND_DIR`
- `AGENT_REVIEW_ROUND_INDEX`
- `AGENT_REVIEW_EXECUTOR_DIR`
- `AGENT_REVIEW_DIR`
- `AGENT_REVIEW_EXECUTOR_TASK_FILE`
- `AGENT_REVIEW_EXECUTOR_CONTEXT_FILE`
- `AGENT_REVIEW_PREVIOUS_JSON`
- `AGENT_REVIEW_PREVIOUS_MD`

This lets your executor command read the previous review and write round-local artifacts without guessing paths.

## Offline Smoke Testing

You can validate the flow without calling real Gemini by using a mock JSON file:

```bash
python3 tools/run_gemini_review.py \
  --task "Smoke test the local review pipeline." \
  --mock-response-file /path/to/mock_review.json
```

The mock file should contain either:

- a raw review JSON object, or
- a CLI-style JSON payload with a `response` field containing the review JSON text

## When To Use This Path

Choose this guide when you want:

- Codex to execute locally
- Gemini to review locally
- no Claude API requirement
- no OpenAI API requirement
- a lightweight repo-level review loop outside MCP infrastructure

Choose [`docs/CODEX_CLAUDE_REVIEW_GUIDE.md`](docs/CODEX_CLAUDE_REVIEW_GUIDE.md) when you specifically want:

- Codex as executor
- Claude Code CLI as reviewer
- reviewer access via an MCP bridge
