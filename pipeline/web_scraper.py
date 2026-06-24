"""
pipeline/web_scraper.py
------------------------
Vaulter AI Stage 2 — Web Scraping Pipeline

Fetches market intelligence from public real estate research sites,
extracts clean text, deduplicates by content hash, and stores chunks
into the same ChromaDB collection used by Stage 1 PDFs.

Called by:  python main.py scrape
            python main.py scrape "CoStar News"
            pipeline/scheduler.py (on a timer)
"""

import csv
import hashlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    LOG_DIR, RAW_WEB_DIR, DATA_DIR,
    CHROMA_DIR, CHROMA_COLLECTION_NAME,
    WEB_SOURCES, LOG_LEVEL,
)
from pipeline.property_matcher import match_properties, matched_property_tags, format_matched_properties

# ─── Web Sources Folder ───────────────────────────────────────────
WEB_SOURCES_DIR = DATA_DIR / "web_sources"


def load_web_sources() -> tuple[list[dict], str]:
    """
    Load web sources from data/web_sources/ — any CSV file in that folder.
    Falls back to WEB_SOURCES in config.py if no file found.

    CSV format (with header row):
        name, url, frequency_hours, tags
        CBRE Market Reports, https://www.cbre.com/insights/reports, 24, "p,h2"

    Tags column is optional — comma-separated CSS selectors.
    """
    WEB_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    csvs = [f for f in WEB_SOURCES_DIR.iterdir()
            if f.is_file() and f.suffix.lower() == ".csv" and not f.name.startswith(".")]

    if not csvs:
        log.info("No CSV in data/web_sources/ — using sources from config.py")
        return WEB_SOURCES, "config.py"

    if len(csvs) > 1:
        log.warning(f"Multiple CSVs in web_sources/ — using: {csvs[0].name}")

    csv_path = csvs[0]
    sources  = []

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            reader.fieldnames = [h.strip() if h else h for h in reader.fieldnames]
            for row in reader:
                row  = {k: (v.strip() if v else "") for k, v in row.items()}
                name = row.get("name", "").strip()
                url  = row.get("url", "").strip()
                if not name or not url:
                    continue
                try:
                    freq = int(row.get("frequency_hours", "24").strip())
                except ValueError:
                    freq = 24
                # Parse tags — comma separated selectors
                tags_raw = row.get("tags", "").strip()
                tags     = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else ["p", "h2", "h3"]
                sources.append({
                    "name":            name,
                    "url":             url,
                    "frequency_hours": freq,
                    "tags":            tags,
                })

        if sources:
            log.info(f"Loaded {len(sources)} web sources from {csv_path.name}")
            return sources, csv_path.name
        else:
            log.warning(f"CSV empty — using sources from config.py")
            return WEB_SOURCES, "config.py (fallback)"

    except Exception as e:
        log.warning(f"Error reading {csv_path.name}: {e} — using sources from config.py")
        return WEB_SOURCES, "config.py (fallback)"

# ─── Logging ──────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [WEB] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "web_scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── Registry (tracks content hashes to skip unchanged pages) ─────
REGISTRY_FILE = DATA_DIR / "web_registry.json"

def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {}

def save_registry(registry: dict):
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


# ─── Helpers ──────────────────────────────────────────────────────

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def chunk_text(text: str) -> list[str]:
    from ingestion.chunker import chunk_text as _chunk_text
    return _chunk_text(text)

def simple_embed(text: str) -> list[float]:
    from ingestion.embedder import LocalHashEmbedding
    result = LocalHashEmbedding()([text])[0]
    return result.tolist() if hasattr(result, 'tolist') else list(result)


# ─── Fetch & Extract ──────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        log.info(f"Fetched {url} — {len(resp.content):,} bytes")
        return BeautifulSoup(resp.text, "html.parser")
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP error {url}: {e}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Connection failed {url} — site may be blocking bots")
    except requests.exceptions.Timeout:
        log.warning(f"Timeout {url}")
    except Exception as e:
        log.error(f"Unexpected error {url}: {e}")
    return None

