"""The agent loop: model -> tool calls -> results -> model, until the task is done."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .llm import ChatMessage
from .tools import ToolBox

SYSTEM_PROMPT = """\
You are errander, a small coding agent working in the user's workspace.

You accomplish tasks by calling tools: explore with list_dir, look before you
leap with read_file, find code with search_files, make changes with write_file,
and verify with run_command. Work in small steps and check your results.

Ground rules:
- Paths are relative to the workspace root; anything outside it is blocked.
- run_command asks the user for confirmation. Never repeat a command they declined.
- When the task is complete, reply with a concise summary and stop calling tools.
- If you are truly stuck, say so instead of repeating the same failed action.
"""

EventFn = Callable[..., None]


@dataclass
class AgentResult:
    """Outcome of one run(): the final answer plus loop statistics."""

    answer: str
    steps: int
    tool_calls: int = 0
    stopped_reason: str | None = None


class Agent:
    """Runs the loop. Any client with a compatible chat() method works.

    The whole design fits on one screen: keep a message list, ask the model
    for its next message, execute any tool calls it asks for, append the
    results, and repeat — until it replies without tool calls.
    """

    def __init__(
        self,
        client: Any,
        toolbox: ToolBox,
        max_steps: int = 25,
        stream: bool = True,
        on_event: EventFn | None = None,
    ):
        self.client = client
        self.toolbox = toolbox
        self.max_steps = max_steps
        self.stream = stream
        self.on_event = on_event or (lambda *args, **kwargs: None)
        self.messages: list[ChatMessage] = [ChatMessage(role="system", content=SYSTEM_PROMPT)]

    def run(self, task: str) -> AgentResult:
        self.messages.append(ChatMessage(role="user", content=task))
        total_calls = 0
        for step in range(1, self.max_steps + 1):
            self.on_event("turn", step)
            reply = self.client.chat(
                self.messages,
                tools=self.toolbox.schemas(),
                stream=self.stream,
                on_delta=lambda text: self.on_event("delta", text),
            )
            self.messages.append(reply)
            self.on_event("assistant", reply)
            if not reply.tool_calls:
                return AgentResult(answer=reply.content or "", steps=step, tool_calls=total_calls)
            for call in reply.tool_calls:
                total_calls += 1
                self.on_event("tool_call", call)
                result = self.toolbox.execute(call)
                self.on_event("tool_result", call, result)
                self.messages.append(ChatMessage(role="tool", content=result, tool_call_id=call.id))
        return AgentResult(
            answer="",
            steps=self.max_steps,
            tool_calls=total_calls,
            stopped_reason=f"step budget of {self.max_steps} exhausted",
        )
