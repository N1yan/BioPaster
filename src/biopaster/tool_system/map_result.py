
from ast import List
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

def map_tool_result(tool_result: ToolResult) -> ToolResult:
    content: list[dict] = []
    for output in tool_result.output:
        output_type = output.get("type", "text")
        if output_type == "text":
            content.append({
                "type": "text",
                "text": str(output.get("content", ""))
            })
        if output_type == "image":
            info = [f"{k}: {output[k]}" for k in output.keys() if k not in ["type", "media_type", "content"]]
            if info:
                content.append({
                    "type": "text",
                    "text": "\n".join(info)
                })
            content.append(_image_block(output.get("content", ""), output.get("media_type", "")))
    return ToolResult(
        name=tool_result.name,
        output=content,
        is_error=tool_result.is_error,
    )
    
    
        
def map_tool_result_gemma(tool_result: ToolResult) -> List:
    texts = []
    images = []
    for output in tool_result.output:
        output_type = output.get("type", "text")
        if output_type == "text":
            texts.append({
                "type": "text",
                "text": str(output.get("content", ""))
            })
        if output_type == "image":
            images.append(_image_block(output.get("content", ""), output.get("media_type", "")))
    if images:
        texts.append({
            "type": "text",
            "text": f"Read returned {len(images)} image(s).",
        })
    if not texts:
        texts = [{"type": "text", "text": "[empty tool result]"}]
        
    return [*images, ToolResult(
        name=tool_result.name,
        output=texts,
        is_error=tool_result.is_error,
    )]
    
