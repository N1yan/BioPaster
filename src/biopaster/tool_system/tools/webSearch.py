from __future__ import annotations
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext
from ddgs import DDGS
import requests
from bs4 import BeautifulSoup
from typing import Any


def _search_web_duckduckgo(query: str, num_results: int = 5) -> list[dict[str, Any]] | str: 
    """Fallback when ddgs fails (e.g. httpx<0.28 incompatible with ddgs>=9)."""
    results = []
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; BioPaster/1.0)"},
            timeout=30,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.select("a.result__a"):
            title = link.get_text(strip=True)
            url = link.get("href", "")
            description = ""
            result_block = link.find_parent("div", class_="result")
            if result_block:
                snippet = result_block.select_one("a.result__snippet")
                if snippet:
                    description = snippet.get_text(strip=True)
            if title and url:
                results.append({"title": title, "url": url, "description": description})
            if len(results) >= num_results:
                break
        return results
    except Exception as e:
        return f"[error] Failed to search the web: {e}"

from ..errors import ToolInputError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget


class WebSearchTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="webSearch",
            description="Search the web and return top results",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "num_results": {"type": "integer", "default": 5}
                },
                "required": ["query"],
            },
            is_read_only=True,
            max_result_size_chars=20_000
        )
        
    def prepare_input(self, tool_input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        properties = self.spec().input_schema["properties"]
        unknown = set(tool_input) - set(properties)
        if unknown:
            raise ToolInputError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        for key, value in tool_input.items():
            kind = properties[key].get("type")
            if kind == "string" and not isinstance(value, str):
                raise ToolInputError(f"{key} must be a string")
            if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ToolInputError(f"{key} must be an integer")
            if kind == "boolean" and not isinstance(value, bool):
                raise ToolInputError(f"{key} must be a boolean")
            if kind == "array" and not isinstance(value, list):
                raise ToolInputError(f"{key} must be a list")
        prepared = dict(tool_input)
        if not prepared.get("query", "").strip():
            raise ToolInputError("query cannot be empty")
        for key in ("num_results", "max_item", "max_chars"):
            if key in prepared and prepared[key] < 1:
                raise ToolInputError(f"{key} must be positive")
        return prepared

    def check_permissions(self, tool_input, context):
        name = self.spec().name
        return PermissionRequest(
            name, None, f"Retrieve remote content with {name}.",
            (PermissionTarget(name, None),), (PermissionRule(name),),
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        """
        Search the web for the given query.
        """
        query = tool_input.get('query', '')
        if not query:
            return ToolResult(
                name="webSearch",
                output=[{
                    "type": "text",
                    "content": "[error] No query provided",
                    "metadata": {},
                }],
                is_error=True,
            )
        num_results = tool_input.get('num_results', 5)
        results = []
        try:
            with DDGS() as ddgs:
                search_results = ddgs.text(query, max_results=num_results)
                for r in search_results:
                    results.append({
                        "title": r.get("title"),
                        "url": r.get("href"),
                        "description": r.get("body")
                    })
        except Exception:
            pass
            
        if not results:
            results = _search_web_duckduckgo(query, num_results)

        return ToolResult(
            name="webSearch",
            output=[{
                "type": "text",
                "content": results,
                "metadata": {"query": query},
            }],
            is_error=isinstance(results, str) and results.startswith("[error]"),
        )


if __name__ == "__main__":
    tool = webSearchTool()
    result = tool.run({"query": "AI for science"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")


