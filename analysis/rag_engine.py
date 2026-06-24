"""
analysis/rag_engine.py
-----------------------
Vaulter AI Stage 3 — RAG Retrieval Engine

Pulls relevant chunks from ChromaDB and assembles them into
a structured context block that Claude can reason over.

Supports four retrieval modes:
  - property_context : everything known about one specific property
  - cross_property   : compare or find patterns across multiple properties
  - type_filter      : pull by data type (email / web_scrape / pdf / property_intelligence)
  - free_search      : open-ended semantic search across all chunks

All results are ranked by relevance score and deduplicated before
being passed to the analyzer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.embedder import get_collection


# ─── Constants ────────────────────────────────────────────────────

MAX_CONTEXT_CHUNKS  = 20    # max chunks sent to Claude per query
MAX_CHARS_PER_CHUNK = 600   # truncate very long chunks to keep context tight
SOURCE_TYPE_LABELS  = {
    "web_scrape":             "Market Research",
    "property_intelligence":  "Property Intelligence",
    "email":                  "Broker Email",
    "pdf":                    "Due Diligence PDF",
}


# ─── Retrieval ────────────────────────────────────────────────────

def get_property_context(property_name: str, query: str = "", n: int = MAX_CONTEXT_CHUNKS) -> list[dict]:
    """
    Retrieve all available intelligence for a specific property.
    Pulls from PDFs, property scrapes, web matches, and emails.

    Args:
        property_name : exact property name from Project Master (e.g. "Magic Ranch 10")
        query         : optional focus question to re-rank results
        n             : max chunks to return
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    search_text = query if query else f"{property_name} market intelligence risk summary"

    # Primary: chunks tagged directly to this property (PDF ingestion + property scraper)
    direct = _query_with_filter(
        collection,
        search_text,
        where={"property": property_name},
        n=n // 2,
    )

    # Secondary: web/email chunks that mention this property via property_matcher
    matched = _query_matched_properties(collection, property_name, search_text, n=n // 2)

    return _merge_and_rank(direct, matched, n)


def get_cross_property_context(query: str, state: str = None, category: str = None, n: int = MAX_CONTEXT_CHUNKS) -> list[dict]:
    """
    Search across all properties — useful for portfolio-level questions,
    comparisons, and risk scans.

    Args:
        query    : the question or topic to search for
        state    : optional filter (e.g. "Arizona", "California")
        category : optional filter (e.g. "Final Engineering", "Disposition")
        n        : max chunks to return
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    where = _build_where(state=state, category=category)
    return _query_with_filter(collection, query, where=where, n=n)


def get_recent_emails(n: int = 15) -> list[dict]:
    """
    Pull the most recent broker email chunks.
    Used for the dashboard 'Recent Email Highlights' section.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    results = collection.get(
        where={"type": "email"},
        limit=min(n * 3, 500),
        include=["documents", "metadatas"],
    )

    chunks = _format_get_results(results)
    # Sort by scraped_at descending (most recent first)
    chunks.sort(key=lambda x: x.get("scraped_at", ""), reverse=True)
    return chunks[:n]


def get_recent_web_intelligence(n: int = 12) -> list[dict]:
    """
    Pull recent web scrape and property intelligence chunks.
    Used for the dashboard 'Latest Market News' section.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    results = collection.get(
        where={"type": {"$in": ["web_scrape", "property_intelligence"]}},
        limit=min(n * 4, 500),
        include=["documents", "metadatas"],
    )

    chunks = _format_get_results(results)
    chunks.sort(key=lambda x: x.get("scraped_at", ""), reverse=True)
    return chunks[:n]


def get_all_property_names() -> list[str]:
    """
    Return a deduplicated list of all property names that have chunks in ChromaDB.
    Used to populate the property selector in the dashboard.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    try:
        results = collection.get(
            limit=min(collection.count(), 9999),
            include=["metadatas"],
        )
        names = set()
        for meta in results["metadatas"]:
            prop = meta.get("property", "")
            if prop and prop not in ("unknown", ""):
                names.add(prop)
            # Also parse pipe-separated matched_properties tags
            matched = meta.get("matched_properties", "")
            if matched:
                for name in matched.split("|"):
                    name = name.strip()
                    if name and name != "unknown":
                        names.add(name)
        return sorted(names)
    except Exception:
        return []


def free_search(query: str, n: int = MAX_CONTEXT_CHUNKS) -> list[dict]:
    """
    Open-ended semantic search across everything in ChromaDB.
    Auto-detects property + document type mentions for targeted retrieval.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    # Detect if query mentions a specific property name
    # If so, combine direct property lookup with semantic search
    property_name = _detect_property_name(query)
    if property_name:
        direct  = _query_with_filter(collection, query, where={"property": property_name}, n=n)
        broader = _query_with_filter(collection, query, where=None, n=n // 2)
        return _merge_and_rank(direct, broader, n)

    return _query_with_filter(collection, query, where=None, n=n)


# Property names loaded lazily from the live Project Master on first use.
# Falls back to an empty list if no Project Master file is present.
_KNOWN_PROPERTIES: list[str] | None = None

def _get_known_properties() -> list[str]:
    """Load property names from the Project Master on first call, then cache."""
    global _KNOWN_PROPERTIES
    if _KNOWN_PROPERTIES is not None:
        return _KNOWN_PROPERTIES
    try:
        # Try the full project-master-aware version first
        from pipeline.property_scraper import load_properties
        props, _ = load_properties()
        _KNOWN_PROPERTIES = [p["name"] for p in props]
    except ImportError:
        try:
            # Fall back to the PROPERTIES list in older versions of property_scraper
            from pipeline.property_scraper import PROPERTIES
            _KNOWN_PROPERTIES = [p["name"] for p in PROPERTIES]
        except Exception:
            _KNOWN_PROPERTIES = []
    except Exception:
        _KNOWN_PROPERTIES = []
    return _KNOWN_PROPERTIES

def _detect_property_name(query: str) -> str | None:
    """Return a known property name if mentioned in the query."""
    q = query.lower()
    for prop in _get_known_properties():
        if prop.lower() in q:
            return prop
    return None


# ─── Internal Helpers ─────────────────────────────────────────────

def _query_with_filter(collection, query: str, where: dict | None, n: int) -> list[dict]:
    """Run a ChromaDB query with an optional metadata filter."""
    try:
        count = collection.count()
        if count == 0:
            return []

        params = {
            "query_texts": [query],
            "n_results":   min(n, count),
            "include":     ["documents", "metadatas", "distances"],
        }
        if where:
            params["where"] = where

        results = collection.query(**params)
        return _format_query_results(results)
    except Exception:
        return []


def _query_matched_properties(collection, property_name: str, query: str, n: int) -> list[dict]:
    """
    Find chunks where property_name appears in the pipe-separated
    matched_properties metadata field (set by property_matcher.py).
    ChromaDB doesn't support 'contains' on strings, so we fetch a broad
    set and filter in Python.
    """
    try:
        results = collection.get(
            where={"match_count": {"$gt": 0}},
            limit=min(500, collection.count()),
            include=["documents", "metadatas"],
        )
        chunks = _format_get_results(results)
        # Filter to chunks that mention this property
        filtered = [
            c for c in chunks
            if property_name.lower() in c.get("matched_properties", "").lower()
        ]
        return filtered[:n]
    except Exception:
        return []


def _build_where(state: str = None, category: str = None) -> dict | None:
    """Build a ChromaDB where clause from optional filters."""
    conditions = []
    if state:
        conditions.append({"state": {"$eq": state}})
    if category:
        conditions.append({"category": {"$eq": category}})
    if len(conditions) == 0:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _format_query_results(results: dict) -> list[dict]:
    """Convert ChromaDB query() results into clean dicts."""
    chunks = []
    if not results or not results.get("documents"):
        return chunks
    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i]
        dist = results["distances"][0][i] if results.get("distances") else 1.0
        score = round(1 - dist, 4)
        chunks.append(_build_chunk(doc, meta, score))
    return chunks


def _format_get_results(results: dict) -> list[dict]:
    """Convert ChromaDB get() results into clean dicts."""
    chunks = []
    if not results or not results.get("documents"):
        return chunks
    for i, doc in enumerate(results["documents"]):
        meta = results["metadatas"][i]
        chunks.append(_build_chunk(doc, meta, score=None))
    return chunks


def _build_chunk(doc: str, meta: dict, score) -> dict:
    """Build a standardised chunk dict from a raw ChromaDB result."""
    text = doc[:MAX_CHARS_PER_CHUNK] + "..." if len(doc) > MAX_CHARS_PER_CHUNK else doc
    source_type = meta.get("type", "unknown")
    return {
        "text":               text,
        "source_type":        source_type,
        "source_label":       SOURCE_TYPE_LABELS.get(source_type, source_type.replace("_", " ").title()),
        "source":             meta.get("source", meta.get("filename", "")),
        "property":           meta.get("property", ""),
        "matched_properties": meta.get("matched_properties", ""),
        "state":              meta.get("state", ""),
        "category":           meta.get("category", ""),
        "scraped_at":         meta.get("scraped_at", meta.get("ingested_at", "")),
        "score":              score,
    }


def _merge_and_rank(primary: list[dict], secondary: list[dict], n: int) -> list[dict]:
    """Merge two chunk lists, deduplicate by text, rank by score descending."""
    seen = set()
    merged = []
    for chunk in primary + secondary:
        key = chunk["text"][:100]
        if key not in seen:
            seen.add(key)
            merged.append(chunk)
    # Sort: scored chunks first (by score desc), unscored chunks after
    scored   = sorted([c for c in merged if c["score"] is not None], key=lambda x: -x["score"])
    unscored = [c for c in merged if c["score"] is None]
    return (scored + unscored)[:n]


def format_context_for_claude(chunks: list[dict]) -> str:
    """
    Format retrieved chunks into a structured context block
    ready to be injected into a Claude prompt.
    """
    if not chunks:
        return "No relevant data found in the database for this query."

    lines = []
    for i, chunk in enumerate(chunks, 1):
        label  = chunk["source_label"]
        source = chunk["source"]
        prop   = chunk["property"] or chunk.get("matched_properties", "").split("|")[0]
        state  = chunk["state"]
        date   = chunk["scraped_at"][:10] if chunk["scraped_at"] else "unknown date"

        header = f"[{i}] {label}"
        if prop and prop not in ("unknown", ""):
            header += f" — {prop}"
        if state and state not in ("unknown", ""):
            header += f" ({state})"
        if source:
            header += f" | Source: {source}"
        header += f" | Date: {date}"

        lines.append(header)
        lines.append(chunk["text"])
        lines.append("")

    return "\n".join(lines)
