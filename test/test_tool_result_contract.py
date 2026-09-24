"""Offline contracts for tool outputs and their immediate consumers."""

import base64
from copy import deepcopy
import importlib
import io
import json
from email.message import Message as Headers

import nbformat
import pytest
from PIL import Image

from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.defaults import build_default_registry
from biopaster.tool_system.map_result import map_tool_result, map_tool_result_gemma
from biopaster.tool_system.permissions import PermissionAnswer
from biopaster.tool_system.persist import Persist
from biopaster.tool_system.protocol import ToolCall, ToolResult


def tool_module(name):
    return importlib.import_module(f"biopaster.tool_system.tools.{name}")


def assert_blocks(blocks):
    assert blocks
    for block in blocks:
        assert set(block) == {"type", "content", "metadata"}
        assert isinstance(block["metadata"], dict)
        assert block["type"] in {"text", "image"}
        assert isinstance(block["content"], (str, list, dict))
        if block["type"] == "image":
            assert block["metadata"]["media_type"].startswith("image/")
            assert base64.b64decode(block["content"])


@pytest.fixture
def context(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path, notebook_path=tmp_path / "run.ipynb")
    ctx.permission_context.permission_handler = lambda request: PermissionAnswer(True)
    return ctx


@pytest.fixture
def offline_tools(monkeypatch):
    class Search:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results):
            return [{"title": "Tutorial", "href": "https://example.org", "body": "Guide"}]

    monkeypatch.setattr(tool_module("webSearch"), "DDGS", Search)
    monkeypatch.setattr(tool_module("webFetch"), "_extract_url_content_http", lambda url: "Tutorial text")
    paper_search = tool_module("paperSearch")
    monkeypatch.setattr(paper_search, "_SOURCE_FNS", {
        source: lambda *args: {"status": "ok", "results": []}
        for source in paper_search.VALID_SOURCES
    })
    paper_fetch = tool_module("paperFetch")
    monkeypatch.setattr(paper_fetch, "_candidate_batches", lambda *args: [[{"source": "arxiv"}]])
    monkeypatch.setattr(paper_fetch, "_fetch_candidate", lambda candidate: {
        "status": "ok", "text": "Original supporting passage.",
        "source": "arxiv", "url": "https://arxiv.org/html/1706.03762",
    })
    monkeypatch.setattr(paper_fetch, "_verified_metadata", lambda *args: {
        "title": "Example paper", "doi": "10.example/paper",
    })

    class Download(io.BytesIO):
        headers = Headers()
        headers["Content-Type"] = "application/octet-stream"

        def geturl(self):
            return "https://example.org/data.bin"

    monkeypatch.setattr(tool_module("fileDownload").urllib.request, "urlopen",
                        lambda *args, **kwargs: Download(b"downloaded data"))
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(
        "print('done')", outputs=[nbformat.v4.new_output("stream", name="stdout", text="done\n")],
    )])
    monkeypatch.setattr(tool_module("executeCode"), "_run_notebook_cell", lambda **kwargs: notebook)


@pytest.mark.parametrize("name", [
    "webSearch", "webFetch", "paperSearch", "paperFetch", "fileDownload", "Read",
    "copyPaste", "Glob", "Write", "Edit", "executeCode", "Bash",
])
def test_registered_tools_return_content_and_metadata(name, context, offline_tools):
    root = context.workspace_root
    source = root / "source.txt"
    source.write_text("first\nOriginal supporting passage.\nlast", encoding="utf-8")
    context.mark_file_read(source)
    inputs = {
        "webSearch": {"query": "tutorial"},
        "webFetch": {"url": "https://example.org"},
        "paperSearch": {"query": "biology", "sources": ["pubmed"]},
        "paperFetch": {"arxiv_id": "1706.03762"},
        "fileDownload": {"url": "https://example.org/data.bin", "save_dir": str(root)},
        "Read": {"file_path": str(source)},
        "copyPaste": {"source_file": str(source), "target_file": str(root / "BioPaster_evidence_test.txt"),
                      "start_string": "Original", "end_string": "passage.", "usage": "Support a claim"},
        "Glob": {"path": str(root), "pattern": "*.txt"},
        "Write": {"file_path": str(root / "new.txt"), "content": "new text"},
        "Edit": {"file_path": str(source), "old_string": "first", "new_string": "changed"},
        "executeCode": {"command": "print('done')", "language": "python"},
        "Bash": {"command": "printf done"},
    }
    result = build_default_registry().dispatch(ToolCall(name, inputs[name], "contract"), context)
    assert not result.is_error, result.output
    assert result.tool_use_id == "contract"
    assert_blocks(result.output)
    block = result.output[0]
    if name == "Bash":
        assert block["content"] == {"stdout": "done", "stderr": ""}
        assert block["metadata"]["exit_code"] == 0
    elif name == "Glob":
        assert block["content"] == ["source.txt"]
        assert block["metadata"]["file_count"] == 1
    elif name == "Edit":
        assert source.read_text().startswith("changed")
        assert block["metadata"]["file_path"] == str(source)
        assert block["content"]["diff_lines"]
    elif name == "executeCode":
        saved = nbformat.read(context.notebook_path, as_version=4)
        assert saved.cells[0].outputs[0].text == "done\n"
    elif name == "fileDownload":
        assert (root / "data.bin").read_bytes() == b"downloaded data"
        assert block["metadata"]["bytes"] == 15


