
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
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    metadata = output["metadata"]
    if metadata:
        text = "Metadata:\n" + json.dumps(metadata, ensure_ascii=False) + "\n\nContent:\n" + text
    return {"type": "text", "text": text}


def map_tool_result(tool_result: ToolResult) -> ToolResult:
    content: list[dict] = []
    for output in tool_result.output:
        output_type = output.get("type", "text")
        if output_type == "text":
            content.append(_text_block(output))
        if output_type == "image":
            metadata = output["metadata"]
            info = {key: value for key, value in metadata.items() if key != "media_type"}
            if info:
                content.append({
                    "type": "text",
                    "text": json.dumps(info, ensure_ascii=False),
                })
            content.append(_image_block(output["content"], metadata["media_type"]))
    return replace(tool_result, output=content)
    
    
        
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
    