def extract_text(soup: BeautifulSoup, css_selectors: list[str]) -> str:
    # Try CSS selectors first
    for selector in css_selectors:
        elements = soup.select(selector)
        if elements:
            raw = " ".join(el.get_text(separator=" ", strip=True) for el in elements)
            cleaned = " ".join(raw.split())
            if len(cleaned) > 200:
                return cleaned

    # Fallback 1: all paragraph and heading text
    tags = soup.find_all(["p", "h2", "h3", "h4"])
    text = " ".join(" ".join(t.get_text(separator=" ", strip=True).split()) for t in tags)
    if len(text) > 200:
        return text

    # Fallback 2: all body text (for JS-heavy sites that still have some static content)
    body = soup.find("body")
    if body:
        # Remove script, style, nav, footer tags
        for tag in body.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(body.get_text(separator=" ", strip=True).split())
        if len(text) > 200:
            return text

    return ""


# ─── ChromaDB ─────────────────────────────────────────────────────

def get_collection():
    from ingestion.embedder import get_collection as _get_collection
    return _get_collection()

def store_chunks(source_name: str, url: str, chunks: list[str], collection,
                 property_tags: dict | None = None):
    timestamp = datetime.now().isoformat()
    ids, docs, metas, embeds = [], [], [], []
    base_meta = {
        "source":     source_name,
        "url":        url,
        "type":       "web_scrape",
        "scraped_at": timestamp,
        **(property_tags or {}),
    }
    for i, chunk in enumerate(chunks):
        ids.append(f"web_{source_name.replace(' ', '_')}_{content_hash(chunk)}_{i}")
        docs.append(chunk)
        metas.append({**base_meta, "chunk": i})
        embeds.append(simple_embed(chunk))
    collection.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeds)
    log.info(f"Stored {len(chunks)} chunks from '{source_name}' into ChromaDB")


# ─── Main ─────────────────────────────────────────────────────────

def scrape_source(source: dict, collection, registry: dict) -> bool:
    name = source["name"]
    url  = source["url"]
    log.info(f"── Scraping: {name}")

    soup = fetch_page(url)
    if soup is None:
        return False

    text = extract_text(soup, source.get("tags", []))
    if len(text) < 100:
        log.warning(f"'{name}' — extracted text too short, skipping")
        return False

    h = content_hash(text)
    if registry.get(name, {}).get("last_hash") == h:
        log.info(f"'{name}' unchanged since last scrape — skipping")
        return False

    RAW_WEB_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (RAW_WEB_DIR / f"{name.replace(' ', '_')}_{ts}.txt").write_text(text, encoding="utf-8")

    # Match text to relevant properties from the Project Master
    matches      = match_properties(text)
    prop_tags    = matched_property_tags(matches)
    matched_desc = format_matched_properties(matches)
    log.info(f"  Matched properties: {matched_desc}")

    chunks = chunk_text(text)
    store_chunks(name, url, chunks, collection, property_tags=prop_tags)
    registry[name] = {"last_hash": h, "last_scraped": datetime.now().isoformat()}
    log.info(f"✓ '{name}' — {len(text):,} chars → {len(chunks)} chunks")
    return True


def scrape_all(target_name: str | None = None):
    collection          = get_collection()
    registry            = load_registry()
    all_sources, source = load_web_sources()
    log.info(f"Web sources: {source} ({len(all_sources)} sources)")

    sources = all_sources
    if target_name:
        sources = [s for s in all_sources if s["name"] == target_name]
        if not sources:
            log.error(
                f"No source named '{target_name}'\n"
                f"Available sources: {[s['name'] for s in all_sources]}"
            )
            return

    new = skipped = failed = 0
    for source in sources:
        try:
            if scrape_source(source, collection, registry):
                new += 1
            else:
                skipped += 1
            time.sleep(1.5)
        except Exception as e:
            log.error(f"Error scraping '{source['name']}': {e}")
            failed += 1

    save_registry(registry)
    log.info(f"Scrape complete — {new} new, {skipped} unchanged, {failed} failed")