@pytest.mark.parametrize("status", ["ok", "abstract_only"])
def test_paper_source_survives_json_and_evidence_copy(status, context, offline_tools):
    block = tool_module("paperFetch")._public_result({
        "text": "Original supporting passage.", "status": status,
        "source": "arxiv", "url": "https://example.org/paper",
    }, {"arxiv_id": "1706.03762"}, 50000)
    assert_blocks([block])
    assert block["metadata"]["status"] == status
    assert block["metadata"]["title"] == "Example paper"
    if status == "abstract_only":
        assert "no full-text body" in block["metadata"]["message"]
    source = context.workspace_root / "BioPaster_paperfetch_contract.json"
    source.write_text(json.dumps(block), encoding="utf-8")
    target = context.workspace_root / "BioPaster_evidence_quote.txt"
    result = build_default_registry().dispatch(ToolCall("copyPaste", {
        "source_file": str(source), "target_file": str(target),
        "start_string": "Original", "end_string": "passage.", "usage": "Support a claim",
    }), context)
    assert not result.is_error
    assert_blocks(result.output)
    evidence = target.read_text()
    assert "Original supporting passage." in evidence
    assert "https://example.org/paper" in evidence
    assert "10.example/paper" in evidence
    assert result.output[0]["metadata"]["usage"] == "Support a claim"


@pytest.mark.parametrize("kind", ["paper_input", "paper_missing", "search_input", "download_input", "copy_missing"])
def test_direct_error_results_follow_contract(kind, context, monkeypatch):
    if kind == "paper_input":
        blocks = tool_module("paperFetch").fetch_paper_fulltext()
    elif kind == "paper_missing":
        module = tool_module("paperFetch")
        monkeypatch.setattr(module, "_candidate_batches", lambda *args: [])
        blocks = module.fetch_paper_fulltext(arxiv_id="1706.03762")
    elif kind == "search_input":
        blocks = tool_module("paperSearch").search_papers("")
    elif kind == "download_input":
        blocks = tool_module("fileDownload").download_file(url="invalid")
    else:
        source = context.workspace_root / "source.txt"
        source.write_text("actual text")
        result = build_default_registry().dispatch(ToolCall("copyPaste", {
            "source_file": str(source), "target_file": str(context.workspace_root / "BioPaster_evidence_test"),
            "start_string": "missing", "end_string": "text", "usage": "test",
        }), context)
        assert result.is_error
        blocks = result.output
    assert_blocks(blocks)
    assert blocks[0]["content"].startswith("[error]")


@pytest.mark.parametrize("kind", ["image", "notebook", "pdf"])
def test_read_images_keep_format_and_location(kind, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    reader = tool_module("read")
    if kind == "image":
        path = tmp_path / "figure.png"
        path.write_bytes(buffer.getvalue())
        result = reader._read_image(path)
    elif kind == "notebook":
        path = tmp_path / "figure.ipynb"
        nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("plot()", outputs=[
            nbformat.v4.new_output("display_data", data={"image/png": base64.b64encode(buffer.getvalue()).decode()}),
        ])])
        nbformat.write(nb, path)
        result = reader._read_notebook(path)
    else:
        path = tmp_path / "figure.pdf"
        Image.new("RGB", (8, 8), "white").save(path, format="PDF")
        result = reader._read_pdf(path, pages="1", mode="image")
    assert not result.is_error
    assert_blocks(result.output)
    image = next(block for block in result.output if block["type"] == "image")
    assert image["metadata"]["filePath"] == str(path)
    for mapped in (map_tool_result(result).output, map_tool_result_gemma(result)):
        api_image = next(block for block in mapped if isinstance(block, dict) and block["type"] == "image")
        assert api_image["source"]["media_type"] == image["metadata"]["media_type"]
        assert api_image["source"]["data"] == image["content"]


