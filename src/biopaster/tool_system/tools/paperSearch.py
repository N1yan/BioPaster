
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlencode

import requests

from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext

USER_AGENT = "BioPaster/1.0 (mailto:{email})"
TIMEOUT = 30
VALID_SOURCES = ("pubmed", "crossref", "arxiv", "openalex", "biorxiv")

# ── shared ──────────────────────────────────────────────────────────────────


def _email() -> str:
    return (
        os.getenv("NCBI_EMAIL")
        or os.getenv("OPENALEX_MAILTO")
        or os.getenv("CROSSREF_MAILTO")
        or "biopaster@example.com"
    )


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT.format(email=_email()), "Accept": "application/json"}


def _empty_paper(**overrides: Any) -> dict[str, Any]:
    paper = {
        "title": "",
        "authors": [],
        "year": None,
        "doi": "",
        "pmid": "",
        "pmcid": "",
        "arxiv_id": "",
        "journal": "",
        "abstract": "",
        "url": "",
        "pdf_url": "",
        "source": "",
    }
    paper.update(overrides)
    return paper


def _first_http(*values: Any) -> str:
    for value in values:
        text = (value or "").strip() if isinstance(value, str) else ""
        if text.startswith("http"):
            return text
    return ""


def _parse_year_limit(year_limit: Any) -> tuple[int | None, int | None]:
    if not year_limit:
        return None, None
    if not isinstance(year_limit, (list, tuple)) or len(year_limit) != 2:
        raise ValueError("year_limit must be [start_year, end_year]")
    start, end = int(year_limit[0]), int(year_limit[1])
    if start > end:
        raise ValueError("year_limit start must be <= end")
    return start, end


def _in_year_range(year: int | None, start: int | None, end: int | None) -> bool:
    if start is None and end is None:
        return True
    if year is None:
        return True
    if start is not None and year < start:
        return False
    if end is not None and year > end:
        return False
    return True


# ── PubMed ──────────────────────────────────────────────────────────────────


