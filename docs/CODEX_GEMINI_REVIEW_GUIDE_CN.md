# Codex + Gemini 审稿人指南

使用 **Codex CLI 作为执行者**，使用 **Gemini 作为本地结构化审稿人**。

[English version](CODEX_GEMINI_REVIEW_GUIDE.md)

这条路径适合你在以下场景使用：

- 让 Codex 负责具体实现和仓库修改
- 让 Gemini 作为外部审查器给出结构化反馈
- 想用**仓库内本地 runner**，而不是 GitHub Actions 或托管服务
- 希望每轮审查都保存完整产物：prompt、原始响应、解析后的 JSON、markdown 摘要

这份指南是在 ARIS 现有能力旁边补上一条本地工具链，不会替代 `skills/skills-codex/`。

## 为什么值得加这条路径

- 对很多用户来说，Codex CLI 已经是足够低门槛的本地执行器。
- Gemini 往往是最容易获取的外部审稿人接口之一，可以通过 API、CLI，或已有的学生计划来接入。
- 把两者配对后，依然保留“执行者 / 审查者”分离的优点，同时显著降低 ARIS 工作流的使用成本和接入门槛。

## 新增了什么

新增两个本地入口：

- `tools/run_gemini_review.py`
  - 对 git diff + 指定文件执行一次结构化 Gemini 审查
- `tools/run_agent_review_loop.py`
  - 多轮执行者 / 审稿循环
  - 每一轮都会：
    1. 运行执行命令
    2. 收集新的仓库状态
    3. 让 Gemini 给出严格的结构化审查
    4. 在 `accept` 时停止，否则继续下一轮

两个工具都只依赖 Python 标准库，以及你本地已有的 Gemini 访问方式。

## 审查模式

reviewer 支持三种 profile：

- `code`
- `research`
- `generic`

所有 profile 都会强制 Gemini 返回严格 JSON 对象，包含：

- `score`
- `decision`
- `summary`
- `strengths`
- `critical_issues`
- `open_questions`
- `next_actions`

## 依赖要求

你需要以下两种 reviewer backend 之一：

1. **Gemini API**
   - 设置 `GEMINI_API_KEY`
2. **Gemini CLI**
   - 本地安装 `gemini`
   - 已完成 CLI 登录，或 CLI 已配置 API key

工具按以下顺序选择 reviewer backend：

- `--backend auto`
  - 如果存在 `GEMINI_API_KEY`，优先用 API
  - 否则退回到 CLI
- `--backend api`
- `--backend cli`

## 可选的本地配置

推荐的凭证文件：

```bash
mkdir -p ~/.gemini
cat > ~/.gemini/.env <<'EOF'
GEMINI_API_KEY="your-key"
EOF
```

如果存在，runner 会自动加载 `~/.gemini/.env`。

如果你想在仓库内本地安装 Gemini CLI：

```bash
mkdir -p .local-tools/gemini-cli
cd .local-tools/gemini-cli
npm install @google/gemini-cli
```

runner 按以下顺序查找 Gemini 可执行文件：

1. `--gemini-bin`
2. `.local-tools/gemini-cli/node_modules/.bin/gemini`
3. `PATH`

## 单次审查

把当前 worktree 当成工程代码做严格审查：

```bash
python3 tools/run_gemini_review.py \
  --profile code \
  --task "Review the current changes as a skeptical senior engineer." \
  --git-diff-mode worktree \
  --include-file README.md
```

审查一次研究进展：

```bash
python3 tools/run_gemini_review.py \
  --profile research \
  --task-file /path/to/review_task.md \
  --git-diff-mode base \
  --git-base-ref origin/main
```

默认输出目录：

```text
outputs/gemini_review_<timestamp>/
```

产物包括：

- `review_bundle.json`
- `prompt.md`
- `gemini_stdout.txt`
- `gemini_stderr.txt`
- `gemini_cli_payload.json`
- `response_text.txt`
- `review.json`
- `review.md`
- `run_metadata.json`

## 多轮 Executor + Review 循环

这个 loop runner 适合“实现 -> 审查 -> 再迭代”。

最小示例：

```bash
python3 tools/run_agent_review_loop.py \
  --profile code \
  --task "Fix the highest-priority issues in the current repository." \
  --executor-cmd 'pytest -q || true' \
  --review-backend auto \
  --max-rounds 3
```

更接近 Codex 工作方式的示例：

```bash
python3 tools/run_agent_review_loop.py \
  --profile research \
  --task "Address the reviewer-critical issues with the minimum useful repository changes." \
  --executor-cmd 'printf "%s\n" "Use Codex in this round, then save notes into $AGENT_REVIEW_EXECUTOR_DIR/notes.txt"' \
  --include-file README.md \
  --max-rounds 2
```

loop 输出目录：

```text
outputs/agent_review_loop_<timestamp>/
```

每一轮会得到：

- `executor/`
- `review/`
- `round_summary.json`

loop 根目录还会保存：

- `loop_config.json`
- `loop_summary.json`
- `loop_summary.md`

## 对执行命令有用的环境变量

每轮执行时，loop 会导出：

- `AGENT_REVIEW_LOOP_DIR`
- `AGENT_REVIEW_ROUND_DIR`
- `AGENT_REVIEW_ROUND_INDEX`
- `AGENT_REVIEW_EXECUTOR_DIR`
- `AGENT_REVIEW_DIR`
- `AGENT_REVIEW_EXECUTOR_TASK_FILE`
- `AGENT_REVIEW_EXECUTOR_CONTEXT_FILE`
- `AGENT_REVIEW_PREVIOUS_JSON`
- `AGENT_REVIEW_PREVIOUS_MD`

这样你的执行命令就可以读取上一轮审查结果，并在当前轮目录下写产物，而不需要硬编码路径。

## 离线 Smoke Test

如果你想不调用真实 Gemini 先验证整条链路，可以使用 mock JSON：

```bash
python3 tools/run_gemini_review.py \
  --task "Smoke test the local review pipeline." \
  --mock-response-file /path/to/mock_review.json
```

mock 文件可以是以下任一种：

- 原始 review JSON 对象
- 带 `response` 字段的 CLI 风格 JSON，字段内是 review JSON 文本

## 什么时候适合用这条路径

如果你想要：

- Codex 在本地执行
- Gemini 在本地审查
- 不依赖 Claude 审稿 API
- 不依赖 OpenAI 审稿 API
- 用一个轻量、仓库级别的 review loop 代替 MCP 基础设施

就选这条路径。

如果你明确想要下面这种组合，请改看 [`docs/CODEX_CLAUDE_REVIEW_GUIDE_CN.md`](CODEX_CLAUDE_REVIEW_GUIDE_CN.md)：

- Codex 作为执行者
- Claude Code CLI 作为审稿人
- 通过 MCP bridge 暴露 reviewer
