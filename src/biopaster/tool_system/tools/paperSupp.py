# from __future__ import annotations

# import io
# import json
# import os
# import re
# import urllib.error
# import urllib.request
# import xml.etree.ElementTree as ET
# import zipfile
# from pathlib import Path
# from typing import Any
# from urllib.parse import urlencode, urljoin, urlparse

# from tool_system.protocol import ToolResult
# from tool_system.registry import ToolSpec

# USER_AGENT = "BioPaster/1.0 (mailto:{email})"
# TIMEOUT = 90
# ID_CONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
# NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
# EUROPEPMC_SUPP = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
# PMC_BIN_BASE = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/bin/"

# _SKIP_ZIP_NAMES = {".ds_store", "thumbs.db"}
# _XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


# def _email() -> str:
#     return (
#         os.getenv("NCBI_EMAIL")
#         or os.getenv("UNPAYWALL_EMAIL")
#         or os.getenv("CROSSREF_MAILTO")
#         or "biopaster@example.com"
#     )


# def _headers() -> dict[str, str]:
#     return {"User-Agent": USER_AGENT.format(email=_email())}


# def _http_get_bytes(url: str) -> tuple[str, str, bytes]:
#     req = urllib.request.Request(url, headers=_headers())
#     with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
#         raw = resp.read()
#         content_type = (resp.headers.get_content_type() or "").lower()
#         return resp.geturl(), content_type, raw


# def _norm_doi(doi: str) -> str:
#     return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.IGNORECASE)


# def _display_pmcid(pmcid: str) -> str:
#     pmc = (pmcid or "").strip().upper().replace("PMC", "")
#     return f"PMC{pmc}" if pmc else ""


# def _select_identifier(
#     *,
#     pmid: str | None = None,
#     doi: str | None = None,
#     pmcid: str | None = None,
#     arxiv_id: str | None = None,
# ) -> tuple[str, str]:
#     provided: list[tuple[str, str]] = []
#     if (v := (pmid or "").strip()):
#         provided.append((v, "pmid"))
#     if (v := _norm_doi(doi or "")):
#         provided.append((v, "doi"))
#     if (v := (pmcid or "").strip()):
#         provided.append((_display_pmcid(v), "pmcid"))
#     if (v := (arxiv_id or "").strip()):
#         provided.append((re.sub(r"v\d+$", "", v), "arxiv"))
#     if not provided:
#         raise ValueError("Provide exactly one of: pmid, doi, pmcid, arxiv_id")
#     if len(provided) > 1:
#         raise ValueError("Provide only one identifier")
#     return provided[0]


# def _resolve_ncbi_ids(
#     *,
#     pmid: str = "",
#     doi: str = "",
#     pmcid: str = "",
# ) -> dict[str, str]:
#     canonical = {"pmid": pmid, "pmcid": pmcid, "doi": doi, "arxiv_id": ""}
#     lookup = pmid or doi or pmcid
#     if not lookup:
#         return canonical
#     try:
#         query = urlencode(
#             {"ids": lookup, "format": "json", "tool": "BioPaster", "email": _email()}
#         )
#         _, _, raw = _http_get_bytes(f"{ID_CONV}?{query}")
#         record = (json.loads(raw.decode("utf-8")).get("records") or [{}])[0]
#         if isinstance(record, dict) and record.get("status") != "error":
#             if v := str(record.get("pmid") or "").strip():
#                 canonical["pmid"] = v
#             if v := str(record.get("pmcid") or "").strip():
#                 canonical["pmcid"] = _display_pmcid(v)
#             if v := str(record.get("doi") or "").strip():
#                 canonical["doi"] = v
#     except (urllib.error.URLError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
#         pass
#     return canonical


# def _safe_filename(name: str) -> str:
#     base = Path(name.replace("\\", "/")).name
#     if not base or base in {".", ".."} or base.lower() in _SKIP_ZIP_NAMES:
#         return ""
#     if base.startswith("__macosx"):
#         return ""
#     return re.sub(r"[^\w.\-]+", "_", base).strip("._")


# def _unique_path(dest_dir: Path, filename: str) -> Path:
#     path = dest_dir / filename
#     if not path.exists():
#         return path
#     stem, suffix = path.stem, path.suffix
#     for i in range(2, 1000):
#         candidate = dest_dir / f"{stem}_{i}{suffix}"
#         if not candidate.exists():
#             return candidate
#     raise ValueError(f"Could not find a free filename for {filename}")


