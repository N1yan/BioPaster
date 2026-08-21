from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

import requests


USER_AGENT = "BioPaster/1.0 (mailto:{email})"
TIMEOUT = 30
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ID_CONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"


def contact_email() -> str:
    return (
        os.getenv("NCBI_EMAIL")
        or os.getenv("UNPAYWALL_EMAIL")
        or os.getenv("CROSSREF_MAILTO")
        or "biopaster@example.com"
    )


def request_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT.format(email=contact_email())}


def normalize_doi(value: str) -> str:
    value = re.sub(r"^doi\s*:\s*", "", (value or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    return value.strip().lower()


def normalize_pmid(value: str) -> str:
    value = re.sub(r"^pmid\s*:\s*", "", (value or "").strip(), flags=re.IGNORECASE)
    match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", value, re.IGNORECASE)
    return match.group(1) if match else value.strip(" /")


def normalize_pmcid(value: str) -> str:
    match = re.search(r"\bPMC\d+\b", value or "", re.IGNORECASE)
    if match:
        return match.group(0).upper()
    value = re.sub(r"^pmcid\s*:\s*", "", (value or "").strip(), flags=re.IGNORECASE)
    value = value.strip(" /").upper()
    return value if not value or value.startswith("PMC") else f"PMC{value}"


def normalize_arxiv_id(value: str) -> str:
    value = re.sub(r"^arxiv\s*:\s*", "", (value or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", value, flags=re.IGNORECASE)


def select_identifier(
    *,
    pmid: str | None = None,
    doi: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
) -> tuple[str, str]:
    provided = [
        (normalize_pmid(pmid or ""), "pmid"),
        (normalize_doi(doi or ""), "doi"),
        (normalize_pmcid(pmcid or ""), "pmcid"),
        (normalize_arxiv_id(arxiv_id or ""), "arxiv"),
    ]
    provided = [(value, kind) for value, kind in provided if value]
    if len(provided) != 1:
        message = "Provide exactly one of: pmid, doi, pmcid, arxiv_id"
        raise ValueError(message if not provided else "Provide only one identifier")
    return provided[0]


def ncbi_get(endpoint: str, params: dict[str, Any]) -> requests.Response:
    merged = {**params, "email": contact_email()}
    if api_key := os.getenv("NCBI_API_KEY", "").strip():
        merged["api_key"] = api_key
    response = requests.get(
        NCBI_BASE + endpoint,
        params=merged,
        headers=request_headers(),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response


def resolve_ncbi_ids(*, pmid: str = "", doi: str = "", pmcid: str = "") -> dict[str, str]:
    identity = {
        "pmid": normalize_pmid(pmid),
        "pmcid": normalize_pmcid(pmcid),
        "doi": normalize_doi(doi),
        "arxiv_id": "",
    }
    lookup = identity["pmid"] or identity["doi"] or identity["pmcid"]
    if not lookup:
        return identity
    try:
        query = urlencode(
            {"ids": lookup, "format": "json", "tool": "BioPaster", "email": contact_email()}
        )
        request = urllib.request.Request(f"{ID_CONV}?{query}", headers=request_headers())
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        record = (payload.get("records") or [{}])[0]
        if isinstance(record, dict) and record.get("status") != "error":
            identity["pmid"] = normalize_pmid(str(record.get("pmid") or identity["pmid"]))
            identity["pmcid"] = normalize_pmcid(str(record.get("pmcid") or identity["pmcid"]))
            identity["doi"] = normalize_doi(str(record.get("doi") or identity["doi"]))
    except (urllib.error.URLError, ValueError, json.JSONDecodeError):
        pass
    return identity


def metadata_matches(identity: dict[str, str], metadata: dict[str, Any]) -> bool:
    keys = ("doi", "pmid", "pmcid", "arxiv_id")
    comparable = [key for key in keys if identity.get(key) and metadata.get(key)]
    return bool(comparable) and all(identity[key] == metadata[key] for key in comparable)
