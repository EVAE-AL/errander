import json

import pytest

from errander.llm import ChatMessage, LLMClient, LLMError


def sse_body(*chunks: dict) -> bytes:
    events = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return events + b"data: [DONE]\n\n"


def test_non_streaming_response_is_parsed(fake_server):
    fake_server.state["body"] = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "on it",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode()

    client = LLMClient(fake_server.url, "key", "model")
    message = client.chat([ChatMessage(role="user", content="hi")], stream=False)

    assert message.role == "assistant"
    assert message.content == "on it"
    assert message.tool_calls[0].name == "read_file"
    assert message.tool_calls[0].arguments == {"path": "a.py"}
    assert (client.prompt_tokens, client.completion_tokens) == (10, 5)


def tool_delta(index: int, **function: str) -> dict:
    return {"choices": [{"delta": {"tool_calls": [{"index": index, "function": function}]}}]}


def test_streaming_tool_call_chunks_are_joined(fake_server):
    fake_server.state["content_type"] = "text/event-stream"
    fake_server.state["body"] = sse_body(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c9",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        tool_delta(0, arguments='{"path":'),
        tool_delta(0, arguments=' "x.txt", "content": "hi"}'),
        {"choices": [{"delta": {"content": "working"}}]},
    )

    client = LLMClient(fake_server.url, "key", "model")
    deltas = []
    message = client.chat(
        [ChatMessage(role="user", content="hi")], stream=True, on_delta=deltas.append
    )

    assert deltas == ["working"]
    assert message.tool_calls[0].name == "write_file"
    assert message.tool_calls[0].arguments == {"path": "x.txt", "content": "hi"}


def test_stream_retries_without_stream_options(fake_server):
    fake_server.state["fail_first"] = True
    fake_server.state["content_type"] = "text/event-stream"
    fake_server.state["body"] = sse_body({"choices": [{"delta": {"content": "hi"}}]})

    client = LLMClient(fake_server.url, "key", "model")
    message = client.chat([ChatMessage(role="user", content="go")], stream=True)

    assert message.content == "hi"
    assert fake_server.state["requests"] == 2  # first attempt failed, retry succeeded


def test_http_errors_become_llm_errors(fake_server):
    fake_server.state["status"] = 401
    fake_server.state["body"] = b'{"error": {"message": "bad key"}}'

    client = LLMClient(fake_server.url, "key", "model")
    with pytest.raises(LLMError, match="401"):
        client.chat([ChatMessage(role="user", content="hi")], stream=False)
