from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import paper_metadata


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"^(?:abstract|article|research article|original article|review|contents?|"
    r"https?://|www\.|doi\b|department\b|university\b|institute\b)",
    re.IGNORECASE,
)
_AFFILIATION_RE = re.compile(
    r"\b(?:academy|brain|centre|center|college|company|corporation|department|"
    r"faculty|google|hospital|inc\.?|institute|laborator(?:y|ies)|research|school|"
    r"university)\b",
    re.IGNORECASE,
)


def _clean_doi(value: str) -> str:
    return _DOI_PREFIX_RE.sub("", value.strip()).rstrip(".,;:")


def _doi_candidates(values: list[str]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        for match in _DOI_RE.finditer(value or ""):
            doi = _clean_doi(match.group(0))
            key = doi.lower()
            if doi and key not in seen:
                seen.add(key)
                candidates.append(doi)
    return candidates


def _split_authors(value: str) -> list[str]:
    value = re.sub(r"\s+", " ", value or "").strip(" ,;")
    if not value:
        return []
    authors = re.split(r"\s*;\s*|\s+and\s+", value, flags=re.IGNORECASE)
    return [author.strip(" ,;") for author in authors if author.strip(" ,;")]


def _meaningful_title(value: str) -> str:
    title = re.sub(r"\s+", " ", value or "").strip()
    return "" if title.lower() in {"", "untitled", "document", "microsoft word"} else title


def _first_page_title_and_authors(text: str) -> tuple[str, list[str]]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    title_index: int | None = None
    for index, line in enumerate(lines[:20]):
        if not 12 <= len(line) <= 300:
            continue
        if _TITLE_NOISE_RE.match(line) or _DOI_RE.search(line):
            continue
        if line.lower().startswith(("published ", "received ", "accepted ")):
            continue
        title_index = index
        break

    if title_index is None:
        return "", []

    title = lines[title_index]
    authors: list[str] = []
    for line in lines[title_index + 1 : title_index + 4]:
        if _TITLE_NOISE_RE.match(line) or _DOI_RE.search(line):
            break
        if ";" in line or re.search(r"\s+and\s+", line, re.IGNORECASE):
            authors = _split_authors(line)
            break
    return title, authors


def _page_text_layout(page: Any, textpage: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    try:
        page_objects = page.get_objects(textpage=textpage)
    except (AttributeError, TypeError):
        return objects

    for obj in page_objects:
        if not all(hasattr(obj, attr) for attr in ("extract", "get_font_size", "get_bounds")):
            continue
        try:
            text = re.sub(r"\s+", " ", obj.extract() or "").strip()
            font_size = float(obj.get_font_size())
            left, bottom, right, top = (float(value) for value in obj.get_bounds())
        except Exception:
            continue
        width = max(0.0, right - left)
        height = max(0.0, top - bottom)
        if not text or width <= 0 or height <= 0:
            continue
        objects.append({
            "text": text,
            "font_size": font_size,
            "left": left,
            "bottom": bottom,
            "right": right,
            "top": top,
            "width": width,
            "height": height,
            "horizontal": width >= height * 1.5,
        })
    return objects


def _clean_person_name(value: str) -> str:
    value = re.sub(r"[∗*†‡§¶]+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" ,;")


def _looks_like_person_name(value: str) -> bool:
    value = _clean_person_name(value)
    if not value or len(value) > 80 or "@" in value or _AFFILIATION_RE.search(value):
        return False
    if any(char in value for char in (":", ";", "?", "!")):
        return False
    words = value.replace(",", " ").split()
    if not 2 <= len(words) <= 7:
        return False
    for word in words:
        core = word.strip(".()[]{}0123456789")
        if not core or not all(char.isalpha() or char in "-'’" for char in core):
            return False
    return True


def _reading_order_by_visual_rows(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(objects, key=lambda candidate: -candidate["top"]):
        if rows:
            row_top = sum(member["top"] for member in rows[-1]) / len(rows[-1])
            tolerance = max(2.0, item["font_size"] * 0.35)
            if abs(item["top"] - row_top) <= tolerance:
                rows[-1].append(item)
                continue
        rows.append([item])
    ordered: list[dict[str, Any]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda item: item["left"]))
    return ordered


def _layout_title_and_authors(objects: list[dict[str, Any]]) -> tuple[str, list[str]]:
    horizontal = [item for item in objects if item["horizontal"]]
    if not horizontal:
        return "", []

    abstract_items = [
        item for item in horizontal if item["text"].strip().lower() in {"abstract", "summary"}
    ]
    abstract_top = max((item["top"] for item in abstract_items), default=float("-inf"))

    title_candidates = []
    for item in horizontal:
        text = item["text"]
        if not 12 <= len(text) <= 300 or item["bottom"] <= abstract_top:
            continue
        if _TITLE_NOISE_RE.match(text) or _DOI_RE.search(text) or "@" in text:
            continue
        title_candidates.append(item)
    if not title_candidates:
        return "", []

    anchor = max(
        title_candidates,
        key=lambda item: (
            item["font_size"],
            -int(item["text"].endswith(".")),
            -len(item["text"]),
        ),
    )
    title_objects = [
        item
        for item in title_candidates
        if abs(item["font_size"] - anchor["font_size"]) <= 0.5
        and abs(item["top"] - anchor["top"]) <= anchor["font_size"] * 2.2
    ]
    title_objects.sort(key=lambda item: (-item["top"], item["left"]))
    title = " ".join(item["text"] for item in title_objects)

    title_bottom = min(item["bottom"] for item in title_objects)
    author_objects = [
        item
        for item in horizontal
        if abstract_top < item["bottom"]
        and item["top"] < title_bottom
        and _looks_like_person_name(item["text"])
    ]
    authors: list[str] = []
    seen: set[str] = set()
    for item in _reading_order_by_visual_rows(author_objects):
        author = _clean_person_name(item["text"])
        if author.casefold() not in seen:
            seen.add(author.casefold())
            authors.append(author)
    return title, authors


def _select_doi(metadata: dict[str, str], page_texts: list[str]) -> tuple[str, bool]:
    embedded = _doi_candidates(list(metadata.values()))
    if len(embedded) == 1:
        return embedded[0], False
    if len(embedded) > 1:
        return "", True
    text_candidates = _doi_candidates(page_texts)
    return (text_candidates[0], False) if len(text_candidates) == 1 else ("", len(text_candidates) > 1)


def extract_pdf_reference_info(pdf_path: str | Path, max_pages: int = 2) -> dict[str, Any]:
    """Extract local PDF fields, then enrich them with exact DOI metadata."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"PDF path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file: {path}")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as exc:
        raise ValueError(f"Failed to open PDF {path}: {exc}") from exc

    try:
        metadata = {
            str(key): str(value).strip()
            for key, value in pdf.get_metadata_dict(skip_empty=True).items()
            if str(value).strip()
        }
        page_texts: list[str] = []
        first_page_layout: list[dict[str, Any]] = []
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    page_texts.append((textpage.get_text_bounded() or "").strip())
                    if index == 0:
                        first_page_layout = _page_text_layout(page, textpage)
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        pdf.close()

    local_title = _meaningful_title(metadata.get("Title", ""))
    local_authors = _split_authors(metadata.get("Author", ""))
    fallback_title, fallback_authors = _layout_title_and_authors(first_page_layout)
    if not fallback_title:
        fallback_title, fallback_authors = _first_page_title_and_authors(
            page_texts[0] if page_texts else ""
        )
    local_title = local_title or fallback_title
    local_authors = local_authors or fallback_authors

    doi, ambiguous_doi = _select_doi(metadata, page_texts)
    exact = None
    if doi:
        exact = paper_metadata.fetch_exact_metadata({
            "doi": doi,
            "pmid": "",
            "pmcid": "",
            "arxiv_id": "",
        })
    result = paper_metadata.empty_metadata(doi=doi)
    if exact:
        result.update(exact)
    result["title"] = result.get("title") or local_title
    result["authors"] = result.get("authors") or local_authors

    warnings: list[str] = []
    if ambiguous_doi:
        warnings.append("multiple DOI candidates")
    if doi and not exact:
        warnings.append(f"metadata lookup failed for DOI: {doi}")
    if not page_texts or not any(page_texts):
        warnings.append("no selectable text found in the inspected pages")
    return {"file_path": str(path.resolve()), **result, "warnings": warnings}
