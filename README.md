<div align="center">

# errander

**A tiny terminal coding agent — no framework, no dependencies, just the agent loop.**

[![CI](https://github.com/EVAE-AL/errander/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

*English | [中文文档](README.zh-CN.md)*

</div>

---

errander takes a task in plain language and works on your codebase with five tools — **list, read, write, search, run** — until it's done. It talks to **any OpenAI-compatible API** (DeepSeek, Zhipu GLM, Qwen, Kimi, OpenAI, local Ollama…), and the entire agent is **4 files / ~830 lines of pure Python** with **zero third-party dependencies**.

## Example session

```text
$ errander "collect all TODO comments in this repo into TODOS.md"

errander v0.1.0 · model deepseek-chat · workspace ~/demo

── step 1 ──────────────────────────────────────
● search_files {"ignore_case": true, "path": ".", "query": "TODO"}
  app/api.py:17:  # TODO: rate-limit this endpoint
  app/tasks.py:42:  # TODO: retries are not exponential yet
── step 2 ──────────────────────────────────────
● write_file TODOS.md  (128 chars)
  created TODOS.md (128 chars)
── step 3 ──────────────────────────────────────
Found 2 TODO comments and wrote them to TODOS.md with file and line references.

3 steps · 2 tool calls · 9.8s · 1,842 tokens
```

## Why this exists

Most agent tutorials hand you a framework. This project is the opposite: the agent loop lives in `src/errander/agent.py` and fits on one screen. Everything that happens between "task" and "done" — SSE parsing, the tool sandbox, model self-correction — can be read in one sitting. Zero dependencies means nothing is hidden.

## Quickstart

Requires Python 3.10+.

```bash
git clone https://github.com/EVAE-AL/errander
cd errander
pip install .

cp .env.example .env      # Windows: copy .env.example .env
# then edit .env and put in your API key

errander "explain what this repo does and write a 5-bullet summary to OVERVIEW.md"
```

Any OpenAI-compatible provider works — set these in `.env` (see `.env.example`):

| Provider | `ERRANDER_BASE_URL` | `ERRANDER_MODEL` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.5-flash` (free, good at tool use) |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Ollama (local) | `http://localhost:11434/v1` | `qwen2.5:7b` |

Useful flags: `--workdir` (workspace root, default: current dir), `--max-steps` (default 25), `-y/--yes` (auto-approve shell commands), `--no-stream`, and tasks can also be piped in: `echo "..." | errander`.

## How it works

```text
             ┌────────────────────────────────────────────┐
  task ────► │  Agent loop               agent.py (~88 lines)
             │                                            │
             │    messages ───►  LLM client  llm.py (~228)
             │                        │          ▲        │──► any OpenAI-compatible API
             │             tool calls │          │ results
             │                        ▼          │        │
             │                  ToolBox  tools.py (~280)
             │         list · read · write · search · run │
             │                                            │
             │    reply without tool calls ───► answer    │
             └────────────────────────────────────────────┘
```

1. The task is appended to a message list that starts with a system prompt.
2. The model replies with either plain text (done) or one or more `tool_calls`.
3. Tool calls are executed; their results are appended as `tool` messages.
4. Repeat until the model replies without tool calls, or the step budget is hit.

`cli.py` (~212 lines) wires it to the terminal: argument parsing, `.env` loading, colored rendering, and confirmations.

## Design decisions worth stealing

- **Errors are data, not exceptions.** A failed tool call returns `error: ...` as the tool result, so the model can read it and self-correct instead of the run crashing.
- **The sandbox is structural.** File tools resolve every path against the workspace root and refuse escapes (`../`, absolute paths, symlinks out); shell commands require an interactive y/N and are auto-declined when stdin is not a terminal.
- **Context hygiene is built in.** Tool output is capped at 8k characters, file reads are windowed at 400 lines with a "continue at start=N" hint, and search stops at 100 hits. Token cost is controlled by construction, not by hoping the model behaves.
- **Hand-rolled SSE, with a fallback.** `llm.py` parses `text/event-stream` with stdlib only — and retries without `stream_options` if a server rejects it.
- **No framework.** There isn't a single LangChain/agents-sdk import in the repo. If you want to learn what an agent actually is, the interesting 50 lines are all here.

## Safety

- File tools cannot read or write anything outside `--workdir`.
- `run_command` asks for confirmation per command; `--yes` skips that (for CI or throwaway directories only).
- `.env` is git-ignored; `.env.example` documents every variable without real secrets.

## Development

```bash
pip install -e ".[dev]"
pytest          # 20 tests, including a real-socket SSE test against a local HTTP server
ruff check .
```

CI runs the same lint + tests on Python 3.10–3.13.

## Roadmap

- [ ] Interactive REPL mode (multi-turn conversation)
- [ ] Session persistence — resume a run after Ctrl-C
- [ ] Token budget meter with cost estimate
- [ ] Import MCP servers as tools

## License

[MIT](LICENSE)
