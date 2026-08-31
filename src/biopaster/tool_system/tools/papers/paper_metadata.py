from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from .paper_identity import (
    TIMEOUT,
    metadata_matches,
    ncbi_get,
    normalize_arxiv_id,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    request_headers,
)


def empty_metadata(**values: Any) -> dict[str, Any]:
    metadata = {
        "title": "",
        "authors": [],
        "journal": "",
        "publication_dates": {},
        "year": None,
        "doi": "",
        "pmid": "",
        "pmcid": "",
        "arxiv_id": "",
    }
    metadata.update(values)
    metadata["doi"] = normalize_doi(str(metadata.get("doi") or ""))
    metadata["pmid"] = normalize_pmid(str(metadata.get("pmid") or ""))
    metadata["pmcid"] = normalize_pmcid(str(metadata.get("pmcid") or ""))
    metadata["arxiv_id"] = normalize_arxiv_id(str(metadata.get("arxiv_id") or ""))
    return metadata


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _descendants(element: ET.Element, name: str):
    return (child for child in element.iter() if _local_name(child) == name)


def _first(element: ET.Element, name: str) -> ET.Element | None:
    return next(_descendants(element, name), None)


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _date(element: ET.Element) -> str:
    parts = {name: _text(_first(element, name)) for name in ("year", "month", "day")}
    if not parts["year"]:
        return ""
    value = parts["year"]
    if parts["month"]:
        value += f"-{parts['month'].zfill(2)}"
    if parts["day"]:
        value += f"-{parts['day'].zfill(2)}"
    return value


def parse_jats_document(xml_text: str) -> tuple[str, dict[str, Any], bool]:
    root = ET.fromstring(xml_text)
    error = next((item for item in root.iter() if _local_name(item).lower() == "error"), None)
    if error is not None:
        raise ValueError(_text(error) or "The XML source returned an error")
    article_scope = _first(root, "article-meta")
    journal_scope = _first(root, "journal-meta")
    if article_scope is None:
        article_scope = root
    if journal_scope is None:
        journal_scope = root

    identifiers = {"doi": "", "pmid": "", "pmcid": ""}
    for element in _descendants(article_scope, "article-id"):
        id_type = (element.get("pub-id-type") or "").lower()
        if id_type == "doi":
            identifiers["doi"] = _text(element)
        elif id_type == "pmid":
            identifiers["pmid"] = _text(element)
        elif id_type in {"pmc", "pmcid"}:
            identifiers["pmcid"] = _text(element)

    authors: list[str] = []
    for contributor in _descendants(article_scope, "contrib"):
        if (contributor.get("contrib-type") or "author") != "author":
            continue
        collective = _text(_first(contributor, "collab"))
        given = _text(_first(contributor, "given-names"))
        surname = _text(_first(contributor, "surname"))
        if name := collective or " ".join(part for part in (given, surname) if part):
            authors.append(name)

    dates: dict[str, str] = {}
    for index, pub_date in enumerate(_descendants(article_scope, "pub-date"), start=1):
        date_type = pub_date.get("pub-type") or pub_date.get("date-type") or f"unspecified_{index}"
        if value := _date(pub_date):
            dates[date_type] = value
    first_date = next(iter(dates.values()), "")
    title = _text(_first(article_scope, "article-title"))
    metadata = empty_metadata(
        title=title,
        authors=authors,
        journal=_text(_first(journal_scope, "journal-title")),
        publication_dates=dates,
        year=int(first_date[:4]) if first_date[:4].isdigit() else None,
        **identifiers,
    )

    parts = [title] if title else []
    abstract = _first(article_scope, "abstract")
    if abstract is not None and (value := _text(abstract)):
        parts.append(value)
    body = _first(root, "body")
    if body is not None:
        parts.extend(value for paragraph in _descendants(body, "p") if (value := _text(paragraph)))
    if not parts and (value := _text(root)):
        parts.append(value)
    return "\n\n".join(parts), metadata, body is not None


