from __future__ import annotations

import mimetypes
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tool_system.protocol import ToolResult
from tool_system.registry import ToolSpec

USER_AGENT = "BioPaster/1.0"
TIMEOUT = 60


_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}


def _is_html(content_type: str, content: bytes = b"") -> bool:
    if (content_type or "").split(";")[0].strip() in _HTML_TYPES:
        return True
    head = content.lstrip()[:64].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _wants_html(url: str, filename: str | None) -> bool:
    name = (filename or "").strip() or unquote(urlparse(url).path)
    return Path(name).suffix.lower() in _HTML_SUFFIXES


def _ext_from_type(content_type: str) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    return ".jpg" if guessed == ".jpe" else guessed


def _filename_from_disposition(disposition: str) -> str:
    if not disposition:
        return ""
    if match := re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;]+)", disposition, re.I):
        return unquote(match.group(1).strip().strip('"'))
    if match := re.search(r'filename="([^"]+)"', disposition, re.I):
        return match.group(1)
    if match := re.search(r"filename=([^;]+)", disposition, re.I):
        return unquote(match.group(1).strip().strip('"'))
    return ""


def _name_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if name and (Path(name).suffix or re.search(r"\d", name)):
        return name
    return ""


def _anon_name() -> str:
    return datetime.now().strftime("download_%Y%m%d_%H%M%S_%f")


def _safe_filename(url: str, filename: str | None, content_type: str, disposition: str) -> str:
    raw = (
        (filename or "").strip()
        or _name_from_url(url)
        or _filename_from_disposition(disposition)
        or _anon_name()
    )
    raw = re.sub(r"[^\w.\-]+", "_", raw).strip("._") or _anon_name()
    if not Path(raw).suffix:
        raw += _ext_from_type(content_type) or ".bin"
    return raw


def download_file(
    *,
    url: str | None = None,
    save_dir: str | None = None,
    filename: str | None = None,
) -> list[dict[str, Any]]:
    url = (url or "").strip()
    if not url:
        return [{
            "type": "text",
            "content": "[error] No URL provided",
        }]
    if not re.match(r"^(https?|ftp)://", url, re.I):
        return [{
            "type": "text",
            "content": "[error] URL must be http, https, or ftp",
        }]

    dest_dir = Path(save_dir or os.getcwd())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            final_url = resp.geturl()
            content_type = (resp.headers.get_content_type() or "").lower()
            disposition = resp.headers.get("Content-Disposition") or ""
            if not _wants_html(url, filename) and _is_html(content_type):
                return [{
                    "type": "text",
                    "content": "[error] got HTML instead of a file",
                    "url": final_url,
                    "content_type": content_type,
                }]
            dest_path = dest_dir / _safe_filename(url, filename, content_type, disposition)
            if dest_path.exists():
                return [{
                    "type": "text",
                    "content": f"[error] file already exists: {dest_path}",
                    "path": str(dest_path.resolve()),
                }]
            content = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        return [{
            "type": "text",
            "content": "[error] failed to download file",
            "url": url,
            "error": str(exc),
        }]

    if not _wants_html(url, filename) and _is_html(content_type, content):
        return [{
            "type": "text",
            "content": "[error] got HTML instead of a file",
            "url": final_url,
            "content_type": content_type,
            "bytes": len(content),
        }]

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)
    return [{
        "type": "text",
        "content": "File downloaded successfully",
        "url": final_url,
        "path": str(dest_path.resolve()),
        "bytes": len(content),
        "content_type": content_type,
    }]

class FileDownloadTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="fileDownload",
            description=(
                "Download a file from a URL and save it. Works for PDF, images, xlsx/csv, "
                "docx, zip, and other formats. Pass any file URL."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string", "description": "Direct file URL (http, https, or ftp)."},
                    "save_dir": {
                        "type": "string",
                        "description": "Directory to save the file. "
                        "If not provided, the file will be saved in the current working directory.",
                    },
                    "filename": {"type": "string"},
                },
                "required": ["url"],
            },
            is_read_only=False,
            max_result_size_chars=8_000,
        )

    def run(self, tool_input: dict[str, Any]) -> ToolResult:
        payload = download_file(
            url=tool_input.get("url"),
            save_dir=tool_input.get("save_dir"),
            filename=tool_input.get("filename"),
        )
        print("\033[33m[fileDownloadTool]\033[0m")
        return ToolResult(
            name="fileDownload",
            output=payload,
        )


if __name__ == "__main__":
    tool = fileDownloadTool()
    url = "https://filesamples.com/samples/document/xlsx/sample1.xlsx"
    result = tool.run({"url": url})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")