<div align="center">

# errander

**一个极简终端编程 Agent —— 不用框架、零依赖，只有 agent 循环本身。**

[![CI](https://github.com/EVAE-AL/errander/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

*[English](README.md) | 中文文档*

</div>

---

errander 接收一句自然语言任务，然后用 5 个工具（**列目录、读文件、写文件、搜索、跑命令**）在你的代码库里干活，直到完成。它对接**任何 OpenAI 兼容 API**（DeepSeek、智谱 GLM、Qwen、Kimi、OpenAI、本地 Ollama……），整个 agent 只有 **4 个文件 / 约 830 行纯 Python**，**零第三方依赖**。

## 演示

```text
$ errander "把这个仓库里的 TODO 注释收集到 TODOS.md"

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

## 为什么做这个

大多数教程塞给你一个框架，这个项目反其道而行：agent 主循环就在 `src/errander/agent.py`，一屏放得下。从「任务」到「完成」之间发生的每一件事——SSE 解析、工具沙箱、模型自纠错——都可以一口气读完。零依赖意味着没有任何被隐藏的逻辑。

## 快速开始

需要 Python 3.10+。

```bash
git clone https://github.com/EVAE-AL/errander
cd errander
pip install .

copy .env.example .env     # Windows；macOS/Linux 用 cp
# 编辑 .env，填入你的 API Key

errander "看看这个仓库是干什么的，写一份 5 条要点的总结到 OVERVIEW.md"
```

任何 OpenAI 兼容的模型服务都能用——在 `.env` 里配置（详见 `.env.example`）：

| 提供商 | `ERRANDER_BASE_URL` | `ERRANDER_MODEL` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash`（免费） |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Ollama（本地） | `http://localhost:11434/v1` | `qwen2.5:7b` |

常用参数：`--workdir`（工作区根目录，默认当前目录）、`--max-steps`（默认 25）、`-y/--yes`（自动批准 shell 命令）、`--no-stream`；任务也可以从管道读入：`echo "..." | errander`。

## 工作原理

```text
             ┌────────────────────────────────────────────┐
  任务 ────► │  Agent 循环                agent.py（~88 行）
             │                                            │
             │    消息列表 ───►  LLM 客户端  llm.py（~228）
             │                        │          ▲        │──► 任何 OpenAI 兼容 API
             │             工具调用   │          │ 结果
             │                        ▼          │        │
             │                  ToolBox  tools.py（~280）
             │         list · read · write · search · run │
             │                                            │
             │    不带工具调用的回复 ───► 最终答案          │
             └────────────────────────────────────────────┘
```

1. 任务追加到消息列表，列表以系统提示词开头。
2. 模型回复：要么是纯文本（完成），要么是一个或多个 `tool_calls`。
3. 工具调用被真实执行，结果以 `tool` 消息的形式追加回消息列表。
4. 循环往复，直到模型不再调用工具，或步数预算耗尽。

`cli.py`（约 212 行）负责接终端：参数解析、`.env` 加载、彩色渲染、命令确认。

## 值得借鉴的设计

- **错误是数据，不是异常。** 工具执行失败会以 `error: ...` 文本返回给模型，让它读到原因并自我修正，而不是让整次运行崩溃。
- **沙箱是结构性的。** 文件工具把每个路径 resolve 到工作区根目录之内，`../`、绝对路径等逃逸手段一律拒绝；shell 命令逐条 y/N 确认，stdin 不是终端时自动拒绝。
- **上下文卫生是内建的。** 工具输出截断在 8k 字符、读文件按 400 行分页并提示续读位置（`start=N`）、搜索最多 100 条命中——token 花销在结构上就被控制住，而不是指望模型自觉。
- **手写 SSE，带兜底。** `llm.py` 仅用标准库解析 `text/event-stream`；若服务端拒绝 `stream_options` 参数会自动去掉后重试。
- **不用框架。** 全仓库没有一行 LangChain / agents-sdk。想搞明白 agent 到底是什么，最有趣的 50 行都在这里。

## 安全

- 文件工具无法读写 `--workdir` 之外的任何路径。
- `run_command` 每条命令都要确认；`--yes` 可跳过（仅建议在 CI 或一次性目录中使用）。
- `.env` 已被 git 忽略；`.env.example` 记录了所有变量且不含真实密钥。

## 开发

```bash
pip install -e ".[dev]"
pytest          # 20 个测试，含一个通过本地 HTTP 服务器验证 SSE 解析的真实 socket 测试
ruff check .
```

CI 会在 Python 3.10–3.13 上跑同样的 lint + 测试。

## Roadmap

- [ ] 交互式 REPL 模式（多轮对话）
- [ ] 会话持久化——Ctrl-C 后可恢复
- [ ] token 预算表与费用估算
- [ ] 把 MCP server 导入为工具

## License

[MIT](LICENSE)
