from pathlib import Path
from typing import Any

from tool_system.protocol import ToolResult
from tool_system.registry import ToolSpec
from tool_system.tools.papers.pdf_metadata import extract_pdf_reference_info

import json
import re


def _read_pdf_text(file: Path) -> str:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(file)
    try:
        pages = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append((textpage.get_text_bounded() or "").strip())
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        pdf.close()
    return "\n\n".join(page for page in pages if page)


def _find_all(text: str, pattern: str) -> list[int]:
    indexes = []
    start = 0
    while True:
        index = text.find(pattern, start)
        if index == -1:
            return indexes
        indexes.append(index)
        start = index + 1

def _read_tool_result(file: Path) -> str:
    result = json.loads(file.read_text(encoding="utf-8"))
    content = result.get("content", "")
    metadata = result.get("metadata", {})
    return content, metadata

class CopyPasteTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="copyPaste",
            description="Copy text from the local files and paste it to the another file.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_file": {
                        "type": "string",
                        "description": "The path to the source file.",
                    },
                    "target_file": {
                        "type": "string",
                        "description": "The path to the target file.",
                    },
                    "start_string": {
                        "type": "string",
                        "description": ("The start string to copy from the source file. "
                                        "Note that the start string would be included in the copied content."),
                    },
                    "end_string": {
                        "type": "string",
                        "description": ("The end string to copy from the source file. "
                                        "Note that the end string would be included in the copied content."),
                    },
                    "usage":{
                        "type": "string",
                        "description": "Natural language description of the usage of the copied content.",
                    }
                },
                "required": ["source_file", "target_file", "start_string", "end_string", "usage"],
            },
        )

    def run(self, tool_input: dict[str, Any]) -> ToolResult:
        source_file = tool_input.get("source_file")
        target_file = tool_input.get("target_file")
        start_string = tool_input.get("start_string")
        end_string = tool_input.get("end_string")
        usage = tool_input.get("usage")

        fields = ["source_file", "target_file", "start_string", "end_string", "usage"]
        values = [source_file, target_file, start_string, end_string, usage]
        missing_fields = [field for field, value in zip(fields, values) if value is None]
        if missing_fields:
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] Missing required fields: {', '.join(missing_fields)}",
                }],
                is_error=True,
            )

        source_path = Path(source_file)
        target_path = Path(target_file)
        if not source_path.exists():
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] Source file not found: {source_path}",
                }],
                is_error=True,
            )
        if target_path.exists():
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] Target file already exists: {target_path}",
                }],
                is_error=True,
            )

        tool_result_pattern = re.compile(r"BioPaster_(?:webfetch|paperfetch)_.*\.json$")
        if source_path.suffix.lower() == ".pdf":
            source_content = _read_pdf_text(source_path)
            metadata = extract_pdf_reference_info(source_path)
        elif tool_result_pattern.match(source_path.name):
            source_content, metadata = _read_tool_result(source_path)
        else:
            source_content = source_path.read_text(encoding="utf-8")
            metadata = {}

        start_indexes = _find_all(source_content, start_string)
        end_indexes = _find_all(source_content, end_string)
        if not start_indexes or not end_indexes:
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] {start_string!r} or {end_string!r} not found",
                }],
            )
        if len(start_indexes) > 1 or len(end_indexes) > 1:
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] multiple {start_string!r} or {end_string!r} found",
                }],
            )

        start_index = start_indexes[0]
        end_index = end_indexes[0] + len(end_string)
        copied = f"#{"-"*20}Copied Content{"-"*20}\n"
        copied += source_content[start_index:end_index] + "\n\n"
        
        print(metadata)
        annotation = f"#{"-"*20}Annotation{"-"*20}\n"
        annotation += f"# URL: {metadata.get('url')}\n" if metadata.get("url", "") else ""
        annotation += f"# Local File: {source_path.name}\n" if source_path.suffix.lower() == ".pdf" else ""
        annotation += f"# Title: {metadata.get('title')}\n" if metadata.get("title", "") else ""
        annotation += f"# Author: {metadata.get('authors')}\n" if metadata.get("authors", "") else ""
        annotation += f"# Publication Date: {metadata.get('publication_dates')}\n" if metadata.get("publication_dates", "") else ""
        annotation += f"# Journal: {metadata.get('journal')}\n" if metadata.get("journal", "") else ""
        annotation += f"# DOI: {metadata.get('doi')}\n" if metadata.get("doi", "") else ""
        annotation += f"# PMID: {metadata.get('pmid')}\n" if metadata.get("pmid", "") else ""
        
        target_path.write_text(annotation + copied, encoding="utf-8")
        print("\033[33m[copyPasteTool]\033[0m")
        return ToolResult(
            name="copyPaste",
            output=[{"type": "text", "content": copied, "metadata": metadata}],
        )


if __name__ == "__main__":
    import pypdfium2 as pdfium

    source_file = Path("/home/yan/test/BioPaster/read_test/paper.pdf")
    start_string = "and"
    end_string = "spatially-resolved transcriptomes"

    pdf = pdfium.PdfDocument(source_file)
    try:
        pages = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append((textpage.get_text_bounded() or "").strip())
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        pdf.close()

    source_content = "\n\n".join(page for page in pages if page)
    print(f"pages={len(pages)} chars={len(source_content)}")
    print("--- preview ---")
    print(source_content[:500])

    start_index = source_content.find(start_string)
    end_index = source_content.find(end_string) + len(end_string)
    if start_index < 0:
        print(f"[error] start_string not found: {start_string!r}")
    elif end_index < 0:
        print(f"[error] end_string not found: {end_string!r}")
    elif end_index <= start_index:
        print("[error] end_string before start_string")
    else:
        copied = source_content[start_index:end_index]
        print("--- copied ---")
        print(copied)
        print(f"... total copied chars={len(copied)}")

    test_pdfs = [
        "./copy_test/paper1.pdf",
        "./copy_test/paper2.pdf",
        "./copy_test/paper3.pdf",
        "./copy_test/paper4.pdf",
        "./copy_test/paper5.pdf",
        "./copy_test/paper6.pdf",
        "./copy_test/paper7.pdf",
    ]
    for pdf_file in test_pdfs:
        print(extract_pdf_reference_info(Path(pdf_file)))
    
    test_json = ["/home/yan/test/BioPaster/.tool_results/BioPaster_paperfetch_toolu_4318243fe6de4136b172331e.json",# paperfetch
                 "/home/yan/test/BioPaster/.tool_results/BioPaster_webfetch_toolu_3698151c009649539e87e47d.json",# webfetch DEseq2
                 "/home/yan/test/BioPaster/.tool_results/BioPaster_webfetch_toolu_ccad203306d24e648fb4972a.json",# webfetch Clawd-Code
                 ]
    content, metadata = _read_tool_result(Path(test_json[0]))
    print(content)
    print(metadata)
    
    json_content = json.loads(Path(test_json[1]).read_text(encoding="utf-8"))
    print(json_content.get("content", ""))
    print(json_content.get("metadata", {}))
    content, metadata = _read_tool_result(Path(test_json[1]))