# def _write_files(dest_dir: Path, items: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
#     dest_dir.mkdir(parents=True, exist_ok=True)
#     written: list[dict[str, Any]] = []
#     for name, content in items:
#         filename = _safe_filename(name)
#         if not filename or not content:
#             continue
#         path = _unique_path(dest_dir, filename)
#         path.write_bytes(content)
#         written.append({"name": path.name, "path": str(path.resolve()), "bytes": len(content)})
#     return written


# def _europepmc_files(pmcid: str, *, include_images: bool) -> tuple[list[tuple[str, bytes]], str, str | None]:
#     query = urlencode({"includeInlineImage": "true" if include_images else "false"})
#     url = f"{EUROPEPMC_SUPP.format(pmcid=pmcid)}?{query}"
#     try:
#         final_url, content_type, raw = _http_get_bytes(url)
#     except urllib.error.HTTPError as exc:
#         if exc.code == 404:
#             return [], url, None
#         raise
#     if not raw.startswith(b"PK"):
#         detail = ""
#         if b"<errorBean" in raw[:500] or "xml" in content_type:
#             try:
#                 root = ET.fromstring(raw)
#                 detail = " ".join(t.strip() for t in root.itertext() if t.strip())
#             except ET.ParseError:
#                 detail = raw[:200].decode("utf-8", errors="replace")
#         return [], final_url, detail or "Europe PMC did not return a zip"
#     out: list[tuple[str, bytes]] = []
#     with zipfile.ZipFile(io.BytesIO(raw)) as zf:
#         for info in zf.infolist():
#             if info.is_dir():
#                 continue
#             name = _safe_filename(info.filename)
#             if not name:
#                 continue
#             out.append((name, zf.read(info)))
#     return out, final_url, None


# def _local_name_from_href(href: str) -> str:
#     path = urlparse(href).path or href
#     return _safe_filename(path) or "supplement"


# def _jats_hrefs(xml_text: str) -> list[str]:
#     root = ET.fromstring(xml_text)
#     hrefs: list[str] = []
#     seen: set[str] = set()
#     for elem in root.iter():
#         if elem.tag.rsplit("}", 1)[-1] != "supplementary-material":
#             continue
#         for child in elem.iter():
#             href = (child.attrib.get(_XLINK_HREF) or child.attrib.get("href") or "").strip()
#             if not href or href.startswith("#") or href in seen:
#                 continue
#             seen.add(href)
#             hrefs.append(href)
#     return hrefs


# def _jats_bin_files(pmcid: str) -> tuple[list[tuple[str, bytes]], str, str | None]:
#     numeric = pmcid.upper().replace("PMC", "")
#     query = urlencode(
#         {
#             "db": "pmc",
#             "id": numeric,
#             "retmode": "xml",
#             "tool": "BioPaster",
#             "email": _email(),
#         }
#     )
#     xml_url = f"{NCBI_EFETCH}?{query}"
#     try:
#         _, _, xml_raw = _http_get_bytes(xml_url)
#         xml_text = xml_raw.decode("utf-8", errors="replace")
#         hrefs = _jats_hrefs(xml_text)
#     except (urllib.error.URLError, ET.ParseError, UnicodeDecodeError) as exc:
#         return [], xml_url, str(exc)
#     if not hrefs:
#         return [], xml_url, "JATS contained no supplementary-material hrefs"

#     files: list[tuple[str, bytes]] = []
#     errors: list[str] = []
#     for href in hrefs:
#         name = _local_name_from_href(href)
#         url = href if href.startswith("http") else urljoin(PMC_BIN_BASE.format(pmcid=pmcid), name)
#         try:
#             _, _, raw = _http_get_bytes(url)
#         except urllib.error.URLError as exc:
#             errors.append(f"{url}: {exc}")
#             continue
#         files.append((name, raw))
#     if files:
#         return files, xml_url, None
#     return [], xml_url, "; ".join(errors) or "No supplementary files could be downloaded from PMC bin/"


# def download_paper_supp(
#     *,
#     pmid: str | None = None,
#     doi: str | None = None,
#     pmcid: str | None = None,
#     arxiv_id: str | None = None,
#     save_dir: str | None = None,
#     include_images: bool = False,
# ) -> dict[str, Any]:
#     try:
#         id_value, id_type = _select_identifier(pmid=pmid, doi=doi, pmcid=pmcid, arxiv_id=arxiv_id)
#     except ValueError as exc:
#         return {"status": "error", "error": str(exc)}

#     canonical = {"pmid": "", "pmcid": "", "doi": "", "arxiv_id": ""}
#     if id_type == "pmid":
#         canonical["pmid"] = id_value
#     elif id_type == "doi":
#         canonical["doi"] = id_value
#     elif id_type == "pmcid":
#         canonical["pmcid"] = id_value
#     else:
#         canonical["arxiv_id"] = id_value

