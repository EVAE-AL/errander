"""The agent's hands: five tools, one registry, and a workspace sandbox."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 8_000
MAX_LISTED_ENTRIES = 500
MAX_READ_LINES = 400
MAX_SEARCH_HITS = 100
MAX_FILE_BYTES = 1_000_000
COMMAND_TIMEOUT_SECONDS = 60

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv",
    "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build",
}

# Files whose contents are secrets: refused for read and write, skipped by
# search. .env.example is deliberately not matched — it is documentation.
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.local", ".env.production", ".env.development",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "*.kdbx", "*.keystore", "*.jks",
    "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "credentials*.json", "secrets*",
)

# Commands hard-blocked BEFORE the confirmation prompt: signatures of data
# exfiltration and destructive one-liners. This list is a speed bump, not a
# wall — the human confirmation and the workspace sandbox remain the real gates.
BLOCKED_COMMANDS = (
    (r"\bcurl\b", "network download (curl)"),
    (r"\bwget\b", "network download (wget)"),
    (r"\binvoke-webrequest\b|\biwr\b", "network download (PowerShell)"),
    (r"\binvoke-expression\b|\biex\b", "execute-string (PowerShell)"),
    (r"\bbitsadmin\b", "background download (bitsadmin)"),
    (r"\bcertutil\b.*\b-urlcache\b", "download via certutil"),
    (r"\brm\s+(-[a-z]*r[a-z]*f[a-z]*|-rf\b)", "recursive force delete (rm -rf)"),
    (r"\b(rmdir|rd)\s+/s\b", "recursive delete (cmd)"),
    (r"\bdel\s+/[qs]", "recursive/quiet delete (cmd)"),
    (r"\bremove-item\b.*\b-recurse\b", "recursive delete (PowerShell)"),
    (r"\bformat\s+[a-z]:", "disk format"),
    (r"\bmkfs\b", "disk format (mkfs)"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\breg\s+(add|delete|import)\b", "registry modification"),
    (r"\bshutdown\b", "system shutdown"),
    (r"\|\s*(sh|bash|zsh|powershell|pwsh|python|perl)\b", "piping into a shell or interpreter"),
)

BLOCKED_COMMAND_RULES = [
    (re.compile(pattern, re.IGNORECASE), label) for pattern, label in BLOCKED_COMMANDS
]


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


@dataclass
class Tool:
    """A callable exposed to the model, with an OpenAI-style JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]


