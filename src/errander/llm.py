"""Minimal chat client for any OpenAI-compatible /chat/completions endpoint.

Standard library only (urllib) with a hand-rolled SSE parser, so streaming
works without any third-party SDK.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DeltaFn = Callable[[str], None]


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    """One message in the conversation, in OpenAI chat format."""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    def to_api(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        return msg


def _parse_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
    calls = []
    for raw in raw_calls or []:
        fn = raw.get("function") or {}
        arguments = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        except json.JSONDecodeError:
            # Malformed arguments go back to the model as feedback, not a crash.
            parsed = {"_unparsed": str(arguments)}
        calls.append(
            ToolCall(id=raw.get("id") or "call_0", name=fn.get("name") or "", arguments=parsed)
        )
    return calls


def _message_from_api(choice: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=choice.get("role", "assistant"),
        content=choice.get("content"),
        tool_calls=_parse_tool_calls(choice.get("tool_calls")),
    )


class LLMError(RuntimeError):
    """The chat endpoint failed, or returned something we cannot parse."""


class LLMClient:
    """Talks to any OpenAI-compatible chat endpoint, streaming or not."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        on_delta: DeltaFn | None = None,
    ) -> ChatMessage:
        """Send the conversation, get back the assistant's next message."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            try:
                return self._chat_stream(payload, on_delta)
            except LLMError as exc:
                text = str(exc)
                if "400" in text and "stream_options" in text:
                    # Some compatible servers reject unknown fields; retry without it.
                    payload.pop("stream_options")
                    return self._chat_stream(payload, on_delta)
                raise
        return self._chat_once(payload)

    # -- non-streaming -------------------------------------------------------

    def _chat_once(self, payload: dict[str, Any]) -> ChatMessage:
        data = self._post(payload)
        self._track_usage(data.get("usage"))
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {json.dumps(data)[:300]}") from exc
        return _message_from_api(choice)

    # -- streaming (hand-rolled SSE) -------------------------------------------

    def _chat_stream(self, payload: dict[str, Any], on_delta: DeltaFn | None) -> ChatMessage:
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            with self._open(payload) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # keep-alive comments and the like
                    self._track_usage(chunk.get("usage"))
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            content_parts.append(piece)
                            if on_delta:
                                on_delta(piece)
                        for i, tc in enumerate(delta.get("tool_calls") or []):
                            slot = calls.setdefault(
                                tc.get("index", i), {"id": "", "name": "", "args": []}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"].append(fn["arguments"])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise LLMError(f"HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"connection failed: {exc.reason}") from exc
        except OSError as exc:  # raw socket timeouts are not wrapped in URLError
            raise LLMError(f"connection failed: {exc}") from exc

        tool_calls = []
        for slot in calls.values():
            raw_args = "".join(slot["args"]) or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {"_unparsed": raw_args}
            tool_calls.append(
                ToolCall(id=slot["id"] or "call_0", name=slot["name"], arguments=arguments)
            )
        content = "".join(content_parts)
        return ChatMessage(role="assistant", content=content or None, tool_calls=tool_calls)

    # -- plumbing -----------------------------------------------------------------

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            },
            method="POST",
        )

    def _open(self, payload: dict[str, Any]) -> Any:
        req = self._request(payload)
        # Loopback endpoints (tests, local Ollama) must bypass any system proxy.
        host = urllib.parse.urlsplit(req.full_url).hostname or ""
        if host in {"localhost", "127.0.0.1", "::1"}:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(req, timeout=self.timeout)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._open(payload) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise LLMError(f"HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"connection failed: {exc.reason}") from exc
        except OSError as exc:
            raise LLMError(f"connection failed: {exc}") from exc

    def _track_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.prompt_tokens += usage.get("prompt_tokens") or 0
        self.completion_tokens += usage.get("completion_tokens") or 0
