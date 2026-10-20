from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from .map_result import map_tool_result
from .protocol import ToolResult


ALWAYS_SAVE = frozenset({"webfetch", "paperfetch"})


class Persist:
    def __init__(self, save_dir: Path, max_content_length: int = 5000):
        self.save_dir = save_dir
        self.max_content_length = max_content_length

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    def _write_output(
        self,
        output: dict,
        tool_result_name: str,
        tool_use_id: str,
        block_index: int,
    ) -> Path:
        suffix = "" if block_index == 0 else f"_{block_index + 1}"
        self.save_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.save_dir / f"BioPaster_{tool_result_name}_{tool_use_id}{suffix}.json"
        path.write_text(
            json.dumps(output, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def persist(self, tool_result: ToolResult, tool_use_id: str) -> ToolResult:
        name = (tool_result.name or "").lower()
        outputs = deepcopy(tool_result.output)

        if name in {"read", "skill"}:
            return replace(tool_result, output=outputs)

        mapped = map_tool_result(tool_result)
        total_chars = sum(
            len(block["text"])
            for block in mapped.output
            if block["type"] == "text"
        )

        if name in ALWAYS_SAVE or total_chars > self.max_content_length:
            for index, output in enumerate(outputs):
                if output["type"] != "text":
                    continue

                path = self._write_output(
                    tool_result.output[index],
                    name,
                    tool_use_id,
                    index,
                )
                output["metadata"]["tool_result_saved_path"] = str(path)

        return replace(tool_result, output=outputs)

if __name__ == "__main__":
    tool_result = ToolResult(
        name="test",
        output=[{"type": "text", "content": "a" * 200, "metadata": {}},
                {"type": "image", "content": "Yg==", "metadata": {"media_type": "image/png"}}],
        is_error=False,
        tool_use_id="test",
    )
    persist = Persist(Path("./.tool_results"), max_content_length=100)
    tool_result = persist.persist(tool_result, "id123")
    print(tool_result)
