from errander.llm import ToolCall
from errander.tools import MAX_OUTPUT_CHARS, ToolBox


def call(name, **arguments):
    return ToolCall(id="t", name=name, arguments=arguments)


def test_write_and_read_roundtrip(tmp_path):
    box = ToolBox(tmp_path)
    assert "created" in box.execute(call("write_file", path="notes/a.txt", content="hello"))
    out = box.execute(call("read_file", path="notes/a.txt"))
    assert "1 | hello" in out


def test_list_dir_shows_entries(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    out = ToolBox(tmp_path).execute(call("list_dir", path="."))
    assert "dir   src" in out
    assert "file  README.md" in out


def test_read_file_is_windowed(tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    out = ToolBox(tmp_path).execute(call("read_file", path="f.txt", start=4, end=6))
    assert "    4 | line4" in out
    assert "line3" not in out
    assert "more lines" in out


def test_paths_outside_workspace_are_blocked(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    box = ToolBox(workdir)
    for escape in ("../secret.txt", str(tmp_path / "secret.txt")):
        out = box.execute(call("read_file", path=escape))
        assert out.startswith("error:"), out
        assert "outside the workspace" in out


def test_search_files_finds_and_skips_junk(tmp_path):
    (tmp_path / "a.py").write_text("x = 1  # TODO: fix\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("TODO in git metadata\n")
    box = ToolBox(tmp_path)

    hits = box.execute(call("search_files", query="TODO"))
    assert "a.py:1:" in hits
    assert ".git" not in hits

    misses = box.execute(call("search_files", query="nonexistent-marker"))
    assert misses.startswith("no matches")


def test_run_command_captures_output(tmp_path):
    box = ToolBox(tmp_path, confirm_run=lambda command: True)
    out = box.execute(call("run_command", command="echo errander-ok"))
    assert out.startswith("exit code 0")
    assert "errander-ok" in out


def test_run_command_respects_decline(tmp_path):
    box = ToolBox(tmp_path, confirm_run=lambda command: False)
    out = box.execute(call("run_command", command="echo should-not-run"))
    assert "declined" in out


def test_oversized_tool_output_is_truncated(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (MAX_OUTPUT_CHARS + 100))
    out = ToolBox(tmp_path).execute(call("read_file", path="big.txt", end=10**9))
    assert "truncated" in out


def test_unknown_tool_is_reported(tmp_path):
    out = ToolBox(tmp_path).execute(call("teleport", where="moon"))
    assert "unknown tool" in out


def test_sensitive_files_are_refused(tmp_path):
    (tmp_path / ".env").write_text("KEY=1")
    (tmp_path / ".env.example").write_text("KEY=")
    box = ToolBox(tmp_path)
    assert box.execute(call("read_file", path=".env")).startswith("blocked")
    assert box.execute(call("write_file", path=".env", content="x")).startswith("blocked")
    assert "KEY=" in box.execute(call("read_file", path=".env.example"))  # docs are fine


def test_allow_secrets_lifts_the_block(tmp_path):
    (tmp_path / ".env").write_text("KEY=1")
    box = ToolBox(tmp_path, allow_secrets=True)
    assert "KEY=1" in box.execute(call("read_file", path=".env"))


def test_search_skips_sensitive_files(tmp_path):
    (tmp_path / ".env").write_text("TODO in env\n")
    (tmp_path / "a.py").write_text("# TODO ok\n")
    hits = ToolBox(tmp_path).execute(call("search_files", query="TODO"))
    assert "a.py" in hits
    assert ".env" not in hits


def test_dangerous_commands_are_hard_blocked_even_with_auto_approval(tmp_path):
    approved = []
    box = ToolBox(tmp_path, confirm_run=lambda c: approved.append(c) or True)
    out = box.execute(call("run_command", command="curl http://example.com"))
    assert out.startswith("blocked")
    assert approved == []  # never even reached the human


def test_new_files_need_no_write_confirmation(tmp_path):
    asked = []
    box = ToolBox(tmp_path, confirm_write=lambda p, c: asked.append(p) or True)
    assert "created" in box.execute(call("write_file", path="new.txt", content="hi"))
    assert asked == []


def test_overwrite_asks_and_decline_leaves_file_untouched(tmp_path):
    (tmp_path / "f.txt").write_text("old")
    asked = []
    box = ToolBox(tmp_path, confirm_write=lambda p, c: asked.append(p) or False)
    out = box.execute(call("write_file", path="f.txt", content="new"))
    assert "declined" in out
    assert asked == ["f.txt"]
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "old"