def crossref_metadata(doi: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            headers=request_headers(),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        item = response.json().get("message") or {}
    except (requests.RequestException, ValueError):
        return None

    authors = []
    for author in item.get("author") or []:
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        if name := " ".join(part for part in (given, family) if part):
            authors.append(name)
    dates: dict[str, str] = {}
    for key in ("published-online", "published-print", "published", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            dates[key] = "-".join(str(part).zfill(2) for part in parts)
    first_date = next(iter(dates.values()), "")
    titles = item.get("title") or []
    journals = item.get("container-title") or []
    return empty_metadata(
        title=str(titles[0]) if titles else "",
        authors=authors,
        journal=str(journals[0]) if journals else "",
        publication_dates=dates,
        year=int(first_date[:4]) if first_date[:4].isdigit() else None,
        doi=str(item.get("DOI") or doi),
    )


def pubmed_metadata(pmid: str) -> dict[str, Any] | None:
    try:
        response = ncbi_get("efetch.fcgi", {"db": "pubmed", "id": pmid, "retmode": "xml"})
        root = ET.fromstring(response.text)
    except Exception:
        return None
    article = root.find(".//PubmedArticle")
    citation = article.find("MedlineCitation") if article is not None else None
    journal_article = citation.find("Article") if citation is not None else None
    if article is None or citation is None or journal_article is None:
        return None

    authors = []
    for author in journal_article.findall("AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        given = _text(author.find("ForeName"))
        family = _text(author.find("LastName"))
        if name := collective or " ".join(part for part in (given, family) if part):
            authors.append(name)
    ids = {"doi": "", "pmid": _text(citation.find("PMID")), "pmcid": ""}
    for item in article.findall("PubmedData/ArticleIdList/ArticleId"):
        id_type = (item.get("IdType") or "").lower()
        if id_type == "doi":
            ids["doi"] = _text(item)
        elif id_type == "pmc":
            ids["pmcid"] = _text(item)
    pub_date = journal_article.find("Journal/JournalIssue/PubDate")
    date = ""
    if pub_date is not None:
        date = "-".join(
            part for name in ("Year", "Month", "Day") if (part := _text(pub_date.find(name)))
        ) or _text(pub_date.find("MedlineDate"))
    year_match = re.search(r"\d{4}", date)
    return empty_metadata(
        title=_text(journal_article.find("ArticleTitle")),
        authors=authors,
        journal=_text(journal_article.find("Journal/Title")),
        publication_dates={"pubmed": date} if date else {},
        year=int(year_match.group()) if year_match else None,
        **ids,
    )


def arxiv_metadata(arxiv_id: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            "https://export.arxiv.org/api/query",
            params={"id_list": arxiv_id},
            headers=request_headers(),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError):
        return None
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        return None
    published = _text(entry.find("atom:published", ns))
    updated = _text(entry.find("atom:updated", ns))
    authors = [_text(name) for name in entry.findall("atom:author/atom:name", ns)]
    return empty_metadata(
        title=_text(entry.find("atom:title", ns)),
        authors=[author for author in authors if author],
        journal="arXiv",
        publication_dates={key: value for key, value in {"published": published, "updated": updated}.items() if value},
        year=int(published[:4]) if published[:4].isdigit() else None,
        arxiv_id=_text(entry.find("atom:id", ns)),
    )


def openalex_work_metadata(work: dict[str, Any], doi: str) -> dict[str, Any]:
    primary = work.get("primary_location") or {}
    ids = work.get("ids") or {}
    authors = [
        str((authorship.get("author") or {}).get("display_name") or "").strip()
        for authorship in work.get("authorships") or []
    ]
    publication_date = str(work.get("publication_date") or "")
    return empty_metadata(
        title=str(work.get("title") or ""),
        authors=[author for author in authors if author],
        journal=str((primary.get("source") or {}).get("display_name") or ""),
        publication_dates={"openalex": publication_date} if publication_date else {},
        year=work.get("publication_year"),
        doi=str(work.get("doi") or doi),
        pmid=str(ids.get("pmid") or "").rsplit("/", 1)[-1],
        pmcid=str(ids.get("pmcid") or "").rsplit("/", 1)[-1],
    )


def fetch_exact_metadata(identity: dict[str, str]) -> dict[str, Any] | None:
    if identity.get("doi"):
        metadata = crossref_metadata(identity["doi"])
        if metadata and metadata_matches(identity, metadata):
            return metadata
    if identity.get("pmid"):
        metadata = pubmed_metadata(identity["pmid"])
        if metadata and metadata_matches(identity, metadata):
            return metadata
    if identity.get("arxiv_id"):
        metadata = arxiv_metadata(identity["arxiv_id"])
        if metadata and metadata_matches(identity, metadata):
            return metadata
    return None