def test_model_message_keeps_metadata_and_error_flag():
    result = ToolResult("Bash", [{"type": "text", "content": {"stdout": "partial", "stderr": "failed"},
                                "metadata": {"exit_code": 7, "cwd": "/work"}}], True, "call-7")
    mapped = map_tool_result(result)
    assert mapped.is_error and mapped.tool_use_id == "call-7"
    text = mapped.output[0]["text"]
    assert "partial" in text and "exit_code" in text and "/work" in text


def test_execute_output_parser_keeps_image_and_error():
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("example", outputs=[
        nbformat.v4.new_output("display_data", data={"text/plain": "plot", "image/png": "YWJj"}),
        nbformat.v4.new_output("error", ename="ValueError", evalue="bad input", traceback=["ValueError: bad input"]),
    ])])
    blocks, has_error = tool_module("executeCode")._notebook_output_parse(nb)
    assert has_error
    assert_blocks(blocks)
    assert blocks[-1]["content"] == "ValueError: bad input"


def test_persist_always_saves_original_without_mutating_input(tmp_path):
    result = ToolResult("paperFetch", [{
        "type": "text",
        "content": "short paper text",
        "metadata": {"url": "https://example.org/paper", "status": "ok"},
    }], tool_use_id="paper-call")
    original = deepcopy(result)

    persisted = Persist(tmp_path, max_content_length=100).persist(result, "paper-call")

    assert result == original
    assert persisted is not result
    block = persisted.output[0]
    assert block["content"] == "short paper text"
    assert block["metadata"]["url"] == "https://example.org/paper"
    assert "preview_truncated" not in block["metadata"]
    saved_path = tmp_path / "BioPaster_paperfetch_paper-call.json"
    assert block["metadata"]["tool_result_saved_path"] == str(saved_path)
    assert json.loads(saved_path.read_text()) == original.output[0]


def test_persist_serializes_structured_previews_and_uses_unique_paths(tmp_path):
    result = ToolResult("Bash", [
        {
            "type": "text",
            "content": {"stdout": "abcdefghijklmnopqrstuvwxyz", "stderr": ""},
            "metadata": {"exit_code": 0},
        },
        {
            "type": "text",
            "content": ["first long value", "second long value"],
            "metadata": {"kind": "records"},
        },
    ])
    original = deepcopy(result)

    persisted = Persist(tmp_path, max_content_length=20).persist(result, "bash-call")

    assert result == original
    first, second = persisted.output
    assert first["content"] == '{"stdout": "abcdefgh'
    assert second["content"] == '["first long value",'
    for block, total_chars in ((first, 54), (second, 41)):
        assert block["metadata"]["preview_truncated"] is True
        assert block["metadata"]["preview_total_chars"] == total_chars
    first_path = tmp_path / "BioPaster_bash_bash-call.json"
    second_path = tmp_path / "BioPaster_bash_bash-call_2.json"
    assert first["metadata"]["tool_result_saved_path"] == str(first_path)
    assert second["metadata"]["tool_result_saved_path"] == str(second_path)
    assert json.loads(first_path.read_text()) == original.output[0]
    assert json.loads(second_path.read_text()) == original.output[1]


def test_persist_leaves_read_and_image_results_unchanged(tmp_path):
    read_result = ToolResult("Read", [{
        "type": "text", "content": "x" * 100,
        "metadata": {"filePath": "/work/input.txt"},
    }])
    image_result = ToolResult("executeCode", [{
        "type": "image", "content": "YWJj",
        "metadata": {"media_type": "image/png"},
    }])

    persisted_read = Persist(tmp_path, max_content_length=10).persist(read_result, "read-call")
    persisted_image = Persist(tmp_path, max_content_length=10).persist(image_result, "image-call")

    assert persisted_read == read_result
    assert persisted_image == image_result
    assert list(tmp_path.iterdir()) == []
