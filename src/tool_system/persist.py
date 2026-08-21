from __future__ import annotations

import json
from pathlib import Path

from tool_system.protocol import ToolResult

ALWAYS_SAVE = frozenset({"webfetch", "paperfetch"})


class Persist:
    def __init__(self, save_dir: Path, max_content_length: int = 5000):
        save_dir.mkdir(exist_ok=True)
        self.save_dir = save_dir
        self.max_content_length = max_content_length

    def _truncate_content(self, output: dict) -> tuple[dict, bool]:
        output["content"] = str(output["content"])[:self.max_content_length]
        output["truncated_status"] = "[Is truncated] tool result has been truncated due to the length limit of the tool result."
        # return output
        
    def _write_output(self, output: dict,tool_result_name: str, tool_use_id: str) -> dict:
        path = self.save_dir / f"BioPaster_{tool_result_name}_{tool_use_id}.json"
        path.write_text(
            json.dumps(output, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        output["tool_result_saved_path"] = f"[Saved path]: tool result has been saved to {path}."
        # return output

    def persist(self, tool_result: ToolResult, tool_use_id: str) -> tuple[ToolResult, dict]:
        name = (tool_result.name or "").lower()
        if name == "read":
            return tool_result
        if name in ALWAYS_SAVE:
            for out in tool_result.output:
                if out.get("type") == "text":
                    self._write_output(out, name, tool_use_id)
                    if len(out.get("content", "")) > self.max_content_length:
                        self._truncate_content(out)
                else:
                    self._truncate_content(out)
                    
        else:
            for out in tool_result.output:
                if out.get("type") == "text" and len(out.get("content", "")) > self.max_content_length:
                    self._write_output(out, name, tool_use_id)
                    self._truncate_content(out)

        return tool_result


if __name__ == "__main__":
    tool_result = ToolResult(
        name="test",
        output=[{"type": "text", "content": "a" * 200},
                {"type": "image", "content": "b" * 200}],
        is_error=False,
        tool_use_id="test",
    )
    persist = Persist(Path("./.tool_results"), max_content_length=100)
    tool_result = persist.persist(tool_result, "id123")
    print(tool_result)

