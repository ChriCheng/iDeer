#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any


# Zotero 9 local connector server.
# Notes:
# - GET /api/users/0/... works for reading/searching.
# - In Zotero 9 local mode, POST /api/users/0/items may return:
#   "Endpoint does not support method".
# - Therefore item saving is done through /connector/saveItems.
ZOTERO_HOST = "http://127.0.0.1:23119"
ZOTERO_LOCAL_API = f"{ZOTERO_HOST}/api/users/0"
ZOTERO_CONNECTOR_SAVE_ITEMS = f"{ZOTERO_HOST}/connector/saveItems"

READ_HEADERS = {
    "Content-Type": "application/json",
    "Zotero-API-Version": "3",
    "Zotero-Allowed-Request": "true",
}

CONNECTOR_HEADERS = {
    "Content-Type": "application/json",
    "Zotero-Allowed-Request": "true",
}


def http_json(
    method: str,
    url: str,
    data: Any | None = None,
    headers: dict | None = None,
    timeout: int = 30,
) -> Any:
    body = (
        json.dumps(data, ensure_ascii=False).encode("utf-8")
        if data is not None
        else None
    )
    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {})
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {e.code} {e.reason} for {method} {url}\n"
            f"Request body: {json.dumps(data, ensure_ascii=False)[:2000]}\n"
            f"Response body: {err_body[:2000]}"
        ) from e


def http_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "iDeer-zotero-save/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def zotero_get(path: str) -> Any:
    return http_json("GET", ZOTERO_LOCAL_API + path, headers=READ_HEADERS)


def zotero_post_api(path: str, data: Any) -> Any:
    """Best-effort Zotero local API write. Some Zotero 9 endpoints may reject POST."""
    return http_json(
        "POST", ZOTERO_LOCAL_API + path, data=data, headers=READ_HEADERS, timeout=30
    )


