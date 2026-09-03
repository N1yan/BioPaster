from __future__ import annotations
import re
import urllib.request
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from ..registry import ToolSpec
from ..protocol import ToolResult
from ..context import ToolContext
import asyncio

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TIMEOUT = 60


def _fallback_html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _code_language(element: Any) -> str | None:
    classes = " ".join(element.get("class", []))
    if element.name == "pre" and element.code:
        classes += " " + " ".join(element.code.get("class", []))
    match = re.search(r"(?:language-|lang-|sourceCode\s+)([A-Za-z0-9+#-]+)", classes)
    return match.group(1) if match else None


def _replace_embeds_with_links(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for embed in soup.find_all("embed"):
        src = str(embed.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            embed.decompose()
            continue
        link = soup.new_tag("a", href=src)
        link.string = "Embedded resource"
        embed.replace_with(link)
    return str(soup)


def _extract_html_content(html: str, url: str) -> str:
    try:
        from markdownify import markdownify

        return markdownify(
            _replace_embeds_with_links(html),
            heading_style="ATX",
            code_language_callback=_code_language,
            strip=[
                "img",
                "picture",
                "svg",
                "video",
                "audio",
                "iframe",
                "object",
                "canvas",
            ],
        )
    except Exception:
        pass
    return _fallback_html_text(html)


async def _extract_url_content_playwright(url: str) -> str:
    """Extract the text content of a webpage using playwright."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()

        return _extract_html_content(html, url)
    except Exception as e:
        return f"[error] {url}: {e}"


def _extract_url_content_http(url: str) -> str:
    """Extract the text content of a webpage using urllib."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/markdown, text/html, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            content_type = (resp.headers.get_content_type() or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        if content_type in {"text/plain", "application/json"}:
            return text.strip()
        if content_type == "text/markdown":
            return text.strip()
        return _extract_html_content(text, url)
    except Exception as e:
        return f"[error] {url}: {e}"


class WebFetchTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="webFetch",
            description=(
                "Fetch the text content of a webpage. Default method is http (urllib). "
                "Use playwright for JavaScript-heavy pages or when http returns 403/blocked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {
                        "type": "string",
                        "enum": ["http", "playwright"],
                        "default": "http",
                    },
                },
                "required": ["url"],
            },
            is_read_only=True,
            max_result_size_chars=20_000,
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        url = tool_input.get("url", "").strip()
        method = tool_input.get("method") or "http"
        if method == "requests":
            method = "http"

        if not re.match(r"^https?://", url):
            return ToolResult(
                name="webFetch",
                output=[{
                    "type": "text",
                    "url": url,
                    "content": "[error] only http/https URLs are allowed",
                }],
                is_error=True,
            )
        if method == "http":
            content = _extract_url_content_http(url)
        elif method == "playwright":
            content = asyncio.run(_extract_url_content_playwright(url))
        else:
            content = "[error] only http or playwright methods are allowed"

        return ToolResult(
            name="webFetch",
            output=[{
                "type": "text",
                "metadata": {"url": url},
                "content": content,
            }],
            is_error=isinstance(content, str) and content.startswith("[error]"),
        )


if __name__ == "__main__":
    tool = webFetchTool()
    result = tool.run({"url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10958690"})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")

    result = tool.run({"url": "https://bioconductor.org/packages//release/bioc/vignettes/DESeq2/inst/doc/DESeq2.html"})