def _pubmed_search(query: str, rows: int, year_start: int | None, year_end: int | None) -> dict[str, Any]:
    term = query
    if year_start is not None and year_end is not None:
        term = f'({query}) AND ("{year_start}"[Date - Publication] : "{year_end}"[Date - Publication])'

    params: dict[str, Any] = {
        "db": "pubmed",
        "term": term,
        "retmax": rows,
        "usehistory": "y",
        "retmode": "xml",
        "sort": "relevance",
        "email": _email(),
    }
    api_key = os.getenv("NCBI_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key

    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    search = requests.get(base + "esearch.fcgi", params=params, timeout=TIMEOUT)
    search.raise_for_status()
    root = ET.fromstring(search.content)
    web_env = root.findtext("WebEnv")
    query_key = root.findtext("QueryKey")
    if not web_env or not query_key:
        return {"status": "ok", "source": "pubmed", "results": []}

    fetch = requests.get(
        base + "efetch.fcgi",
        params={
            "db": "pubmed",
            "query_key": query_key,
            "WebEnv": web_env,
            "retmax": rows,
            "retmode": "xml",
            "rettype": "abstract",
            "email": _email(),
            **({"api_key": api_key} if api_key else {}),
        },
        timeout=TIMEOUT,
    )
    fetch.raise_for_status()
    results = []
    for article in ET.fromstring(fetch.content).findall("PubmedArticle"):
        paper = _parse_pubmed_article(article)
        if paper:
            results.append(paper)
    return {"status": "ok", "source": "pubmed", "results": results}


def _parse_pubmed_article(article: ET.Element) -> dict[str, Any] | None:
    citation = article.find("MedlineCitation")
    if citation is None:
        return None
    art = citation.find("Article")
    if art is None:
        return None

    authors: list[str] = []
    for author in art.findall("AuthorList/Author"):
        last = (author.findtext("LastName") or "").strip()
        fore = (author.findtext("ForeName") or "").strip()
        collective = (author.findtext("CollectiveName") or "").strip()
        if last:
            authors.append(f"{last} {fore}".strip())
        elif collective:
            authors.append(collective)

    abstract_parts: list[str] = []
    for text_el in art.findall("Abstract/AbstractText"):
        label = text_el.get("Label", "")
        content = "".join(text_el.itertext()).strip()
        if not content:
            continue
        abstract_parts.append(f"{label}: {content}" if label else content)

    year = None
    year_text = art.findtext("Journal/JournalIssue/PubDate/Year")
    if year_text:
        try:
            year = int(year_text.strip())
        except ValueError:
            year = None
    if year is None:
        medline = art.findtext("Journal/JournalIssue/PubDate/MedlineDate") or ""
        match = re.search(r"\d{4}", medline)
        if match:
            year = int(match.group())

    doi = ""
    for eloc in art.findall("ELocationID"):
        if eloc.get("EIdType") == "doi" and eloc.text:
            doi = eloc.text.strip()
            break

    pmid = (citation.findtext("PMID") or "").strip()
    pmcid = ""
    for aid in article.findall("PubmedData/ArticleIdList/ArticleId"):
        id_type = (aid.get("IdType") or "").lower()
        text = (aid.text or "").strip()
        if not text:
            continue
        if id_type == "doi" and not doi:
            doi = text
        elif id_type == "pmc":
            pmcid = text if text.upper().startswith("PMC") else f"PMC{text}"

    return _empty_paper(
        title=(art.findtext("ArticleTitle") or "").strip(),
        authors=authors,
        year=year,
        pmid=pmid,
        pmcid=pmcid,
        doi=doi,
        journal=(art.findtext("Journal/Title") or "").strip(),
        abstract=" ".join(abstract_parts),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        pdf_url=f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else "",
        source="pubmed",
    )


# ── CrossRef ────────────────────────────────────────────────────────────────


def _crossref_search(query: str, rows: int, year_start: int | None, year_end: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {"query": query, "rows": rows}
    filters: list[str] = []
    if year_start is not None:
        filters.append(f"from-pub-date:{year_start}")
    if year_end is not None:
        filters.append(f"until-pub-date:{year_end}")
    if filters:
        params["filter"] = ",".join(filters)

    resp = requests.get(
        "https://api.crossref.org/works",
        params=params,
        headers=_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    items = (resp.json().get("message") or {}).get("items") or []
    results = []
    for item in items:
        authors = []
        for author in item.get("author") or []:
            family = (author.get("family") or "").strip()
            given = (author.get("given") or "").strip()
            if family:
                authors.append(f"{family} {given}".strip())
        year = None
        issued = ((item.get("issued") or {}).get("date-parts") or [[]])
        if issued and issued[0]:
            try:
                year = int(issued[0][0])
            except (TypeError, ValueError, IndexError):
                year = None
        titles = item.get("title") or []
        journals = item.get("container-title") or []
        doi = (item.get("DOI") or "").strip()
        pdf_url = ""
        for link in item.get("link") or []:
            ctype = (link.get("content-type") or "").lower()
            href = (link.get("URL") or "").strip()
            if href and "pdf" in ctype:
                pdf_url = href
                break
        results.append(
            _empty_paper(
                title=titles[0] if titles else "",
                authors=authors,
                year=year,
                doi=doi,
                journal=journals[0] if journals else "",
                abstract=(item.get("abstract") or "").strip(),
                url=_first_http(item.get("URL"), f"https://doi.org/{doi}" if doi else ""),
                pdf_url=pdf_url,
                source="crossref",
            )
        )
    return {"status": "ok", "source": "crossref", "results": results}


# ── arXiv ───────────────────────────────────────────────────────────────────


def _arxiv_search(query: str, rows: int, year_start: int | None, year_end: int | None) -> dict[str, Any]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    params = {
        "search_query": f"all:{query.replace(' ', '+')}",
        "start": 0,
        "max_results": rows,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    resp = requests.get(
        "https://export.arxiv.org/api/query",
        params=params,
        headers={"User-Agent": USER_AGENT.format(email=_email())},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    results = []
    for entry in ET.fromstring(resp.content).findall("atom:entry", ns):
        raw_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        match = re.search(r"(\d{4}\.\d{4,5})", raw_id)
        arxiv_id = match.group(1) if match else ""
        published = entry.findtext("atom:published", default="", namespaces=ns) or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        if not _in_year_range(year, year_start, year_end):
            continue
        authors = [
            (name.text or "").strip()
            for name in entry.findall("atom:author/atom:name", ns)
            if name.text
        ]
        results.append(
            _empty_paper(
                title=re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                authors=authors,
                year=year,
                arxiv_id=arxiv_id,
                journal="arXiv",
                abstract=re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=ns) or "").strip(),
                url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else "",
                source="arxiv",
            )
        )
    return {"status": "ok", "source": "arxiv", "results": results}


# ── bioRxiv (via Europe PMC; official bioRxiv API has no keyword search) ──


def _biorxiv_authors(rec: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in (rec.get("authorList") or {}).get("author") or []:
        last = (author.get("lastName") or "").strip()
        first = (author.get("firstName") or "").strip()
        if last and first:
            authors.append(f"{last} {first}")
        elif last:
            authors.append(last)
        elif name := (author.get("fullName") or "").strip():
            authors.append(name)
    if authors:
        return authors
    return [part.strip() for part in (rec.get("authorString") or "").split(",") if part.strip()]


def _biorxiv_search(query: str, rows: int, year_start: int | None, year_end: int | None) -> dict[str, Any]:
    term = f'({query}) AND SRC:PPR AND PUBLISHER:"bioRxiv"'
    if year_start is not None and year_end is not None:
        term += f" AND FIRST_PDATE:[{year_start}-01-01 TO {year_end}-12-31]"
    resp = requests.get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={
            "query": term,
            "format": "json",
            "pageSize": rows,
            "resultType": "core",
        },
        headers={"User-Agent": USER_AGENT.format(email=_email())},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    records = (resp.json().get("resultList") or {}).get("result") or []
    results = []
    for rec in records:
        year = None
        if rec.get("pubYear"):
            try:
                year = int(rec["pubYear"])
            except (TypeError, ValueError):
                year = None
        doi = (rec.get("doi") or "").strip()
        pdf_url = ""
        landing = ""
        for item in ((rec.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
            href = (item.get("url") or "").strip()
            style = (item.get("documentStyle") or "").lower()
            if not href:
                continue
            if style == "pdf" and not pdf_url:
                pdf_url = href
            elif style in {"html", "doi"} and not landing:
                landing = href
        results.append(
            _empty_paper(
                title=(rec.get("title") or "").strip(),
                authors=_biorxiv_authors(rec),
                year=year,
                doi=doi,
                pmid=str(rec.get("pmid") or "").strip(),
                journal="bioRxiv",
                abstract=(rec.get("abstractText") or "").strip(),
                url=_first_http(landing, f"https://doi.org/{doi}" if doi else ""),
                pdf_url=pdf_url,
                source="biorxiv",
            )
        )
    return {"status": "ok", "source": "biorxiv", "results": results}


# ── OpenAlex ────────────────────────────────────────────────────────────────


def _openalex_search(query: str, rows: int, year_start: int | None, year_end: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "search": query,
        "per_page": rows,
        "mailto": _email(),
    }
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    filters: list[str] = []
    if year_start is not None:
        filters.append(f"from_publication_date:{year_start}-01-01")
    if year_end is not None:
        filters.append(f"to_publication_date:{year_end}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    resp = requests.get(f"https://api.openalex.org/works?{urlencode(params)}", timeout=TIMEOUT)
    resp.raise_for_status()
    results = []
    for work in resp.json().get("results") or []:
        inverted = work.get("abstract_inverted_index") or {}
        positions: list[tuple[int, str]] = []
        for word, idxs in inverted.items():
            for pos in idxs:
                positions.append((pos, word))
        positions.sort()
        abstract = " ".join(word for _, word in positions)

        doi = work.get("doi") or ""
        if isinstance(doi, str) and doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/") :]
        ids = work.get("ids") or {}
        pmid = ""
        if isinstance(ids.get("pmid"), str):
            pmid = ids["pmid"].rsplit("/", 1)[-1]
        authors = [
            (auth.get("author") or {}).get("display_name", "")
            for auth in work.get("authorships") or []
            if (auth.get("author") or {}).get("display_name")
        ]
        best_oa = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        oa_url = ((work.get("open_access") or {}).get("oa_url") or "").strip()
        pdf_url = _first_http(best_oa.get("pdf_url"), oa_url if oa_url.lower().endswith(".pdf") else "")
        pmcid = ((work.get("ids") or {}).get("pmcid") or "")
        if isinstance(pmcid, str):
            pmcid = pmcid.rsplit("/", 1)[-1]
        else:
            pmcid = ""
        if pmcid and not pmcid.upper().startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        results.append(
            _empty_paper(
                title=work.get("title") or "",
                authors=authors,
                year=work.get("publication_year"),
                doi=doi,
                pmid=pmid,
                pmcid=pmcid,
                journal=(primary.get("source") or {}).get("display_name") or "",
                abstract=abstract,
                url=_first_http(
                    best_oa.get("landing_page_url"),
                    primary.get("landing_page_url"),
                    f"https://doi.org/{doi}" if doi else "",
                ),
                pdf_url=pdf_url,
                source="openalex",
            )
        )
    return {"status": "ok", "source": "openalex", "results": results}


_SOURCE_FNS = {
    "pubmed": _pubmed_search,
    "crossref": _crossref_search,
    "arxiv": _arxiv_search,
    "openalex": _openalex_search,
    "biorxiv": _biorxiv_search,
}


# ── merge ───────────────────────────────────────────────────────────────────


def _norm_doi(doi: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.IGNORECASE).lower()


def _norm_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _paper_key(paper: dict[str, Any]) -> str | None:
    if doi := _norm_doi(paper.get("doi") or ""):
        return f"doi:{doi}"
    if pmid := (paper.get("pmid") or "").strip():
        return f"pmid:{pmid}"
    if arxiv_id := (paper.get("arxiv_id") or "").strip():
        return f"arxiv:{arxiv_id}"
    if title := _norm_title(paper.get("title") or ""):
        return f"title:{title}"
    return None


def _author_count(paper: dict[str, Any]) -> int:
    authors = paper.get("authors") or []
    return len(authors) if isinstance(authors, list) else 0


RRF_K = 1


def _merge_paper(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for field, value in incoming.items():
        if field in {"source", "matched_sources", "authors"}:
            continue
        if value and not existing.get(field):
            existing[field] = value
    if _author_count(incoming) > _author_count(existing):
        existing["authors"] = list(incoming.get("authors") or [])
    sources = set(existing.get("matched_sources") or [])
    if incoming.get("source"):
        sources.add(incoming["source"])
    existing["matched_sources"] = sorted(sources)


def _fuse_results(
    by_source: dict[str, list[dict[str, Any]]],
    source_order: list[str],
) -> list[dict[str, Any]]:
    """Merge duplicates, then order by reciprocal rank fusion across sources."""
    by_key: dict[str, dict[str, Any]] = {}
    ranks: dict[str, dict[str, int]] = {}
    leftover: list[dict[str, Any]] = []

    for src in source_order:
        for index, paper in enumerate(by_source.get(src) or [], start=1):
            key = _paper_key(paper)
            if not key:
                leftover.append(paper)
                continue
            if key not in by_key:
                entry = dict(paper)
                entry["matched_sources"] = [paper["source"]] if paper.get("source") else []
                by_key[key] = entry
                ranks[key] = {}
            else:
                _merge_paper(by_key[key], paper)
            prev = ranks[key].get(src)
            if prev is None or index < prev:
                ranks[key][src] = index

    ranked: list[tuple[float, dict[str, Any]]] = []
    for key, paper in by_key.items():
        score = sum(1.0 / (RRF_K + rank) for rank in ranks[key].values())
        ranked.append((score, paper))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [paper for _, paper in ranked] + leftover

def _openalex_pdf_urls(doi: str) -> dict[str, str]:
    params: dict[str, str] = {"mailto": _email()}
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
        if resp.status_code != 200:
            return {}
        work = resp.json()
    except requests.RequestException:
        return {}
    loc = work.get("best_oa_location") or {}
    oa_url = ((work.get("open_access") or {}).get("oa_url") or "").strip()
    pdf_url = _first_http(loc.get("pdf_url"), oa_url if oa_url.lower().endswith(".pdf") else "")
    return {"pdf_url": pdf_url}


def _fill_missing_pdf_urls(papers: list[dict[str, Any]]) -> None:
    pending = [
        paper
        for paper in papers
        if not (paper.get("pdf_url") or "").strip() and _norm_doi(paper.get("doi") or "")
    ]
    if not pending:
        return

    def lookup(paper: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        return paper, _openalex_pdf_urls(_norm_doi(paper.get("doi") or ""))

    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        for paper, urls in pool.map(lookup, pending):
            if urls.get("pdf_url") and not paper.get("pdf_url"):
                paper["pdf_url"] = urls["pdf_url"]


def search_papers(
    query: str,
    *,
    sources: list[str] | None = None,
    max_item: int = 10,
    year_limit: Any = None,
    get_pdf_url: bool = False,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return [{"type": "text", "content": "[error] query is required", "results": []}]

    selected = list(sources or VALID_SOURCES)
    invalid = [src for src in selected if src not in VALID_SOURCES]
    if invalid:
        return [{
            "type": "text",
            "content": f"[error] Invalid sources: {invalid}. Valid: {list(VALID_SOURCES)}",
            "results": [],
        }]
    if not selected:
        return [{
            "type": "text",
            "content": "[error] No sources selected",
            "results": [],
        }]

    try:
        year_start, year_end = _parse_year_limit(year_limit)
    except (TypeError, ValueError) as exc:
        return [{
            "type": "text",
            "content": f"[error] {str(exc)}",
            "results": [],
        }]

    rows = max(1, min(int(max_item), 50))
    by_source: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(_SOURCE_FNS[src], query, rows, year_start, year_end): src
            for src in selected
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:
                errors.append({"source": src, "error": str(exc)})
                continue
            if outcome.get("status") == "error":
                errors.append({"source": src, "error": str(outcome.get("error") or "search failed")})
                continue
            by_source[src] = list(outcome.get("results") or [])

    merged = _fuse_results(by_source, selected)[:rows]

    if get_pdf_url:
        _fill_missing_pdf_urls(merged)
    return [{
        "type": "text",
        "content": merged,
        "query": query,
        "year_limit": [year_start, year_end] if year_start is not None else None,
        "sources_queried": selected,
        "result_count": len(merged),
        "errors": errors or None,
    }]
    

class PaperSearchTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paperSearch",
            description=(
                "Search scholarly papers by keyword. By default queries PubMed, CrossRef, "
                "arXiv, OpenAlex, and bioRxiv in parallel. Merges duplicates and returns "
                "titles, authors, year, identifiers, journal, abstract, landing url, and "
                "pdf_url when the source already has one. Set get_pdf_url=true to also query "
                "OpenAlex for hits that still lack pdf_url. "
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "max_item": {"type": "integer", "default": 10},
                    "year_limit": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "[start_year, end_year]",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(VALID_SOURCES)},
                        "description": (
                            "Which APIs to query. Allowed: pubmed, crossref, arxiv, openalex, biorxiv. "
                            "Use one or more of them to search for papers."
                        ),
                    },
                    "get_pdf_url": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, call OpenAlex for results that have a DOI but no pdf_url. "
                        ),
                    },
                },
                "required": ["query"],
            },
            is_read_only=True,
            max_result_size_chars=20_000,
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(tool_input.get("query") or "").strip()
        payload = search_papers(
            query,
            sources=tool_input.get("sources"),
            max_item=int(tool_input.get("max_item") or 10),
            year_limit=tool_input.get("year_limit"),
            get_pdf_url=bool(tool_input.get("get_pdf_url") or False),
        )

        return ToolResult(
            name="paperSearch",
            output=payload,
            is_error=any(
                isinstance(item, dict)
                and isinstance(item.get("content"), str)
                and item["content"].startswith("[error]")
                for item in payload
            ),
        )


if __name__ == "__main__":
    tool = paperSearchTool()
    result = tool.run({"query": "AI for science",
                       "max_item": 5,
                       "year_limit": [2020, 2026],
                       "sources": ["pubmed", "crossref", "arxiv", "openalex", "biorxiv"],
                       "get_pdf_url": True})
    for out in result.output:
        for key, value in out.items():
            print(f"{key}: {str(value)[:200]}")
            
            
    search_papers("PMID: 41448140")
