"""agentra.proxy.main.translate_anthropic_to_openai: Anthropic Messages request ->
OpenAI/NIM chat-completions request."""

from agentra.proxy.main import translate_anthropic_to_openai


def test_string_system_prompt_becomes_a_system_message():
    out = translate_anthropic_to_openai({"system": "be terse", "messages": []})
    assert out["messages"][0] == {"role": "system", "content": "be terse"}


def test_block_list_system_prompt_is_concatenated():
    req = {"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "messages": []}
    assert translate_anthropic_to_openai(req)["messages"][0]["content"] == "ab"


def test_tool_result_and_tool_use_blocks_are_flattened_to_text():
    req = {
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "ls", "input": {"path": "/"}}]},
            {"role": "user", "content": [{"type": "tool_result", "content": "file.txt"}]},
        ]
    }
    msgs = translate_anthropic_to_openai(req)["messages"]
    assert "[Tool Call ls]" in msgs[0]["content"]
    assert "[Tool Result]: file.txt" in msgs[1]["content"]


def test_tools_map_to_openai_functions():
    req = {
        "messages": [],
        "tools": [{"name": "ls", "description": "list", "input_schema": {"type": "object"}}],
    }
    tool = translate_anthropic_to_openai(req)["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "ls"
    assert tool["function"]["parameters"] == {"type": "object"}


def test_max_tokens_is_clamped_to_4096():
    out = translate_anthropic_to_openai({"messages": [], "max_tokens": 200000})
    assert out["max_tokens"] == 4096


def test_empty_message_content_is_replaced_with_a_space():
    out = translate_anthropic_to_openai({"messages": [{"role": "user", "content": ""}]})
    assert out["messages"][0]["content"] == " "