#     if id_type != "arxiv":
#         canonical = _resolve_ncbi_ids(
#             pmid=canonical["pmid"],
#             doi=canonical["doi"],
#             pmcid=canonical["pmcid"],
#         )
#         canonical["arxiv_id"] = ""
#     elif not canonical["pmcid"]:
#         canonical = _resolve_ncbi_ids(doi=f"10.48550/arXiv.{id_value}")
#         canonical["arxiv_id"] = id_value

#     pmc = _display_pmcid(canonical.get("pmcid") or "")
#     if not pmc:
#         return {
#             "status": "not_found",
#             "identifier": id_value,
#             "id_type": id_type,
#             "canonical_ids": canonical,
#             "error": "Supplementary files need a PMCID (Europe PMC / PMC OA). This identifier did not resolve to one.",
#         }

#     dest_dir = Path(save_dir or "papers/supp") / pmc
#     attempts: list[dict[str, Any]] = []

#     try:
#         files, url, err = _europepmc_files(pmc, include_images=include_images)
#     except urllib.error.URLError as exc:
#         files, url, err = [], EUROPEPMC_SUPP.format(pmcid=pmc), str(exc)
#     if files:
#         written = _write_files(dest_dir, files)
#         if written:
#             return {
#                 "status": "ok",
#                 "identifier": id_value,
#                 "id_type": id_type,
#                 "canonical_ids": canonical,
#                 "source": "europepmc",
#                 "url": url,
#                 "save_dir": str(dest_dir.resolve()),
#                 "files": written,
#                 "attempts": attempts,
#             }
#     attempts.append({"source": "europepmc", "url": url, "status": "not_found" if not err else "fetch_failed", "error": err})

#     try:
#         files, url, err = _jats_bin_files(pmc)
#     except urllib.error.URLError as exc:
#         files, url, err = [], NCBI_EFETCH, str(exc)
#     if files:
#         written = _write_files(dest_dir, files)
#         if written:
#             return {
#                 "status": "ok",
#                 "identifier": id_value,
#                 "id_type": id_type,
#                 "canonical_ids": canonical,
#                 "source": "pmc_jats",
#                 "url": url,
#                 "save_dir": str(dest_dir.resolve()),
#                 "files": written,
#                 "attempts": attempts,
#             }
#     attempts.append({"source": "pmc_jats", "url": url, "status": "not_found" if not err else "fetch_failed", "error": err})

#     return {
#         "status": "not_found",
#         "identifier": id_value,
#         "id_type": id_type,
#         "canonical_ids": canonical,
#         "error": "No supplementary files found in Europe PMC or PMC JATS.",
#         "attempts": attempts,
#     }


# class paperSuppTool:
#     def spec(self) -> ToolSpec:
#         return ToolSpec(
#             name="paperSupp",
#             description=(
#                 "Download supplementary files for a paper. Provide exactly one of pmid, doi, "
#                 "pmcid, or arxiv_id. Uses Europe PMC's official zip, then PMC JATS "
#                 "supplementary-material links. Requires a resolvable PMCID. Saves files and "
#                 "returns local paths. Use paperFetch for text, paperPdf for the article PDF."
#             ),
#             input_schema={
#                 "type": "object",
#                 "additionalProperties": False,
#                 "properties": {
#                     "pmid": {"type": "string"},
#                     "doi": {"type": "string"},
#                     "pmcid": {"type": "string"},
#                     "arxiv_id": {"type": "string"},
#                     "save_dir": {
#                         "type": "string",
#                         "description": "Parent directory. Files go in {save_dir}/{PMCID}/. Default: papers/supp",
#                     },
#                     "include_images": {
#                         "type": "boolean",
#                         "default": False,
#                         "description": "If true, Europe PMC zip also includes inline article figures.",
#                     },
#                 },
#             },
#             is_read_only=False,
#             max_result_size_chars=8_000,
#         )

#     def run(self, tool_input: dict[str, Any]) -> ToolResult:
#         payload = download_paper_supp(
#             pmid=tool_input.get("pmid"),
#             doi=tool_input.get("doi"),
#             pmcid=tool_input.get("pmcid"),
#             arxiv_id=tool_input.get("arxiv_id"),
#             save_dir=tool_input.get("save_dir"),
#             include_images=bool(tool_input.get("include_images") or False),
#         )
#         return ToolResult(
#             name="paperSupp",
#             output=payload,
#             is_error=payload.get("status") in {"error", "fetch_failed", "not_found"},
#         )