def split_csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_doi(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.strip()


def extract_doi_from_url(url: str) -> str:
    decoded = urllib.parse.unquote(str(url or "").strip())
    m = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", decoded, flags=re.I)
    if not m:
        return ""
    return normalize_doi(m.group(1).rstrip(").,;]"))


def extract_arxiv_id(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""

    patterns = [
        r"arxiv\.org/abs/([^?#]+)",
        r"arxiv\.org/pdf/([^?#]+)",
        r"arxiv:([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url, flags=re.I)
        if m:
            return m.group(1).strip().removesuffix(".pdf")
    return ""


def infer_pdf_url(
    url: str, pdf_url: str = "", doi: str = "", arxiv_id: str = ""
) -> str:
    """Infer a direct PDF URL when possible.

    Reliable:
    - arXiv abs/pdf URLs

    Best effort:
    - existing direct .pdf URLs

    Not attempted:
    - Semantic Scholar pages without arXiv/PDF metadata
    - publisher pages behind login/CAPTCHA
    """
    pdf_url = clean_text(pdf_url)
    if pdf_url:
        return pdf_url

    url = clean_text(url)
    arxiv_id = clean_text(arxiv_id) or extract_arxiv_id(url)

    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith(".pdf"):
        return url

    return ""


def parse_author_name(name: str) -> dict:
    name = clean_text(name)
    if not name:
        return {"creatorType": "author", "firstName": "", "lastName": ""}

    if "," in name:
        last, first = [x.strip() for x in name.split(",", 1)]
        return {"creatorType": "author", "firstName": first, "lastName": last}

    parts = name.split()
    if len(parts) >= 2:
        return {
            "creatorType": "author",
            "firstName": " ".join(parts[:-1]),
            "lastName": parts[-1],
        }

    return {"creatorType": "author", "firstName": "", "lastName": name}


def parse_authors(authors: str | list[str]) -> list[dict]:
    names = authors if isinstance(authors, list) else split_csv(authors)
    creators = []
    for name in names:
        creator = parse_author_name(name)
        if creator.get("firstName") or creator.get("lastName"):
            creators.append(creator)
    return creators


def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    if not arxiv_id:
        return {}

    api_url = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(
        arxiv_id
    )
    try:
        text = http_text(api_url, timeout=20)
    except Exception:
        return {}

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}

    entry = root.find("atom:entry", ns)
    if entry is None:
        return {}

    title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
    abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
    published = clean_text(entry.findtext("atom:published", default="", namespaces=ns))

    authors = []
    for author in entry.findall("atom:author", ns):
        name = clean_text(author.findtext("atom:name", default="", namespaces=ns))
        if name:
            authors.append(name)

    doi = ""
    doi_node = entry.find("arxiv:doi", ns)
    if doi_node is not None and doi_node.text:
        doi = normalize_doi(doi_node.text)

    extra_lines = [f"arXiv: {arxiv_id}"]
    if doi:
        extra_lines.append(f"DOI: {doi}")

    return {
        "itemType": "journalArticle",
        "title": title or f"arXiv:{arxiv_id}",
        "abstractNote": abstract,
        "date": published[:10] if published else "",
        "creators": parse_authors(authors),
        "DOI": doi,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "archive": "arXiv",
        "extra": "\n".join(extra_lines),
    }


def fetch_crossref_metadata(doi: str) -> dict:
    doi = normalize_doi(doi)
    if not doi:
        return {}

    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    try:
        data = http_json(
            "GET",
            url,
            headers={
                "User-Agent": "iDeer-zotero-save/1.0 (mailto:unknown@example.com)"
            },
            timeout=20,
        )
    except Exception:
        return {}

    msg = data.get("message", {})
    if not isinstance(msg, dict):
        return {}

    title_list = msg.get("title") or []
    container_list = msg.get("container-title") or []
    title = clean_text(title_list[0]) if title_list else doi
    publication_title = clean_text(container_list[0]) if container_list else ""

    creators = []
    for author in msg.get("author") or []:
        first = clean_text(author.get("given", ""))
        last = clean_text(author.get("family", ""))
        if first or last:
            creators.append(
                {"creatorType": "author", "firstName": first, "lastName": last}
            )

    date = ""
    issued = msg.get("issued", {}).get("date-parts") or []
    if issued and issued[0]:
        date = "-".join(str(x) for x in issued[0])

    return {
        "itemType": "journalArticle",
        "title": title,
        "publicationTitle": publication_title,
        "date": date,
        "creators": creators,
        "DOI": doi,
        "url": msg.get("URL", f"https://doi.org/{doi}"),
        "abstractNote": clean_text(msg.get("abstract", "")),
        "volume": str(msg.get("volume", "") or ""),
        "issue": str(msg.get("issue", "") or ""),
        "pages": str(msg.get("page", "") or ""),
    }


def _extra_contains_arxiv(extra: str, arxiv_id: str) -> bool:
    return bool(
        arxiv_id
        and re.search(rf"\barXiv:\s*{re.escape(arxiv_id)}\b", extra, flags=re.I)
    )


def search_existing_items(query: str) -> list[dict]:
    query = clean_text(query)
    if not query:
        return []

    encoded = urllib.parse.quote(query)
    try:
        result = zotero_get(f"/items/top?limit=25&q={encoded}")
    except Exception:
        return []
    return result if isinstance(result, list) else []


def item_exists(
    title: str = "", url: str = "", doi: str = "", arxiv_id: str = ""
) -> bool:
    candidates = [doi, arxiv_id, url, title]
    for query in candidates:
        for item in search_existing_items(query):
            data = item.get("data", {})
            existing_doi = normalize_doi(data.get("DOI", ""))
            existing_title = clean_text(data.get("title", "")).lower()
            existing_url = clean_text(data.get("url", ""))
            existing_extra = clean_text(data.get("extra", ""))

            if (
                doi
                and existing_doi
                and existing_doi.lower() == normalize_doi(doi).lower()
            ):
                return True
            if arxiv_id and _extra_contains_arxiv(existing_extra, arxiv_id):
                return True
            if url and existing_url and existing_url.rstrip("/") == url.rstrip("/"):
                return True
            if title and existing_title and existing_title == clean_text(title).lower():
                return True
    return False


def list_collections(query: str = "") -> list[dict]:
    path = "/collections?limit=100"
    if query:
        path += "&q=" + urllib.parse.quote(query)
    try:
        result = zotero_get(path)
    except Exception:
        return []
    return result if isinstance(result, list) else []


def find_collection_key(name: str, parent_key: str | None = None) -> str | None:
    """Find collection by name. If parent_key is provided, prefer exact child match."""
    name = clean_text(name)
    if not name:
        return None

    candidates = list_collections(name)
    fallback = None

    for collection in candidates:
        data = collection.get("data", {})
        if data.get("name") != name:
            continue

        key = collection.get("key") or data.get("key")
        parent = data.get("parentCollection") or False

        if parent_key is None:
            # Top-level preferred, but any same-name collection is usable as fallback.
            if parent in (False, None, ""):
                return key
            fallback = fallback or key
        else:
            if parent == parent_key:
                return key
            fallback = fallback or key

    return fallback


def create_collection(name: str, parent_key: str | None = None) -> str | None:
    """Best-effort local collection creation.

    Zotero 9 local API may reject collection writes. If so, return None and let
    /connector/saveItems save to the currently selected collection.
    """
    payload = {"name": clean_text(name)}
    if parent_key:
        payload["parentCollection"] = parent_key

    try:
        result = zotero_post_api("/collections", [payload])
    except Exception as e:
        print(
            f"[zotero_save] Cannot create collection {name!r} via local API: {e}",
            file=sys.stderr,
        )
        return None

    successful = result.get("successful", {})
    if successful:
        first = next(iter(successful.values()))
        return first.get("key")

    unchanged = result.get("unchanged", {})
    if unchanged:
        first = next(iter(unchanged.values()))
        return first.get("key")

    print(
        f"[zotero_save] Collection creation returned unexpected result: {result}",
        file=sys.stderr,
    )
    return None


def get_or_create_collection(name: str, parent_key: str | None = None) -> str | None:
    key = find_collection_key(name, parent_key=parent_key)
    if key:
        return key
    return create_collection(name, parent_key=parent_key)


def build_minimal_item(args) -> dict:
    title = (
        clean_text(args.title)
        or normalize_doi(args.doi)
        or clean_text(args.url)
        or "Untitled"
    )
    doi = normalize_doi(args.doi) or extract_doi_from_url(args.url)
    arxiv_id = clean_text(getattr(args, "arxiv_id", "")) or extract_arxiv_id(args.url)

    extra_lines = []
    if arxiv_id:
        extra_lines.append(f"arXiv: {arxiv_id}")
    if doi:
        extra_lines.append(f"DOI: {doi}")

    return {
        "itemType": "journalArticle",
        "title": title,
        "url": clean_text(args.url),
        "DOI": doi,
        "creators": parse_authors(args.authors),
        "abstractNote": clean_text(args.abstract),
        "archive": "arXiv" if arxiv_id else "",
        "extra": "\n".join(extra_lines),
    }


def enrich_metadata(args) -> dict:
    doi = normalize_doi(args.doi) or extract_doi_from_url(args.url)
    arxiv_id = clean_text(getattr(args, "arxiv_id", "")) or extract_arxiv_id(args.url)

    metadata = {}
    if arxiv_id:
        metadata = fetch_arxiv_metadata(arxiv_id)
    if not metadata and doi:
        metadata = fetch_crossref_metadata(doi)

    fallback = build_minimal_item(args)
    merged = dict(fallback)
    for key, value in metadata.items():
        if value not in ("", None, [], {}):
            merged[key] = value

    if args.title and not merged.get("title"):
        merged["title"] = clean_text(args.title)

    return merged


def build_pdf_attachment(pdf_url: str) -> dict | None:
    pdf_url = clean_text(pdf_url)
    if not pdf_url:
        return None
    return {
        "title": "Full Text PDF",
        "url": pdf_url,
        "mimeType": "application/pdf",
        "snapshot": True,
    }


def sanitize_connector_item(item: dict) -> dict:
    """Keep fields that Zotero Connector saveItems accepts."""
    allowed_fields = {
        "itemType",
        "title",
        "creators",
        "abstractNote",
        "publicationTitle",
        "volume",
        "issue",
        "pages",
        "date",
        "journalAbbreviation",
        "language",
        "DOI",
        "ISSN",
        "shortTitle",
        "url",
        "accessDate",
        "archive",
        "archiveLocation",
        "libraryCatalog",
        "rights",
        "extra",
        "tags",
        "attachments",
        "notes",
        "seeAlso",
    }

    cleaned = {k: v for k, v in item.items() if k in allowed_fields}
    cleaned = {k: v for k, v in cleaned.items() if v not in ("", None, [], {})}

    creators = []
    for creator in cleaned.get("creators", []) or []:
        if not isinstance(creator, dict):
            continue
        if not creator.get("creatorType"):
            creator["creatorType"] = "author"
        if creator.get("lastName") or creator.get("firstName") or creator.get("name"):
            creators.append(creator)
    if creators:
        cleaned["creators"] = creators
    else:
        cleaned.pop("creators", None)

    cleaned.setdefault("attachments", [])
    cleaned.setdefault("notes", [])
    cleaned.setdefault("seeAlso", [])
    return cleaned


def save_items_via_connector(
    item: dict, source_url: str, collection_key: str | None = None
) -> Any:
    """Use Zotero Connector's write endpoint.

    The collection placement is best-effort. In Zotero Connector mode, Zotero may
    save to the currently selected collection even when a collection key is sent.
    """
    session_id = f"ideer-{uuid.uuid4().hex}"

    item_with_collection = dict(item)
    if collection_key:
        # Best effort; some connector versions ignore this field.
        item_with_collection["collections"] = [collection_key]

    payload = {
        "sessionID": session_id,
        "uri": source_url or item.get("url", ""),
        "items": [item_with_collection],
        "saveOptions": {
            "downloadAssociatedFiles": True,
            "automaticSnapshots": True,
        },
    }

    if collection_key:
        # Also best effort; harmless if ignored.
        payload["target"] = {"libraryID": 0, "collections": [collection_key]}

    try:
        return http_json(
            "POST",
            ZOTERO_CONNECTOR_SAVE_ITEMS,
            data=payload,
            headers=CONNECTOR_HEADERS,
            timeout=90,
        )
    except Exception as first_error:
        # Retry without target/collections if connector rejects them.
        payload = {
            "sessionID": session_id,
            "uri": source_url or item.get("url", ""),
            "items": [item],
            "saveOptions": {
                "downloadAssociatedFiles": True,
                "automaticSnapshots": True,
            },
        }
        try:
            return http_json(
                "POST",
                ZOTERO_CONNECTOR_SAVE_ITEMS,
                data=payload,
                headers=CONNECTOR_HEADERS,
                timeout=90,
            )
        except Exception as second_error:
            raise RuntimeError(
                f"saveItems failed twice.\nFirst: {first_error}\nSecond: {second_error}"
            ) from second_error


def save_one_paper(
    *,
    title: str = "",
    url: str = "",
    doi: str = "",
    arxiv_id: str = "",
    pdf_url: str = "",
    authors: str = "",
    abstract: str = "",
    collection_name: str = "",
    collection_key: str | None = None,
    tags: str = "iDeer",
    allow_duplicates: bool = False,
) -> bool:
    args = SimpleNamespace(
        title=title,
        url=url,
        doi=doi,
        arxiv_id=arxiv_id,
        pdf_url=pdf_url,
        authors=authors,
        abstract=abstract,
    )

    item = enrich_metadata(args)

    item_title = clean_text(item.get("title", ""))
    item_url = clean_text(item.get("url", ""))
    item_doi = normalize_doi(item.get("DOI", ""))
    item_arxiv_id = clean_text(arxiv_id) or extract_arxiv_id(item_url)

    if not allow_duplicates and item_exists(
        title=item_title,
        url=item_url,
        doi=item_doi,
        arxiv_id=item_arxiv_id,
    ):
        print(f"[zotero_save] Already exists: {item_title or item_doi or item_url}")
        return True

    tag_values = split_csv(tags)
    if tag_values:
        item["tags"] = [{"tag": tag} for tag in tag_values]

    inferred_pdf_url = infer_pdf_url(
        item_url,
        pdf_url=pdf_url,
        doi=item_doi,
        arxiv_id=item_arxiv_id,
    )
    pdf_attachment = build_pdf_attachment(inferred_pdf_url)
    if pdf_attachment:
        item["attachments"] = [pdf_attachment]

    if collection_key is None and collection_name:
        collection_key = find_collection_key(collection_name)

    item = sanitize_connector_item(item)

    try:
        save_items_via_connector(
            item, source_url=item_url, collection_key=collection_key
        )
        if inferred_pdf_url:
            print(
                f"[zotero_save] Saved with PDF attachment: {item_title or item_doi or item_url}"
            )
        else:
            print(f"[zotero_save] Saved: {item_title or item_doi or item_url}")
        return True
    except Exception as e:
        print(
            f"[zotero_save] Full metadata save failed, retrying minimal connector item. Detail: {e}",
            file=sys.stderr,
        )

    minimal = {
        "itemType": "journalArticle",
        "title": item_title or item_url or item_doi or "Untitled",
        "url": item_url,
        "DOI": item_doi,
        "tags": [{"tag": tag} for tag in tag_values],
        "attachments": [pdf_attachment] if pdf_attachment else [],
        "notes": [],
        "seeAlso": [],
    }
    if item_arxiv_id:
        minimal["extra"] = f"arXiv: {item_arxiv_id}"

    minimal = sanitize_connector_item(minimal)
    save_items_via_connector(
        minimal, source_url=item_url, collection_key=collection_key
    )

    if inferred_pdf_url:
        print(
            f"[zotero_save] Saved minimal with PDF attachment: {item_title or item_doi or item_url}"
        )
    else:
        print(f"[zotero_save] Saved minimal: {item_title or item_doi or item_url}")

    return True


def compact_history_date(run_date: str) -> str:
    # history date is YYYY-MM-DD; date collection is YY.M.D, e.g. 2026-05-02 -> 26.5.2
    y, m, d = run_date.split("-")
    return f"{int(y) % 100}.{int(m)}.{int(d)}"


def discover_history_dates(history_dir: Path, sources: list[str]) -> list[str]:
    dates: set[str] = set()

    for source in sources:
        source_dir = history_dir / source
        if not source_dir.exists():
            continue
        for child in source_dir.iterdir():
            if not child.is_dir():
                continue
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", child.name):
                continue
            json_dir = child / "json"
            if json_dir.exists() and any(json_dir.glob("*.json")):
                dates.add(child.name)

    return sorted(dates, reverse=True)


def load_items_from_history(
    history_dir: Path, sources: list[str], run_date: str
) -> list[dict]:
    items: list[dict] = []

    for source in sources:
        json_dir = history_dir / source / run_date / "json"
        if not json_dir.exists():
            print(f"[zotero_import] Skip missing directory: {json_dir}")
            continue

        for path in json_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[zotero_import] Failed to read {path}: {e}", file=sys.stderr)
                continue

            if isinstance(item, dict):
                item["_source"] = source
                item["_path"] = str(path)
                items.append(item)

    return items


def infer_arxiv_id_from_item(item: dict) -> str:
    arxiv_id = clean_text(item.get("arxiv_id", ""))
    if arxiv_id:
        return arxiv_id
    return extract_arxiv_id(str(item.get("url", "") or ""))


def infer_pdf_url_from_item(item: dict) -> str:
    pdf_url = clean_text(item.get("pdf_url", ""))
    if pdf_url:
        return pdf_url

    arxiv_id = infer_arxiv_id_from_item(item)
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    url = clean_text(item.get("url", ""))
    if urllib.parse.urlparse(url).path.lower().endswith(".pdf"):
        return url

    return ""


def select_history_items(
    items: list[dict], min_score: float, max_per_source: int
) -> list[dict]:
    by_source: dict[str, list[dict]] = {}

    for item in items:
        source = str(item.get("_source", "unknown"))
        by_source.setdefault(source, []).append(item)

    selected: list[dict] = []

    for source, source_items in by_source.items():
        source_items.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)

        count = 0
        for item in source_items:
            score = float(item.get("score", 0) or 0)
            if score < min_score:
                continue
            if count >= max_per_source:
                break
            selected.append(item)
            count += 1

    selected.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    return selected


def import_history(args) -> int:
    history_dir = Path(args.history_dir)
    sources = args.sources

    dates = discover_history_dates(history_dir, sources)
    if not dates:
        print(
            f"[zotero_import] No history JSON found under {history_dir} for sources={sources}",
            file=sys.stderr,
        )
        return 1

    run_date = args.date or dates[0]
    if run_date not in dates:
        print(
            f"[zotero_import] Warning: date {run_date} not discovered in history date list: {dates[:5]}",
            file=sys.stderr,
        )

    date_collection = args.date_collection or compact_history_date(run_date)
    parent_collection = (
        args.parent_collection or args.collection or "iDeer Daily Papers"
    )

    print(f"[zotero_import] History date: {run_date}")
    print(f"[zotero_import] Parent collection: {parent_collection}")
    print(f"[zotero_import] Date collection: {date_collection}")

    parent_key = get_or_create_collection(parent_collection)
    date_key = (
        get_or_create_collection(date_collection, parent_key=parent_key)
        if parent_key
        else find_collection_key(date_collection)
    )

    if not date_key:
        print(
            "[zotero_import] Could not create/find the date collection through local API.\n"
            f"[zotero_import] Please manually create/select: {parent_collection}/{date_collection}\n"
            "[zotero_import] The connector may save items to Zotero's currently selected collection.",
            file=sys.stderr,
        )

    raw_items = load_items_from_history(history_dir, sources, run_date)
    print(f"[zotero_import] Loaded {len(raw_items)} items from history JSON.")

    selected = select_history_items(
        raw_items,
        min_score=args.min_score,
        max_per_source=args.max_per_source,
    )
    print(
        f"[zotero_import] Selected {len(selected)} items after score/source filtering."
    )

    ok = 0
    failed = 0
    seen_keys: set[str] = set()

    for item in selected:
        source = str(item.get("_source", "unknown"))
        title = clean_text(item.get("title", ""))
        url = clean_text(item.get("url", ""))
        doi = normalize_doi(item.get("doi", ""))
        arxiv_id = infer_arxiv_id_from_item(item)
        pdf_url = infer_pdf_url_from_item(item)
        authors = clean_text(item.get("authors", ""))
        abstract = clean_text(item.get("abstract", ""))
        score = item.get("score", 0)

        key = (doi or arxiv_id or url or title).lower()
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)

        tags = f"iDeer,{source},{date_collection}"

        print(f"[zotero_import] {source} | score={score} | {title[:90]}")
        if pdf_url:
            print(f"                PDF: {pdf_url}")

        if args.dry_run:
            ok += 1
            continue

        try:
            if save_one_paper(
                title=title,
                url=url,
                doi=doi,
                arxiv_id=arxiv_id,
                pdf_url=pdf_url,
                authors=authors,
                abstract=abstract,
                collection_name=date_collection,
                collection_key=date_key,
                tags=tags,
                allow_duplicates=args.allow_duplicates,
            ):
                ok += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"[zotero_import] Failed: {title} — {e}", file=sys.stderr)

    print(
        f"[zotero_import] Done. ok={ok}, failed={failed}, collection={date_collection}"
    )
    return 0 if failed == 0 else 1