class ToolBox:
    """Registry and implementations for the agent's tools.

    File tools are sandboxed: every path is resolved against `workdir` and
    anything escaping it is rejected. Sensitive-looking files (.env, keys,
    …) are refused for read and write unless `allow_secrets` is set.
    `confirm_run` gates shell commands and `confirm_write` gates overwrites
    of existing files behind explicit user approval.
    """

    def __init__(
        self,
        workdir: Path,
        confirm_run: Callable[[str], bool] | None = None,
        confirm_write: Callable[[str, str], bool] | None = None,
        allow_secrets: bool = False,
    ):
        self.workdir = workdir.resolve()
        self.confirm_run = confirm_run
        self.confirm_write = confirm_write
        self.allow_secrets = allow_secrets
        self.tools = [
            Tool(
                name="list_dir",
                description=(
                    "List the entries of a directory (type and size). "
                    "Use path='.' for the workspace root."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path, relative to the workspace root",
                        },
                    },
                },
                fn=self._list_dir,
            ),
            Tool(
                name="read_file",
                description=(
                    "Read a UTF-8 text file with line numbers, one window of up to "
                    f"{MAX_READ_LINES} lines at a time."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path, relative to the workspace root",
                        },
                        "start": {
                            "type": "integer",
                            "description": "First line to read (1-based, default 1)",
                        },
                        "end": {
                            "type": "integer",
                            "description": f"Last line to read (default {MAX_READ_LINES})",
                        },
                    },
                    "required": ["path"],
                },
                fn=self._read_file,
            ),
            Tool(
                name="write_file",
                description=(
                    "Create or overwrite a text file. Parent directories are created as needed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path, relative to the workspace root",
                        },
                        "content": {"type": "string", "description": "Full content to write"},
                    },
                    "required": ["path", "content"],
                },
                fn=self._write_file,
            ),
            Tool(
                name="search_files",
                description="Search text files for a substring; returns 'file:line: text' matches.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to search for"},
                        "path": {
                            "type": "string",
                            "description": "Directory or file to search (default '.')",
                        },
                        "ignore_case": {
                            "type": "boolean",
                            "description": "Case-insensitive (default false)",
                        },
                    },
                    "required": ["query"],
                },
                fn=self._search_files,
            ),
            Tool(
                name="run_command",
                description=(
                    "Run a shell command in the workspace and return exit code and "
                    "output. The user confirms first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The shell command to run"},
                    },
                    "required": ["command"],
                },
                fn=self._run_command,
            ),
        ]
        self._by_name = {tool.name: tool for tool in self.tools}

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI 'tools' payloads for every registered tool."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools
        ]

    def execute(self, call: Any) -> str:
        """Run one tool call; errors are returned as text so the model can self-correct."""
        tool = self._by_name.get(call.name)
        if tool is None:
            return f"error: unknown tool '{call.name}' (available: {', '.join(self._by_name)})"
        try:
            result = tool.fn(**call.arguments)
        except TypeError as exc:
            return f"error: bad arguments for {call.name}: {exc}"
        except PermissionError as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001 — tool failures are fed back to the model on purpose
            return f"error: {type(exc).__name__}: {exc}"
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + f"\n... (truncated at {MAX_OUTPUT_CHARS} chars)"
        return result

    # -- sandbox ---------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        resolved = (self.workdir / path).resolve()
        if resolved != self.workdir and self.workdir not in resolved.parents:
            raise PermissionError(f"'{path}' is outside the workspace ({self.workdir})")
        return resolved

    # -- tool implementations -----------------------------------------------------

    def _list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            return f"not a directory: {path}"
        entries = sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        lines = []
        for entry in entries[:MAX_LISTED_ENTRIES]:
            line = f"{'file' if entry.is_file() else 'dir '}  {entry.name}"
            if entry.is_file():
                with contextlib.suppress(OSError):
                    line += f"  {entry.stat().st_size:>9,} B"
            lines.append(line)
        if len(entries) > MAX_LISTED_ENTRIES:
            lines.append(f"... ({len(entries) - MAX_LISTED_ENTRIES} more entries)")
        return "\n".join(lines) or "(empty directory)"

    def _read_file(self, path: str, start: int = 1, end: int = MAX_READ_LINES) -> str:
        target = self._resolve(path)
        if not self.allow_secrets and _is_sensitive(target):
            return f"blocked: '{path}' looks like a secrets file; reading it is refused"
        if not target.is_file():
            return f"not a file: {path}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        lo = max(start, 1) - 1
        hi = min(end, lo + MAX_READ_LINES, len(lines))
        window = lines[lo:hi]
        if not window:
            return f"(no lines in range {start}..{end}; the file has {len(lines)} lines)"
        body = "\n".join(f"{n:>5} | {text}" for n, text in enumerate(window, start=lo + 1))
        if hi < len(lines):
            body += f"\n... ({len(lines) - hi} more lines; continue with start={hi + 1})"
        return body

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if not self.allow_secrets and _is_sensitive(target):
            return f"blocked: '{path}' looks like a secrets file; writing it is refused"
        overwriting = target.exists()
        if overwriting and self.confirm_write and not self.confirm_write(path, content):
            return f"the user declined to overwrite '{path}'; do not retry without asking"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"{'overwrote' if overwriting else 'created'} {path} ({len(content)} chars)"

    def _search_files(self, query: str, path: str = ".", ignore_case: bool = False) -> str:
        root = self._resolve(path)
        needle = query.lower() if ignore_case else query
        hits: list[str] = []
        scanned = 0

        def walk_files() -> Iterator[Path]:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                )
                for name in sorted(filenames):
                    if not name.startswith("."):
                        yield Path(dirpath) / name

        for full in ([root] if root.is_file() else walk_files()):
            if len(hits) >= MAX_SEARCH_HITS:
                hits.append(f"... (stopped at {MAX_SEARCH_HITS} matches)")
                break
            try:
                if not self.allow_secrets and _is_sensitive(full):
                    continue
                if full.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = full.relative_to(self.workdir).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                haystack = line.lower() if ignore_case else line
                if needle in haystack:
                    hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        break

        if not hits:
            return f"no matches for '{query}' under '{path}' ({scanned} files scanned)"
        return "\n".join(hits)

    def _run_command(self, command: str) -> str:
        for pattern, label in BLOCKED_COMMAND_RULES:
            if pattern.search(command):
                return (
                    f"blocked: '{command}' matches the safety policy ({label}); "
                    "do not retry it or try to work around it"
                )
        if self.confirm_run and not self.confirm_run(command):
            return "the user declined this command; do not retry it without asking them first"
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {COMMAND_TIMEOUT_SECONDS}s"
        output = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        return f"exit code {proc.returncode}\n{output}"
