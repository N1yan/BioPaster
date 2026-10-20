"""Limit all returned text while preserving sources, images, and full results."""

import json
from types import SimpleNamespace

from biopaster.agent.agent_loop import _process_tool_result
from biopaster.tool_system.protocol import ToolResult
from biopaster.tool_system.tools.read import _read_text
from biopaster.tool_system.tools.copyPaste import _read_tool_result
from biopaster.mcp.result import convert_mcp_result
from mcp.types import CallToolResult, TextContent


def text_size(result):
    return sum(len(block["text"]) for block in result.output if block["type"] == "text")


def process(result, directory):
    context = SimpleNamespace(session_log=SimpleNamespace(directory=directory))
    return _process_tool_result(result, "test-call", context)


def test_long_single_line_read_is_limited_without_saving_another_file(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"content": "x" * 260000}), encoding="utf-8")
    original = source.read_bytes()
    result = _read_text(source, offset=1, limit=40)
    context = SimpleNamespace(session_log=SimpleNamespace(directory=tmp_path / "session"))
    processed = _process_tool_result(result, "read-call", context)
    text = "".join(block["text"] for block in processed.output if block["type"] == "text")
    assert text_size(processed) == 20000
    assert "The combined text output was truncated" in text
    assert str(source) in processed.output[-1]["text"]
    assert "No additional file was saved" in text
    assert "very long lines" in text
    assert "tool_result_saved_path" not in text
    assert not (tmp_path / "session").exists()
    assert source.read_bytes() == original
    assert len(result.output[0]["content"]) > 260000


def test_short_read_and_images_are_unchanged(tmp_path):
    result = ToolResult("Read", [
        {"type": "text", "content": "short", "metadata": {"filePath": "/source"}},
        {"type": "image", "content": "a" * 100, "metadata": {"media_type": "image/png"}},
    ])
    processed = process(result, tmp_path / "results")
    assert processed.output[0]["text"].endswith("\n\nContent:\nshort")
    assert processed.output[1]["source"]["data"] == result.output[1]["content"]
    assert "truncated" not in processed.output[0]["text"]
    assert not (tmp_path / "results").exists()


def test_other_large_results_still_save_full_content(tmp_path):
    result = ToolResult("Example", [{"type": "text", "content": "x" * 30000, "metadata": {}}])
    processed = process(result, tmp_path / "results")
    saved_path = next((tmp_path / "results" / ".tool_results").glob("*.json"))
    assert json.loads(saved_path.read_text())["content"] == "x" * 30000
    assert text_size(processed) == 20000
    assert str(saved_path) in processed.output[-1]["text"]
    assert result.output[0]["content"] == "x" * 30000
    assert result.output[0]["metadata"] == {}


def test_mcp_text_and_structured_content_share_one_limit(tmp_path):
    raw = CallToolResult(
        content=[TextContent(type="text", text="a" * 19000),
                 TextContent(type="text", text="b" * 19000)],
        structuredContent={"value": "c" * 19000},
    )
    result = convert_mcp_result(raw, tool_name="mcp__test__large", server_name="test")
    processed = process(result, tmp_path / "session")
    assert text_size(processed) == 20000
    files = list((tmp_path / "session" / ".tool_results").glob("*.json"))
    assert len(files) == 3
    for path in files:
        assert str(path) in processed.output[-1]["text"]
    saved = [json.loads(path.read_text())["content"] for path in files]
    assert "a" * 19000 in saved and "b" * 19000 in saved
    assert {"value": "c" * 19000} in saved


def test_metadata_is_limited_and_later_images_are_preserved(tmp_path):
    result = ToolResult("executeCode", [
        {"type": "text", "content": "Failed", "metadata": {"evalue": "x" * 50000}},
        {"type": "image", "content": "YWJj", "metadata": {"media_type": "image/png"}},
    ], is_error=True, tool_use_id="original-id")
    processed = process(result, tmp_path / "session")
    assert text_size(processed) == 20000
    assert processed.is_error and processed.tool_use_id == "original-id"
    images = [block for block in processed.output if block["type"] == "image"]
    assert images[0]["source"]["data"] == "YWJj"
    path = next((tmp_path / "session" / ".tool_results").glob("*.json"))
    assert json.loads(path.read_text())["metadata"]["evalue"] == "x" * 50000


def test_webfetch_full_file_remains_readable_by_copypaste(tmp_path):
    result = ToolResult("webFetch", [
        {"type": "text", "content": "Full article", "metadata": {"url": "https://example.org"}},
    ])
    processed = process(result, tmp_path / "session")
    path = next((tmp_path / "session" / ".tool_results").glob("*.json"))
    text, metadata = _read_tool_result(path)
    assert text == "Full article"
    assert metadata == {"url": "https://example.org"}
    assert "truncated" not in processed.output[0]["text"]
