from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from typing import Any

import requests
from bs4 import BeautifulSoup

from tool_system.tools.papers.paper_identity import (
    TIMEOUT,
    contact_email as _email,
    metadata_matches as _metadata_matches,
    ncbi_get as _ncbi_get,
    request_headers as _headers,
    resolve_ncbi_ids as _resolve_ncbi_ids,
    select_identifier as _select_identifier,
)
from tool_system.tools.papers.paper_metadata import (
    fetch_exact_metadata as _fetch_exact_metadata,
    openalex_work_metadata as _openalex_work_metadata,
    parse_jats_document as _parse_jats_document,
)
from tool_system.protocol import ToolResult
from tool_system.registry import ToolSpec

_SOURCE_PRIORITY = {"pmc": 0, "biorxiv": 1, "arxiv": 2, "unpaywall": 3, "openalex": 4}


def _http_get(url: str) -> tuple[str, str, str]:
    """GET url. Returns (final_url, content_type, text)."""
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        content_type = resp.headers.get_content_type() or ""
        charset = resp.headers.get_content_charset() or "utf-8"
        final_url = resp.geturl()
    return final_url, content_type, raw.decode(charset, errors="replace")


# ── discovery (text sources only) ───────────────────────────────────────────


def _unique_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = f"{item.get('source')}::{item.get('format')}::{item.get('url') or item.get('pmcid')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _arxiv_html_candidate(arxiv_id: str) -> dict[str, Any]:
    clean = re.sub(r"v\d+$", "", arxiv_id.strip())
    return {
        "source": "arxiv",
        "format": "html",
        "url": f"https://arxiv.org/html/{clean}",
        "arxiv_id": clean,
    }


