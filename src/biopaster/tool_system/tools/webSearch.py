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

        print("\033[33m[webSearchTool]\033[0m")
        return ToolResult(
            name="webSearch",
            output=[{
                "type": "text",
                "content": results,
                "query": query,
            }],
            is_error=isinstance(results, str) and results.startswith("[error]"),
        )


if __name__ == "__main__":
    tool = webSearchTool()
    result = tool.run({"query": "AI for science"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")





