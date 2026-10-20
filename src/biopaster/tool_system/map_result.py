
import json
from dataclasses import replace

from .protocol import ToolResult

def _image_block(image_data: str, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": image_data
        }
    }

def _text_block(output: dict) -> dict:
    value = output["content"]
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False)
    )

    metadata = output["metadata"]
    if metadata:
        text = (
            "Metadata:\n"
            + json.dumps(metadata, ensure_ascii=False)
            + "\n\nContent:\n"
            + text
        )

    return {"type": "text", "text": text}


def map_tool_result(
    tool_result: ToolResult,
    *,
    max_text_chars: int | None = None,
) -> ToolResult:
    content: list[dict] = []

    for output in tool_result.output:
        output_type = output.get("type", "text")

        if output_type == "text":
            content.append(_text_block(output))

        elif output_type == "image":
            metadata = output["metadata"]
            info = {
                key: value
                for key, value in metadata.items()
                if key != "media_type"
            }

            if info:
                content.append({
                    "type": "text",
                    "text": json.dumps(info, ensure_ascii=False),
                })

            content.append(
                _image_block(output["content"], metadata["media_type"])
            )

    mapped = replace(tool_result, output=content)
    name = (tool_result.name or "").lower()

    if max_text_chars is None or name == "skill":
        return mapped

    if max_text_chars <= 0:
        raise ValueError("max_text_chars must be positive")

    total_chars = sum(
        len(block["text"])
        for block in content
        if block["type"] == "text"
    )

    if total_chars <= max_text_chars:
        return mapped

    path_key = (
        "filePath" if name == "read"
        else "tool_result_saved_path"
    )
    paths = list(dict.fromkeys(
        output["metadata"][path_key]
        for output in tool_result.output
        if output.get("metadata", {}).get(path_key)
    ))

    notice = "\n\nNotice: The combined text output was truncated.\n"

    if name == "read":
        notice += (
            "No additional file was saved. Read a smaller range "
            "from the original files listed below. For very long lines, "
            "use code to extract only the needed portions.\n"
        )
    elif paths:
        notice += (
            "If the information you need is missing, use the Read tool "
            "to inspect the full results listed below:\n"
        )
    else:
        notice += (
            "Some accompanying text was omitted. "
            "Image content was not truncated.\n"
        )

    notice += "\n".join(paths)

    # Include the complete notice and file paths in the total limit.
    if len(notice) >= max_text_chars:
        raise ValueError(
            "Too many or overly long result paths. "
            "Reduce the scope of this tool call."
        )

    remaining = max_text_chars - len(notice)
    limited: list[dict] = []

    for block in content:
        # Preserve images without truncating them.
        if block["type"] != "text":
            limited.append(block)
            continue

        text = block["text"][:remaining]
        if text:
            limited.append({**block, "text": text})

        remaining -= len(text)

    limited.append({"type": "text", "text": notice})
    return replace(mapped, output=limited)
    
    
        
def map_tool_result_gemma(tool_result: ToolResult) -> list[dict | ToolResult]:
    texts = []
    images = []
    for output in tool_result.output:
        output_type = output.get("type", "text")
        if output_type == "text":
            texts.append(_text_block(output))
        if output_type == "image":
            metadata = output["metadata"]
            info = {key: value for key, value in metadata.items() if key != "media_type"}
            if info:
                texts.append({"type": "text", "text": json.dumps(info, ensure_ascii=False)})
            images.append(_image_block(output["content"], metadata["media_type"]))
    if images:
        texts.append({
            "type": "text",
            "text": f"Read returned {len(images)} image(s).",
        })
    if not texts:
        texts = [{"type": "text", "text": "[empty tool result]"}]
        
    return [*images, replace(tool_result, output=texts)]
    
