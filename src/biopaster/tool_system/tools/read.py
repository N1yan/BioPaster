from __future__ import annotations

import base64
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image

from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext

from ..errors import ToolInputError
from ..permissions import (
    PermissionRequest,
    PermissionRule,
    PermissionTarget,
)

# -----image----------------------
IMAGE_MAX_WIDTH = 2000
IMAGE_MAX_HEIGHT = 2000
IMAGE_TARGET_RAW_SIZE = int(5 * 1024 * 1024 * 3 / 4)
IMAGE_MAX_TOKENS = 20_000
_JPEG_QUALITIES = (80, 60, 40, 20)


def _norm_fmt(fmt: str | None) -> str:
    fmt = (fmt or "png").lower()
    return "jpeg" if fmt == "jpg" else fmt


def _to_bytes(image: Image.Image, fmt: str, **save_kw: Any) -> bytes:
    fmt = _norm_fmt(fmt)
    work = image
    if fmt == "jpeg" and work.mode in {"RGBA", "P", "LA"}:
        work = work.convert("RGB")
    buf = io.BytesIO()
    pil_fmt = "JPEG" if fmt == "jpeg" else fmt.upper()
    work.save(buf, format=pil_fmt, **save_kw)
    return buf.getvalue()


def _fit(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    if width <= max_w and height <= max_h:
        return width, height
    scale = min(max_w / width, max_h / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _estimate_tokens(raw: bytes) -> int:
    # Claude Code: ceil(base64_length * 0.125)
    b64_len = math.ceil(len(raw) / 3) * 4
    return math.ceil(b64_len * 0.125)


def _jpeg_under(image: Image.Image, max_bytes: int) -> bytes | None:
    for quality in _JPEG_QUALITIES:
        raw = _to_bytes(image, "jpeg", quality=quality, optimize=True)
        if len(raw) <= max_bytes:
            return raw
    return None


def _maybe_resize_and_downsample(raw: bytes) -> tuple[bytes, str]:
    image = Image.open(io.BytesIO(raw))
    image.load()
    fmt = _norm_fmt(image.format)
    width, height = image.size

    if len(raw) <= IMAGE_TARGET_RAW_SIZE and width <= IMAGE_MAX_WIDTH and height <= IMAGE_MAX_HEIGHT:
        return raw, f"image/{fmt}"

    if width <= IMAGE_MAX_WIDTH and height <= IMAGE_MAX_HEIGHT:
        if fmt == "png":
            png = _to_bytes(image, "png", optimize=True, compress_level=9)
            if len(png) <= IMAGE_TARGET_RAW_SIZE:
                return png, "image/png"
        jpeg = _jpeg_under(image, IMAGE_TARGET_RAW_SIZE)
        if jpeg is not None:
            return jpeg, "image/jpeg"

    new_w, new_h = _fit(width, height, IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT)
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    out = _to_bytes(resized, fmt)
    if len(out) <= IMAGE_TARGET_RAW_SIZE:
        return out, f"image/{fmt}"
    if fmt == "png":
        png = _to_bytes(resized, "png", optimize=True, compress_level=9)
        if len(png) <= IMAGE_TARGET_RAW_SIZE:
            return png, "image/png"
    jpeg = _jpeg_under(resized, IMAGE_TARGET_RAW_SIZE)
    if jpeg is not None:
        return jpeg, "image/jpeg"

    small_w = min(new_w, 1000)
    small_h = max(1, round(new_h * small_w / max(new_w, 1)))
    smaller = resized.resize((small_w, small_h), Image.Resampling.LANCZOS)
    return _to_bytes(smaller, "jpeg", quality=20, optimize=True), "image/jpeg"


def _compress_to_token_limit(raw: bytes, max_tokens: int) -> tuple[bytes, str]:
    max_bytes = int((max_tokens / 0.125) * 0.75)
    image = Image.open(io.BytesIO(raw))
    image.load()
    width, height = image.size
    for side in (2000, 1600, 1200, 800, 600, 400):
        w, h = _fit(width, height, side, side)
        trial = image if (w, h) == (width, height) else image.resize((w, h), Image.Resampling.LANCZOS)
        jpeg = _jpeg_under(trial, max_bytes)
        if jpeg is not None:
            return jpeg, "image/jpeg"
    tiny = image.resize(_fit(width, height, 400, 400), Image.Resampling.LANCZOS)
    return _to_bytes(tiny, "jpeg", quality=20, optimize=True), "image/jpeg"


def _read_image(path: Path) -> ToolResult:
    raw = path.read_bytes()
    if not raw:
        return ToolResult(
            name="Read",
            output=[{"type": "text", 
                     "content": f"[error] image file is empty: {path}",
                     "filePath": str(path)}],
            is_error=True,
        )
    try:
        buf, media = _maybe_resize_and_downsample(raw)
    except Exception as exc:
        return ToolResult(
            name="Read",
            output=[{"type": "text", 
                     "content": f"[error] failed to read image: {exc}",
                     "filePath": str(path)}],
            is_error=True,
        )
    if _estimate_tokens(buf) > IMAGE_MAX_TOKENS:
        try:
            buf, media = _compress_to_token_limit(raw, IMAGE_MAX_TOKENS)
        except Exception as exc:
            return ToolResult(
                name="Read",
                output=[{"type": "text", 
                         "content": f"[error] failed to compress image: {exc}",
                         "filePath": str(path)}],
                is_error=True,
            )
    encoded = base64.b64encode(buf).decode("ascii")
    return ToolResult(
        name="Read",
        output=[{
            "type": "image",
            "media_type": media,
            "content": encoded,
            "originalSize": len(raw),
            "filePath": str(path),
            }]
    )


# -----pdf----------------------
# Render pages to JPEG (capped like images) and extract selectable text.
PDF_MAX_EXTRACT_SIZE = 100 * 1024 * 1024
PDF_MAX_PAGES_PER_READ = 10
PDF_RENDER_DPI = 100
_PDF_PAGES_RE = re.compile(r"^(\d+)(?:-(\d+)?)?$")

def _pdf_error(message: str, path: Path) -> ToolResult:
    return ToolResult(name="Read", 
                      output=[{"type": "text", 
                               "content": f"[error] {message}",
                               "filePath": str(path)}],
                      is_error=True,
                      )

def _parse_pdf_pages(pages: str) -> tuple[int, int] | None:
    match = _PDF_PAGES_RE.fullmatch(pages.strip())
    if not match:
        return None
    first = int(match.group(1))
    if first < 1:
        return None
    if match.group(2) is None and "-" not in pages.strip():
        return first, first
    if match.group(2) is None:
        return None
    last = int(match.group(2))
    if last < first:
        return None
    return first, last

def _pdf_page_text(page: Any) -> str:
    textpage = page.get_textpage()
    try:
        return (textpage.get_text_bounded() or "").strip()
    finally:
        textpage.close()

def _pdf_page_image(page: Any, max_tokens: int) -> tuple[bytes, str]:
    scale = PDF_RENDER_DPI / 72
    bitmap = page.render(scale=scale)
    try:
        jpeg = _to_bytes(bitmap.to_pil().convert("RGB"), "jpeg", quality=85, optimize=True)
    finally:
        bitmap.close()
    buf, media = _maybe_resize_and_downsample(jpeg)
    if _estimate_tokens(buf) > max_tokens:
        buf, media = _compress_to_token_limit(jpeg, max_tokens)
    return buf, media

def _read_pdf(path: Path, pages: str | None = None, mode: str = "text") -> ToolResult:
    import pypdfium2 as pdfium

    raw = path.read_bytes()
    if not raw:
        return _pdf_error(f"PDF file is empty", path)
    if not raw.startswith(b"%PDF-"):
        return _pdf_error(f"File is not a valid PDF (missing %PDF- header)", path)
    if len(raw) > PDF_MAX_EXTRACT_SIZE:
        return _pdf_error(
            f"PDF exceeds maximum size for extraction ({PDF_MAX_EXTRACT_SIZE} bytes)."
            , path)

    try:
        pdf = pdfium.PdfDocument(raw)
    except Exception as exc:
        return _pdf_error(f"failed to open PDF: {exc}", path)

    try:
        page_count = len(pdf)
        pages_arg = (pages or "").strip()
        if pages_arg:
            parsed = _parse_pdf_pages(pages_arg)
            if parsed is None:
                return _pdf_error(
                    f'Invalid pages parameter: "{pages}". Use formats like "1-5" or "3".', 
                    path)
            first, last = parsed
        else:
            if page_count > PDF_MAX_PAGES_PER_READ:
                return _pdf_error(
                    f"This PDF has {page_count} pages, which is too many to read at once. "
                    f'Use pages (e.g. "1-5"). Maximum {PDF_MAX_PAGES_PER_READ} pages per request.'
                    , path)
            first, last = 1, page_count
        if last - first + 1 > PDF_MAX_PAGES_PER_READ:
            return _pdf_error(
                f'Page range exceeds maximum of {PDF_MAX_PAGES_PER_READ} pages per request.'
                , path)
        if first > page_count:
            return _pdf_error(f"PDF has {page_count} pages; cannot start at {first}.", path)
        last = min(last, page_count)
        kind = (mode or "text").strip().lower()
        if kind not in ("text", "image"):
            return _pdf_error('mode must be "text" or "image"', path)

        n_pages = last - first + 1
        page_token_budget = max(2_000, IMAGE_MAX_TOKENS // n_pages) if kind == "image" else 0

        extracted: list[dict[str, Any]] = []
        for index in range(first - 1, last):
            page = pdf[index]
            item: dict[str, Any] = {"page": index + 1,
                                    "type": kind}
            if kind == "text":
                item["content"] = _pdf_page_text(page)
            else:
                buf, media = _pdf_page_image(page, page_token_budget)
                item["media_type"] = media
                item["content"] = base64.b64encode(buf).decode("ascii")
            extracted.append(item)
        
        outputs = []
        for item in extracted:
            outputs.append({
                "type": item["type"],
                "media_type": item.get("media_type", ""),
                "content": item["content"],
                "page": item["page"],
                "filePath": str(path),
                "originalSize": len(raw),
                "pageCount": page_count,
                "count": len(extracted),
            })
        
        return ToolResult(
            name="Read",
            output=outputs
        )
    finally:
        pdf.close()

# -----notebook (ipynb)----------------------
NOTEBOOK_LARGE_OUTPUT_THRESHOLD = 10_000
NOTEBOOK_OUTPUT_TEXT_MAX = 30_000
NOTEBOOK_MAX_JSON_BYTES = 256 * 1024

def _process_output_text(text: Any) -> str:
    if not text:
        return ""
    raw = "".join(text) if isinstance(text, list) else str(text)
    if len(raw) <= NOTEBOOK_OUTPUT_TEXT_MAX:
        return raw
    remaining_lines = raw[NOTEBOOK_OUTPUT_TEXT_MAX:].count("\n") + 1
    return f"{raw[:NOTEBOOK_OUTPUT_TEXT_MAX]}\n\n... [{remaining_lines} lines truncated] ..."


def _extract_notebook_image(data: dict[str, Any] | None) -> dict[str, str] | None:
    if not data:
        return None
    for mime in ("image/png", "image/jpeg"):
        value = data.get(mime)
        if isinstance(value, str) and value.strip():
            raw = base64.b64decode(re.sub(r"\s+", "", value))
            buf, media = _compress_to_token_limit(raw, IMAGE_MAX_TOKENS)
            return {
                "base64": base64.b64encode(buf).decode("ascii"),
                "type": media,
            }
    return None


def _process_notebook_output(output: dict[str, Any]) -> dict[str, Any]:
    kind = output.get("output_type")
    if kind == "stream":
        return {"output_type": kind, "text": _process_output_text(output.get("text"))}
    if kind in ("execute_result", "display_data"):
        data = output.get("data") if isinstance(output.get("data"), dict) else None
        result: dict[str, Any] = {
            "output_type": kind,
            "text": _process_output_text(data.get("text/plain") if data else None),
        }
        image = _extract_notebook_image(data)
        if image:
            result["image"] = image
        return result
    if kind == "error":
        traceback = output.get("traceback") or []
        joined = "\n".join(traceback) if isinstance(traceback, list) else str(traceback)
        ename = output.get("ename") or ""
        evalue = output.get("evalue") or ""
        return {
            "output_type": kind,
            "text": _process_output_text(f"{ename}: {evalue}\n{joined}"),
        }
    return {"output_type": kind or "unknown", "text": ""}

def _is_large_notebook_outputs(outputs: list[dict[str, Any]]) -> bool:
    size = 0
    for item in outputs:
        size += len(item.get("text") or "")
        image = item.get("image")
        if image:
            size += len(image.get("base64") or "")
        if size > NOTEBOOK_LARGE_OUTPUT_THRESHOLD:
            return True
    return False

_CELL_MAGIC_LANGUAGE = {
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "r": "r",
    "sql": "sql",
    "javascript": "javascript",
    "js": "javascript",
    "html": "html",
    "latex": "latex",
    "perl": "perl",
    "ruby": "ruby",
    "python": "python",
}

def _cell_language(cell: dict[str, Any], source: str, fallback: str) -> str:
    meta = cell.get("metadata") if isinstance(cell.get("metadata"), dict) else {}
    vscode = meta.get("vscode") if isinstance(meta.get("vscode"), dict) else {}
    dotnet = (
        meta.get("dotnet_interactive")
        if isinstance(meta.get("dotnet_interactive"), dict)
        else {}
    )
    for value in (
        meta.get("language"),
        vscode.get("languageId"),
        dotnet.get("language"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("%%"):
            magic = stripped[2:].split(None, 1)[0].lower()
            return _CELL_MAGIC_LANGUAGE.get(magic, magic)
        break
    return fallback

def _process_notebook_cell(
    cell: dict[str, Any],
    index: int,
    code_language: str,
    include_large_outputs: bool,
) -> dict[str, Any]:
    source = cell.get("source") or ""
    if isinstance(source, list):
        source = "".join(source)
    cell_type = cell.get("cell_type") or "code"
    processed: dict[str, Any] = {
        "index": index,
        "cellType": cell_type,
        "source": source,
    }
    native_id = cell.get("id")
    if native_id:
        processed["id"] = native_id
    if cell_type == "code":
        processed["language"] = _cell_language(cell, source, code_language)
        count = cell.get("execution_count")
        if count:
            processed["execution_count"] = count
        raw_outputs = cell.get("outputs") or []
        if raw_outputs:
            outputs = [_process_notebook_output(o) for o in raw_outputs if isinstance(o, dict)]
            if not include_large_outputs and _is_large_notebook_outputs(outputs):
                processed["outputs"] = [
                    {
                        "output_type": "stream",
                        "text": (
                            "Outputs are too large to include. "
                            f"Read cells[{index}].outputs from the .ipynb with jq or Python."
                        ),
                    }
                ]
            else:
                processed["outputs"] = outputs
    return processed

def _notebook_blocks(
    processed: list[dict[str, Any]],
    path: Path,
    *,
    cell_count: int,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{
        "type": "text",
        "content": f"{path}  cells {offset}-{offset + len(processed) - 1} / {cell_count}  limit={limit}",
    }]
    for cell in processed:
        index = cell.get("index")
        cell_type = cell.get("cellType") or "code"
        header = f"cell {index} ({cell_type})"
        if cell.get("language"):
            header += f" {cell['language']}"
        source = cell.get("source") or ""
        blocks.append({
            "type": "text",
            "content": f"{header}\n{source}",
            "cell": index,
        })
        for out in cell.get("outputs") or []:
            if not isinstance(out, dict):
                continue
            text = out.get("text")
            if text:
                blocks.append({"type": "text", 
                               "content": str(text), 
                               "cell": index})
            image = out.get("image")
            if isinstance(image, dict) and image.get("base64"):
                blocks.append({
                    "type": "image",
                    "media_type": image.get("type") or "image/png",
                    "content": image["base64"],
                    "filePath": str(path),
                    "cell": index,
                })
    return blocks


def _read_notebook(path: Path, offset: int = 1, limit: int = 200) -> ToolResult:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        return ToolResult(
            name="Read",
            output=[{"type": "text", 
                     "content": f"[error] Not a valid Jupyter notebook: {path}",
                     "filePath": str(path)}],
            is_error=True,
        )

    meta = notebook.get("metadata") or {}
    lang_info = meta.get("language_info") or {}
    language = lang_info.get("name") or "python"
    cells: list[dict[str, Any]] = notebook["cells"]
    start = max(offset - 1, 0)
    stop = start + limit

    if start >= len(cells):
        return ToolResult(
            name="Read",
            output=[{
                "type": "text",
                "content": f"[error] Notebook has {len(cells)} cells; cannot start at offset {offset}.",
                "filePath": str(path)}],
            is_error=True,
        )

    processed = [
        _process_notebook_cell(cell, i, language, include_large_outputs=False)
        for i, cell in enumerate(cells[start:stop], start=start)
        if isinstance(cell, dict)
    ]

    payload_bytes = len(json.dumps(processed, ensure_ascii=False).encode("utf-8"))
    if payload_bytes > NOTEBOOK_MAX_JSON_BYTES:
        return ToolResult(
            name="Read",
            output=[{"type": "text", 
                     "content": f"[error] Notebook content ({payload_bytes} bytes) exceeds maximum allowed size ({NOTEBOOK_MAX_JSON_BYTES} bytes).",
                     "filePath": str(path)}],
            is_error=True,
        )

    return ToolResult(
        name="Read",
        output=_notebook_blocks(
            processed,
            path,
            cell_count=len(cells),
            offset=offset,
            limit=limit,
        ),
    )

# -----text----------------------
# Port of Claude Code FileReadTool text path + readFileInRange / addLineNumbers.

TEXT_MAX_BYTES = 256 * 1024
TEXT_FAST_PATH_MAX_BYTES = 10 * 1024 * 1024


def _add_line_numbers(content: str, start_line: int) -> str:
    if not content:
        return ""
    numbered: list[str] = []
    for i, line in enumerate(content.split("\n")):
        num = str(i + start_line)
        numbered.append(f"{num}→{line}" if len(num) >= 6 else f"{num:>6}→{line}")
    return "\n".join(numbered)

def _normalize_text(raw: str) -> str:
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    return raw.replace("\r\n", "\n").replace("\r", "\n")

def _read_text(path: Path, offset: int = 1, limit: int | None = None) -> ToolResult:
    size = path.stat().st_size
    if limit is None and size > TEXT_MAX_BYTES:
        return ToolResult(
            name="Read",
            output=[{
                "type": "text",
                "content": (
                    f"[error] File content ({size} bytes) exceeds maximum allowed size "
                    f"({TEXT_MAX_BYTES} bytes). Use offset and limit to read a portion."
                ),
                "filePath": str(path),
            }],
            is_error=True,
        )

    start = 0 if offset == 0 else max(offset - 1, 0)
    start_line = offset

    if size < TEXT_FAST_PATH_MAX_BYTES:
        text = _normalize_text(path.read_text(encoding="utf-8-sig"))
        lines = text.split("\n")
        total_lines = len(lines)
        selected = lines[start:] if limit is None else lines[start : start + limit]
    else:
        selected = []
        total_lines = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw_line in handle:
                line = raw_line.replace("\r\n", "\n").replace("\r", "").removesuffix("\n")
                if total_lines >= start and (limit is None or len(selected) < limit):
                    selected.append(line)
                total_lines += 1

    content = "\n".join(selected)
    if not content:
        warning = (
            "[warning] the file exists but the contents are empty."
            if total_lines == 0
            else (
                f"[warning] the file exists but is shorter than the provided offset "
                f"({offset}). The file has {total_lines} lines."
            )
        )
        return ToolResult(
            name="Read",
            output=[{"type": "text", 
                     "content": warning,
                     "filePath": str(path),
                     "numLines": 0,
                     "startLine": offset,
                     "totalLines": total_lines}]
        )

    return ToolResult(
        name="Read",
        output=[{
            "type": "text",
            "content": _add_line_numbers(content, start_line),
            "filePath": str(path),
            "numLines": len(selected),
            "startLine": start_line,
            "totalLines": total_lines,
        }]
    )

def _is_blocked_device_path(path: Path) -> bool:
    p = str(path)
    blocked = {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
    if p in blocked:
        return True
    if p.startswith("/proc/") and (p.endswith("/fd/0") or p.endswith("/fd/1") or p.endswith("/fd/2")):
        return True
    return False

class ReadTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Read",
            description="Read a file from local file system",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file_path": { "type": "string",},
                    "offset": {
                        "type": "integer",
                        "description": "1-based line number to start from. If the file is a ipynb file, it represents the cell index.",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of lines to read from offset. If the file is a ipynb file, it represents the number of cells to read from offset.",
                        "default": 200,
                    },
                    "pages": {
                        "type": "string",
                        "description": (
                            "PDF only. Page or inclusive range, e.g. \"3\" or \"1-5\". "
                            "Max 10 pages per request."
                        ),
                        "default": "1-5",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["text", "image"],
                        "description": (
                            "PDF only. \"text\" extracts selectable text; "
                            "\"image\" renders pages as compressed JPEGs. One or the other, not both."
                        ),
                        "default": "text",
                    },
                },
                "required": ["file_path"]
            },
            is_read_only=True,
            max_result_size_chars=20_000
        )
        
    def prepare_input(
          self,
          tool_input: dict[str, Any],
          context: ToolContext,
    ) -> dict[str, Any]:
        unknown_fields = set(tool_input) - {
            "file_path", "offset", "limit", "pages", "mode",
        }
        if unknown_fields:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown_fields))}"
            )

        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ToolInputError(
                "file_path must be a non-empty string"
            )

        paths = context.resolve_permission_paths(file_path)

        if any(_is_blocked_device_path(path) for path in paths):
            raise ToolInputError("Reading this device path is not allowed.")

        path = paths[-1]
        if not path.is_file():
            raise ToolInputError(
                f"File does not exist or is not a regular file: {path}"
            )

        offset = tool_input.get("offset", 1)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 1
        ):
            raise ToolInputError(
                "offset must be an integer of at least 1"
            )

        limit = tool_input.get("limit", 200)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 2000
        ):
            raise ToolInputError(
                "limit must be an integer between 1 and 2000"
            )

        pages = tool_input.get("pages", "1-5")
        if pages is not None and not isinstance(pages, str):
            raise ToolInputError(
                "pages must be a string when provided"
            )

        mode = tool_input.get("mode", "text")
        if mode not in ("text", "image"):
            raise ToolInputError(
                "mode must be 'text' or 'image'"
            )

        return {
            "file_path": str(path),
            "offset": offset,
            "limit": limit,
            "pages": pages,
            "mode": mode,
            "_permission_paths": paths,
        }
        
    def check_permissions(
          self,
          tool_input: dict[str, Any],
          context: ToolContext,
    ) -> PermissionRequest:
        paths = tool_input["_permission_paths"]

        return PermissionRequest(
            tool_name=self.spec().name,
            tool_use_id=None,
            description=f"Read file: {tool_input['file_path']}",
            targets=tuple(
                PermissionTarget(
                    tool_name="Read",
                    rule_content=str(path),
                )
                for path in paths
            ),
            suggestions=tuple(
                PermissionRule(
                    tool_name="Read",
                    rule_content=str(path),
                )
                for path in paths
            ),
        )
    
    def run(
          self,
          tool_input: dict[str, Any],
          context: ToolContext,
    ) -> ToolResult:
        file_path = Path(tool_input["file_path"])
        offset = tool_input["offset"]
        limit = tool_input["limit"]
        pages = tool_input["pages"]
        mode = tool_input["mode"]

        if not file_path.is_file():
            raise ToolInputError(
                f"File does not exist or is not a regular file: {file_path}"
            )

        suffix = file_path.suffix.lower()

        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            read_result = _read_image(file_path)

        elif suffix == ".pdf":
            read_result = _read_pdf(
                file_path,
                pages=pages,
                mode=mode,
            )

        elif suffix == ".ipynb":
            read_result = _read_notebook(
                file_path,
                offset=offset,
                limit=limit,
            )

        else:
            read_result = _read_text(
                file_path,
                offset=offset,
                limit=limit,
            )

        if not read_result.is_error:
            context.mark_file_read(file_path)

        return read_result
        
    
if __name__ == "__main__":
    tool = ReadTool()
    
    # pdf file
    result = tool.run({"file_path": "paper.pdf",
                       "pages": "1-2",
                       "mode": "text"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:20]}")
            
    result = tool.run({"file_path": "paper.pdf",
                       "pages": "1-2",
                       "mode": "image"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:20]}")
    
    # image file
    result = tool.run({"file_path": "fig5_immunoblot.png"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:20]}")
    
    # notebook file
    result = tool.run({"file_path": "feature_select.ipynb",
                       "offset": 1,
                       "limit": 5})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:20]}")
    
    # text file
    result = tool.run({"file_path": "paperFetch.json",
                       "offset": 1,
                       "limit": 5})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:20]}")
