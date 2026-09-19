from conftest import FakeLLM
from errander.agent import Agent
from errander.llm import ChatMessage, ToolCall
from errander.tools import ToolBox


def test_runs_until_the_model_stops_calling_tools(tmp_path):
    llm = FakeLLM(
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="write_file",
                    arguments={"path": "hi.txt", "content": "hello"},
                )
            ],
        ),
        ChatMessage(role="assistant", content="created hi.txt"),
    )
    agent = Agent(llm, ToolBox(tmp_path), stream=False)

    result = agent.run("create hi.txt containing hello")

    assert (tmp_path / "hi.txt").read_text(encoding="utf-8") == "hello"
    assert result.answer == "created hi.txt"
    assert result.steps == 2
    assert result.tool_calls == 1
    assert llm.calls[-1][-1].role == "tool"  # the tool result was fed back to the model


def test_step_budget_stops_the_loop(tmp_path):
    looping = ChatMessage(
        role="assistant", tool_calls=[ToolCall(id="t", name="list_dir", arguments={})]
    )
    agent = Agent(FakeLLM(*[looping] * 3), ToolBox(tmp_path), max_steps=3, stream=False)

    result = agent.run("keep listing forever")

    assert result.stopped_reason is not None
    assert result.steps == 3
    assert result.answer == ""


def test_unknown_tool_error_goes_back_to_the_model(tmp_path):
    llm = FakeLLM(
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="t", name="deploy_prod", arguments={})],
        ),
        ChatMessage(role="assistant", content="couldn't deploy"),
    )
    agent = Agent(llm, ToolBox(tmp_path), stream=False)

    result = agent.run("deploy to production")

    tool_message = llm.calls[-1][-1]
    assert "unknown tool" in tool_message.content
    assert result.answer == "couldn't deploy"