def save_item(args) -> bool:
    return save_one_paper(
        title=args.title,
        url=args.url,
        doi=args.doi,
        arxiv_id=args.arxiv_id,
        pdf_url=args.pdf_url,
        authors=args.authors,
        abstract=args.abstract,
        collection_name=args.collection,
        collection_key=None,
        tags=args.tags,
        allow_duplicates=args.allow_duplicates,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Save iDeer papers to local Zotero 9 through connector API."
    )

    # Batch mode from iDeer history.
    parser.add_argument(
        "--import_history",
        action="store_true",
        help="Import selected history JSON results to Zotero",
    )
    parser.add_argument(
        "--date",
        default="",
        help="History date, e.g. 2026-05-02. Default: latest date found in history",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=["arxiv", "semanticscholar"],
        help="Sources to import from history",
    )
    parser.add_argument(
        "--history_dir", default="history", help="iDeer history directory"
    )
    parser.add_argument(
        "--parent_collection",
        default="iDeer Daily Papers",
        help="Parent Zotero collection",
    )
    parser.add_argument(
        "--date_collection",
        default="",
        help="Date collection name. Default: YY.M.D from history date",
    )
    parser.add_argument(
        "--min_score", type=float, default=7, help="Minimum score to import"
    )
    parser.add_argument(
        "--max_per_source",
        type=int,
        default=15,
        help="Maximum items imported per source",
    )
    parser.add_argument(
        "--dry_run", action="store_true", help="Print selected items without saving"
    )
    parser.add_argument(
        "--allow_duplicates",
        action="store_true",
        help="Do not skip existing Zotero items",
    )

    # Single-item debug/compatibility mode.
    parser.add_argument("--url", default="", help="Paper URL")
    parser.add_argument("--title", default="", help="Paper title")
    parser.add_argument("--doi", default="", help="Paper DOI")
    parser.add_argument(
        "--arxiv_id", default="", help="Optional arXiv ID, e.g. 1706.03762"
    )
    parser.add_argument("--pdf_url", default="", help="Optional direct PDF URL")
    parser.add_argument("--authors", default="", help="Comma-separated authors")
    parser.add_argument("--abstract", default="", help="Abstract text")
    parser.add_argument("--collection", default="iDeer", help="Zotero collection name")
    parser.add_argument("--tags", default="iDeer", help="Comma-separated tags")

    args = parser.parse_args()

    try:
        if args.import_history:
            return import_history(args)

        if not args.url and not args.title and not args.doi:
            print(
                "[zotero_save] Need at least --url, --title, or --doi, "
                "or use --import_history.",
                file=sys.stderr,
            )
            return 2

        ok = save_item(args)
        return 0 if ok else 1
    except urllib.error.URLError as e:
        print(
            "[zotero_save] Cannot connect to Zotero connector API. "
            "Please open Zotero and enable local connector access. "
            f"Detail: {e}",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        print(f"[zotero_save] Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
