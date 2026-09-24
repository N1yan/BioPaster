from pathlib import Path
from typing import Any

from ..protocol import ToolResult
from ..registry import ToolSpec
from ..tools.papers.pdf_metadata import extract_pdf_reference_info
from ..context import ToolContext
from ..errors import ToolInputError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget

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

def _read_tool_result(file: Path) -> tuple[str, dict[str, Any]]:
    result = json.loads(file.read_text(encoding="utf-8"))
    content = result.get("content", "")
    metadata = result.get("metadata", {})
    return content, metadata

class CopyPasteTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="copyPaste",
            description=("Copy text or code from the local files and paste it to the another file."
                         "Supports multiple source file formats, including but not limited to JSON, PDF, and TXT."),
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
                        "description": "The path to the target file. And the file name must start with: BioPaster_evidence_",
                    },
                    "start_string": {
                        "type": "string",
                        "description": ("The start string to copy from the source file."
                                        "Note that the start string would be included in the copied content."),
                    },
                    "end_string": {
                        "type": "string",
                        "description": ("The end string to copy from the source file."
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

    def prepare_input(self, tool_input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        unknown = set(tool_input) - {"source_file", "target_file", "start_string", "end_string", "usage"}
        if unknown:
            raise ToolInputError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        for key in ("source_file", "target_file", "start_string", "end_string", "usage"):
            if not isinstance(tool_input.get(key), str) or not tool_input[key]:
                raise ToolInputError(f"{key} must be a non-empty string")
        source_paths = context.resolve_permission_paths(tool_input["source_file"])
        target_paths = context.resolve_permission_paths(tool_input["target_file"])
        if not source_paths[-1].is_file():
            raise ToolInputError(f"Source is not a regular file: {source_paths[-1]}")
        if any(not p.name.startswith("BioPaster_evidence_") for p in target_paths):
            raise ToolInputError("Evidence target names must start with BioPaster_evidence_.")
        if source_paths[-1] == target_paths[-1]:
            raise ToolInputError("Source and target must be different files.")
        if not target_paths[-1].parent.is_dir():
            raise ToolInputError("Target parent directory does not exist.")
        return {**tool_input, "source_file": str(source_paths[-1]),
                "target_file": str(target_paths[-1]),
                "_source_paths": source_paths, "_target_paths": target_paths}

    def check_permissions(self, tool_input, context):
        targets = tuple(PermissionTarget("Read", str(p)) for p in tool_input["_source_paths"])
        targets += tuple(PermissionTarget("Edit", str(p)) for p in tool_input["_target_paths"])
        return PermissionRequest(
            self.spec().name, None,
            f"Copy from {tool_input['source_file']} to {tool_input['target_file']}.",
            targets, tuple(PermissionRule(t.tool_name, t.rule_content) for t in targets),
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        source_path = Path(tool_input["source_file"])
        target_path = Path(tool_input["target_file"])
        start_string = tool_input["start_string"]
        end_string = tool_input["end_string"]

        tool_result_pattern = re.compile(r"BioPaster_(?:webfetch|paperfetch)_.*\.json$")
        if source_path.suffix.lower() == ".pdf":
            source_content = _read_pdf_text(source_path)
            metadata = extract_pdf_reference_info(source_path)
        elif tool_result_pattern.match(source_path.name):
            source_content, metadata = _read_tool_result(source_path)
        else:
            source_content = source_path.read_text(encoding="utf-8")
            metadata = {}

        metadata = {
            **metadata,
            "source_file": str(source_path),
            "target_file": str(target_path),
            "usage": tool_input["usage"],
        }

        start_indexes = _find_all(source_content, start_string)
        end_indexes = _find_all(source_content, end_string)
        if not start_indexes or not end_indexes:
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": f"[error] {start_string!r} or {end_string!r} not found",
                    "metadata": metadata,
                }],
                is_error=True,
            )
        if len(start_indexes) > 1 or len(end_indexes) > 1:
            errors = []
            if len(start_indexes) > 1:
                errors.append(f"[error] multiple {start_string!r} found")
            if len(end_indexes) > 1:
                errors.append(f"[error] multiple {end_string!r} found")
            return ToolResult(
                name="copyPaste",
                output=[{
                    "type": "text",
                    "content": "\n".join(errors),
                    "metadata": metadata,
                }],
                is_error=True,
            )

        start_index = start_indexes[0]
        end_index = end_indexes[0] + len(end_string)
        if end_indexes[0] < start_index:
            raise ToolInputError("end_string occurs before start_string")
        copied = f"#{"-"*20}Copied Content{"-"*20}\n"
        copied += source_content[start_index:end_index] + "\n\n"
        
        annotation = f"#{"-"*20}Annotation{"-"*20}\n"
        annotation += f"# PURPOSE: {metadata.get('usage')}\n" if metadata.get("usage", "") else ""
        annotation += f"# URL: {metadata.get('url')}\n" if metadata.get("url", "") else ""
        annotation += f"# Local Evidence File: {source_path.name}\n" # if source_path.suffix.lower() == ".pdf" else ""
        annotation += f"# Title: {metadata.get('title')}\n" if metadata.get("title", "") else ""
        annotation += f"# Author: {metadata.get('authors')}\n" if metadata.get("authors", "") else ""
        annotation += f"# Publication Date: {metadata.get('publication_dates')}\n" if metadata.get("publication_dates", "") else ""
        annotation += f"# Journal: {metadata.get('journal')}\n" if metadata.get("journal", "") else ""
        annotation += f"# DOI: {metadata.get('doi')}\n" if metadata.get("doi", "") else ""
        annotation += f"# PMID: {metadata.get('pmid')}\n" if metadata.get("pmid", "") else ""
        
        # target_path.write_text(annotation + copied, encoding="utf-8")
        with target_path.open("a", encoding="utf-8") as f:
            f.write(annotation + copied)
        
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
