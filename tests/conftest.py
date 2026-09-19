"""Shared test doubles and a local HTTP server for exercising the LLM client."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class FakeLLM:
    """Scripted stand-in for LLMClient: pops one prepared reply per chat() call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, stream=True, on_delta=None):
        self.calls.append(list(messages))
        return self.replies.pop(0)


@pytest.fixture
def fake_server():
    """A real HTTP server on a random port; each test configures `fake_server.state`."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            state = self.server.errander_state
            state["requests"] += 1
            if state["fail_first"] and state["requests"] == 1:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "stream_options is not supported"}}')
                return
            self.send_response(state["status"])
            self.send_header("Content-Type", state["content_type"])
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.errander_state = {
        "status": 200,
        "content_type": "application/json",
        "body": b"{}",
        "requests": 0,
        "fail_first": False,
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Handle:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        state = server.errander_state

    yield Handle
    server.shutdown()