def _biorxiv_text_candidates(doi: str) -> list[dict[str, Any]]:
    if not doi.startswith("10.1101/"):
        return []
    out: list[dict[str, Any]] = []
    for server in ("biorxiv", "medrxiv"):
        try:
            resp = requests.get(
                f"https://api.biorxiv.org/details/{server}/{doi}/json",
                headers=_headers(),
                timeout=TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            collection = resp.json().get("collection") or []
            if not collection:
                continue
            item = collection[0]
            version = str(item.get("version") or "1").strip()
            if jats := (item.get("jatsxml") or "").strip():
                out.append({"source": "biorxiv", "format": "jats_xml", "url": jats, "doi": doi})
            out.append(
                {
                    "source": "biorxiv",
                    "format": "html",
                    "url": f"https://www.{server}.org/content/{doi}v{version}",
                    "doi": doi,
                }
            )
            return out
        except requests.RequestException:
            continue
    return [
        {
            "source": "biorxiv",
            "format": "html",
            "url": f"https://www.biorxiv.org/content/{doi}v1",
            "doi": doi,
        }
    ]


def _unpaywall_html_candidates(doi: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": _email()},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException:
        return []
    out: list[dict[str, Any]] = []
    for item in [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]:
        if not item:
            continue
        url = (item.get("url") or "").strip()
        pdf_url = (item.get("url_for_pdf") or "").strip()
        if pdf_url and url == pdf_url:
            continue
        if not url or url.lower().endswith(".pdf"):
            continue
        if any(c.get("url") == url for c in out):
            continue
        out.append({"source": "unpaywall", "format": "html", "url": url, "doi": doi})
    return out


def _openalex_text_candidates(doi: str) -> list[dict[str, Any]]:
    params = {"mailto": _email()}
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    try:
        resp = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            params=params,
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        work = resp.json()
    except requests.RequestException:
        return []
    metadata = _openalex_work_metadata(work, doi)
    out: list[dict[str, Any]] = []
    oa_url = (
        (work.get("open_access") or {}).get("oa_url")
        or (work.get("best_oa_location") or {}).get("landing_page_url")
        or ""
    ).strip()
    if oa_url and not oa_url.lower().endswith(".pdf"):
        out.append({
            "source": "openalex",
            "format": "html",
            "url": oa_url,
            "doi": doi,
            "metadata": metadata,
        })
    pmcid = ((work.get("ids") or {}).get("pmcid") or "").rsplit("/", 1)[-1]
    if pmcid:
        display = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
        out.append(
            {
                "source": "pmc",
                "format": "pmc_xml",
                "pmcid": display,
                "doi": doi,
                "metadata": metadata,
                "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{display}/",
            }
        )
    return out


def _discover_candidates(
    id_value: str,
    id_type: str,
    canonical: dict[str, str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    doi = canonical["doi"] or (id_value if id_type == "doi" else "")
    if doi:
        candidates.extend(_biorxiv_text_candidates(doi))
        candidates.extend(_unpaywall_html_candidates(doi))
        candidates.extend(_openalex_text_candidates(doi))

    return _unique_candidates(candidates)


def _primary_candidates(
    id_value: str,
    id_type: str,
    canonical: dict[str, str],
) -> list[dict[str, Any]]:
    if id_type == "arxiv":
        return [_arxiv_html_candidate(id_value)]
    if canonical["pmcid"]:
        pmcid = canonical["pmcid"]
        return [{
            "source": "pmc",
            "format": "pmc_xml",
            "pmcid": pmcid,
            "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
        }]
    return []


def _candidate_batches(
    id_value: str,
    id_type: str,
    canonical: dict[str, str],
):
    primary = _primary_candidates(id_value, id_type, canonical)
    if primary:
        yield primary
    if id_type != "arxiv":
        primary_keys = {
            (item.get("source"), item.get("url") or item.get("pmcid"))
            for item in primary
        }
        fallback = [
            item
            for item in _sort_candidates(_discover_candidates(id_value, id_type, canonical))
            if (item.get("source"), item.get("url") or item.get("pmcid")) not in primary_keys
        ]
        if fallback:
            yield fallback


def _sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[int, int]:
        src = _SOURCE_PRIORITY.get(str(item.get("source")), 99)
        fmt_rank = 0 if item.get("format") in {"pmc_xml", "jats_xml"} else 1
        return (src, fmt_rank)

    return sorted(candidates, key=key)


# ── fetchers ────────────────────────────────────────────────────────────────


def _fetch_result(
    *,
    status: str,
    source: str,
    url: str,
    text: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "text": text,
        "source": source,
        "url": url,
        "error": error,
        "metadata": metadata,
    }


def _fetch_pmc(pmcid: str) -> dict[str, Any]:
    numeric = pmcid.upper().replace("PMC", "")
    display = f"PMC{numeric}"
    try:
        resp = _ncbi_get("efetch.fcgi", {"db": "pmc", "id": numeric, "retmode": "xml"})
        xml_text = resp.text
        text, metadata, has_body = _parse_jats_document(xml_text)
        if not text.strip():
            return _fetch_result(
                status="error",
                source="pmc",
                url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{display}/",
                error="PMC record contained no parseable text",
            )
        if not has_body:
            return _fetch_result(
                status="abstract_only",
                text=text,
                source="pmc",
                url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{display}/",
                metadata=metadata,
            )
        return _fetch_result(
            status="ok",
            text=text,
            source="pmc",
            url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{display}/",
            metadata=metadata,
        )
    except Exception as exc:
        return _fetch_result(
            status="error",
            source="pmc",
            url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{display}/",
            error=str(exc),
        )


def _fetch_html(url: str, source: str = "html") -> dict[str, Any]:
    try:
        final_url, content_type, body = _http_get(url)
    except urllib.error.URLError as exc:
        return _fetch_result(status="error", source=source, url=url, error=str(exc))
    if "html" not in content_type.lower():
        return _fetch_result(
            status="error",
            source=source,
            url=final_url,
            error=f"Not HTML ({content_type})",
        )
    soup = BeautifulSoup(body, "html.parser")
    article = soup.find("article") or soup.find("main") or soup.select_one("#content")
    if article is None:
        return _fetch_result(
            status="error",
            source=source,
            url=final_url,
            error="No article HTML found",
        )
    for tag in article.select("script, style, nav, header, footer, aside"):
        tag.decompose()
    parts = [
        el.get_text(" ", strip=True)
        for el in article.find_all(["h1", "h2", "h3", "p"])
        if el.get_text(" ", strip=True)
    ]
    text = "\n\n".join(parts)
    return _fetch_result(status="ok", text=text, source=source, url=final_url)


def _fetch_jats_xml(url: str, source: str) -> dict[str, Any]:
    try:
        final_url, _, body = _http_get(url)
        text, metadata, _ = _parse_jats_document(body)
        if not text.strip():
            return _fetch_result(
                status="error",
                source=source,
                url=final_url,
                error="JATS XML contained no parseable text",
            )
        return _fetch_result(
            status="ok",
            text=text,
            source=source,
            url=final_url,
            metadata=metadata,
        )
    except Exception as exc:
        return _fetch_result(status="error", source=source, url=url, error=str(exc))


def _fetch_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    fmt = candidate.get("format")
    source = str(candidate.get("source") or "")
    if fmt == "pmc_xml":
        return _fetch_pmc(str(candidate.get("pmcid") or ""))
    url = (candidate.get("url") or "").strip()
    if not url:
        return _fetch_result(
            status="error",
            source=source,
            url="",
            error="Candidate has no URL",
        )
    if fmt == "jats_xml":
        return _fetch_jats_xml(url, source)
    result = _fetch_html(url, source)
    if result.get("status") == "ok" and isinstance(candidate.get("metadata"), dict):
        result["metadata"] = candidate["metadata"]
    return result


# ── exact metadata ──────────────────────────────────────────────────────────


def _verified_metadata(
    result: dict[str, Any],
    identity: dict[str, str],
) -> dict[str, Any] | None:
    embedded = result.get("metadata")
    if isinstance(embedded, dict) and _metadata_matches(identity, embedded):
        return embedded
    return _fetch_exact_metadata(identity)


def _public_result(
    result: dict[str, Any],
    identity: dict[str, str],
    max_chars: int,
) -> dict[str, Any]:
    text = str(result["text"])
    limit = max(1, int(max_chars))
    status = str(result["status"])
    output = {
        "type": "text",
        "content": text[:limit],
        "status": status,
        "canonical_ids": identity,
        "fulltext_source": result.get("source"),
        "url": result.get("url"),
        "text_total_chars": len(text),
        "truncated": len(text) > limit,
        "metadata": _verified_metadata(result, identity),
    }
    if status == "abstract_only":
        output["message"] = "The source contained metadata and an abstract, but no full-text body."
    return output


# ── public API ──────────────────────────────────────────────────────────────


def fetch_paper_fulltext(
    *,
    pmid: str | None = None,
    doi: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
    max_chars: int = 50_000,
) -> list[dict[str, Any]]:
    try:
        id_value, id_type = _select_identifier(pmid=pmid, doi=doi, pmcid=pmcid, arxiv_id=arxiv_id)
    except ValueError as exc:
        return [{
            "type": "text",
            "content": f"[error] {str(exc)}",
            "status": "error",
        }]

    canonical = {"pmid": "", "pmcid": "", "doi": "", "arxiv_id": ""}
    if id_type == "pmid":
        canonical["pmid"] = id_value
    elif id_type == "doi":
        canonical["doi"] = id_value
    elif id_type == "pmcid":
        canonical["pmcid"] = id_value
    else:
        canonical["arxiv_id"] = id_value

    if id_type != "arxiv":
        canonical = _resolve_ncbi_ids(
            pmid=canonical["pmid"],
            doi=canonical["doi"],
            pmcid=canonical["pmcid"],
        )
        canonical["arxiv_id"] = ""

    errors: list[dict[str, str]] = []
    fallback_result: dict[str, Any] | None = None
    had_candidates = False

    for candidates in _candidate_batches(id_value, id_type, canonical):
        for candidate in candidates:
            had_candidates = True
            result = _fetch_candidate(candidate)
            if result.get("status") == "ok" and result.get("text"):
                return [_public_result(result, canonical, max_chars)]
            if result.get("status") == "abstract_only" and result.get("text"):
                fallback_result = result
            elif result.get("error"):
                errors.append({
                    "source": str(result.get("source") or candidate.get("source") or "unknown"),
                    "error": str(result["error"]),
                })

    if fallback_result:
        return [_public_result(fallback_result, canonical, max_chars)]

    return [{
        "type": "text",
        "content": "[error] No full text could be retrieved.",
        "status": "fetch_failed" if had_candidates else "not_found",
        "canonical_ids": canonical,
        "errors": errors,
    }]



class PaperFetchTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paperFetch",
            description=(
                "Fetch readable full text for a paper. Provide exactly one of pmid, doi, pmcid, "
                "or arxiv_id. Tries PMC XML, bioRxiv JATS/HTML, arXiv HTML, then OA HTML. "
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pmid": {"type": "string"},
                    "doi": {"type": "string"},
                    "pmcid": {"type": "string"},
                    "arxiv_id": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 50000},
                },
            },
            is_read_only=True,
            max_result_size_chars=20_000,
        )

    def run(self, tool_input: dict[str, Any]) -> ToolResult:
        payload = fetch_paper_fulltext(
            pmid=tool_input.get("pmid"),
            doi=tool_input.get("doi"),
            pmcid=tool_input.get("pmcid"),
            arxiv_id=tool_input.get("arxiv_id"),
            max_chars=int(tool_input.get("max_chars") or 50_000),
        )
        print("\033[33m[paperFetchTool]\033[0m")
        return ToolResult(
            name="paperFetch",
            output=payload,
        )


if __name__ == "__main__":
    pmcid = "PMC10958690"
    tool = paperFetchTool()
    result = tool.run({"pmcid": pmcid})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")
