"""
ingestion/watcher.py
--------------------
Monitors the watched_folder for new files and triggers the ingestion
pipeline automatically when a new supported file is detected.

Folder structure (required):
  data/watched_folder/
    <State>/
      <Property Name>/
        file.pdf

Examples:
  data/watched_folder/Arizona/Magic Ranch 10/survey.pdf
  data/watched_folder/New Mexico/Mesa Del Sol/ESA.pdf
  data/watched_folder/California/Cabazon/alta.pdf

The state and property are read directly from the folder path — no fuzzy
filename matching. Both are validated against the Project Master to get the
correct category tag and catch folder name typos.

Folder routing after ingestion:
  Active properties  → processed/<state>/
  Sold properties    → processed/sold/<state>/
  Unknown/unmatched  → processed/unknown/
"""

import logging
import shutil
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import WATCH_DIR, PROCESSED_DIR, get_chunk_settings
from ingestion.extractor import extract, is_supported
from ingestion.chunker import chunk_text
from ingestion.embedder import store_chunks
from ingestion.registry import (
    get_file_hash,
    is_already_ingested,
    record_ingestion,
)

log = logging.getLogger("vaulter.watcher")

# ─── Property List Cache ──────────────────────────────────────────────────────
_PROPERTIES:      list[dict] | None = None
_SOLD_PROPERTIES: list[dict] | None = None

# Valid state folder names — derived dynamically from the property list on first use.
# Falls back to the known set if the Project Master file isn't present yet.
_VALID_STATES_CACHE: set | None = None

def _get_valid_states() -> set:
    """Return the set of valid state folder names from the live property list."""
    global _VALID_STATES_CACHE
    if _VALID_STATES_CACHE is not None:
        return _VALID_STATES_CACHE
    try:
        from pipeline.property_scraper import load_all_properties
        active, sold = load_all_properties()
        states = {p["state"].lower() for p in active + sold if p.get("state")}
        if states:
            _VALID_STATES_CACHE = states
            return _VALID_STATES_CACHE
    except Exception:
        pass
    # Fallback if Project Master not yet available
    _VALID_STATES_CACHE = {"arizona", "california", "new mexico", "colorado", "texas"}
    return _VALID_STATES_CACHE


def _load_properties() -> tuple[list[dict], list[dict]]:
    global _PROPERTIES, _SOLD_PROPERTIES
    if _PROPERTIES is not None:
        return _PROPERTIES, _SOLD_PROPERTIES
    try:
        from pipeline.property_scraper import load_all_properties
        _PROPERTIES, _SOLD_PROPERTIES = load_all_properties()
        log.info(f"Properties loaded: {len(_PROPERTIES)} active, {len(_SOLD_PROPERTIES)} sold")
    except FileNotFoundError:
        log.warning("No Project Master found — category will be tagged as unknown.")
        _PROPERTIES      = []
        _SOLD_PROPERTIES = []
    except Exception as e:
        log.warning(f"Could not load properties: {e}")
        _PROPERTIES      = []
        _SOLD_PROPERTIES = []
    return _PROPERTIES, _SOLD_PROPERTIES


# ─── Folder-Based Property Resolution ────────────────────────────────────────

def _resolve_from_path(path: Path) -> dict:
    """
    Read state and property directly from the folder structure:
      watched_folder / <State> / <Property Name> / file.pdf

    Validates both against the Project Master to get category.
    Returns a match dict compatible with the old _match_property() output.
    """
    parts = path.parts  # e.g. [..., 'watched_folder', 'Arizona', 'Magic Ranch 10', 'file.pdf']

    # Find watched_folder in the path
    try:
        wf_idx = next(i for i, p in enumerate(parts) if p == "watched_folder")
    except StopIteration:
        log.warning(f"  [WARN] File not inside watched_folder: {path}")
        return _unknown()

    remaining = parts[wf_idx + 1:]  # e.g. ('Arizona', 'Magic Ranch 10', 'file.pdf')

    if len(remaining) < 3:
        # File dropped directly in watched_folder or a state folder — no property folder
        if len(remaining) == 1:
            log.warning(f"  [WARN] Drop files into State/Property subfolders, not directly in watched_folder")
        elif len(remaining) == 2:
            log.warning(f"  [WARN] Drop files into a Property subfolder inside the State folder")
        return _unknown()

    folder_state    = remaining[0]   # e.g. "Arizona"
    folder_property = remaining[1]   # e.g. "Magic Ranch 10"

    # Validate state
    if folder_state.lower() not in _get_valid_states():
        log.warning(f"  [WARN] Unrecognised state folder '{folder_state}' — expected one of: {', '.join(sorted(_get_valid_states()))}")
        return _unknown()

    # Normalise state to lowercase_underscore for storage (matches existing convention)
    state_key = folder_state.lower().replace(" ", "_")

    # Look up property in project master to get category and status
    active_props, sold_props = _load_properties()

    # Case-insensitive match on property name
    def find_prop(prop_list, name):
        name_l = name.lower()
        return next((p for p in prop_list if p["name"].lower() == name_l), None)

    match = find_prop(active_props, folder_property)
    if match:
        return {
            "property": match["name"],
            "state":    state_key,
            "category": match.get("category", "unknown"),
            "status":   "active",
            "matched":  True,
        }

    match = find_prop(sold_props, folder_property)
    if match:
        return {
            "property": match["name"],
            "state":    state_key,
            "category": match.get("category", "unknown"),
            "status":   "sold",
            "matched":  True,
        }

    # Property folder name not in project master — still tag it, just warn
    log.warning(
        f"  [WARN] '{folder_property}' not found in Project Master — "
        f"tagging as-is. Check spelling matches the Project Master exactly."
    )
    return {
        "property": folder_property,
        "state":    state_key,
        "category": "unknown",
        "status":   "active",
        "matched":  True,  # we know state+property from folder, just not category
    }


def _unknown() -> dict:
    return {
        "property": "unknown",
        "state":    "unknown",
        "category": "unknown",
        "status":   "unknown",
        "matched":  False,
    }


# ─── Folder Structure Setup ───────────────────────────────────────────────────

def create_property_folders():
    """
    Create state/property subfolders in watched_folder based on the Project Master.
    Safe to run multiple times — skips folders that already exist.
    """
    active_props, _ = _load_properties()
    if not active_props:
        log.warning("No properties loaded — cannot create folders.")
        return

    created = 0
    for prop in active_props:
        state    = prop.get("state", "").strip()
        name     = prop.get("name", "").strip()
        if not state or not name:
            continue
        folder = WATCH_DIR / state / name
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created += 1

    log.info(f"Property folders ready ({created} new folders created in watched_folder/)")


# ─── Core Ingestion Function ──────────────────────────────────────────────────

def ingest_file(path: Path):
    """
    Full ingestion pipeline for a single file.
    Property identity is read from the folder path, not the filename.
    """
    log.info(f"[INGEST] {path.name}")

    if not is_supported(path):
        log.warning(f"  [SKIP] Unsupported file type: {path.suffix} — {path.name}")
        return

    doc_hash = get_file_hash(path)
    if is_already_ingested(doc_hash):
        log.info(f"  [SKIP] Already ingested: {path.name}")
        return

    try:
        # Step 1: Resolve property from folder path
        match = _resolve_from_path(path)

        if match["matched"]:
            log.info(f"  Property : {match['property']}")
            log.info(f"  State    : {match['state']}")
            log.info(f"  Category : {match['category']}")
            log.info(f"  Status   : {match['status']}")
        else:
            log.warning(f"  [WARN] Could not resolve property from path — tagged as unknown")

        # Step 2: Extract text
        log.info("  Extracting text...")
        text, metadata = extract(path)

        if not text.strip():
            log.warning(f"  [WARN] No text extracted from {path.name}")
            return

        method     = "OCR" if metadata.get("ocr_used") else "direct"
        page_count = metadata.get("page_count", 1)
        log.info(f"  Extracted {len(text):,} characters via {method} from {page_count} pages")

        # Step 3: Tag metadata with property info
        metadata["property"] = match["property"]
        metadata["state"]    = match["state"]
        metadata["category"] = match["category"]
        metadata["status"]   = match["status"]

        # Step 4: Chunk
        chunk_size, overlap = get_chunk_settings(page_count)
        log.info(f"  Chunking with size={chunk_size}, overlap={overlap} ({page_count} pages)")
        chunks = chunk_text(text, page_count=page_count)
        log.info(f"  Split into {len(chunks)} chunks")

        # Step 5: Store in ChromaDB
        store_chunks(chunks, metadata, doc_hash)

        # Step 6: Route to processed folder
        if match["status"] == "sold":
            dest_dir     = PROCESSED_DIR / "sold" / match["state"]
            folder_label = f"processed/sold/{match['state']}/"
        elif match["state"] != "unknown":
            dest_dir     = PROCESSED_DIR / match["state"] / match["property"]
            folder_label = f"processed/{match['state']}/{match['property']}/"
        else:
            dest_dir     = PROCESSED_DIR / "unknown"
            folder_label = "processed/unknown/"

        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        log.info(f"  Moved to {folder_label}")

        # Step 7: Record in registry
        record_ingestion(
            file_hash=doc_hash,
            filename=path.name,
            chunks=len(chunks),
            pages=page_count,
            ocr_used=metadata.get("ocr_used", False),
            property_name=match["property"],
            state=match["state"],
            category=match["category"],
        )

        log.info(f"  [DONE] {path.name} ({len(chunks)} chunks, {folder_label.strip('/')}, method={method})\n")

    except Exception as e:
        log.error(f"  [ERROR] Failed to ingest {path.name}: {e}", exc_info=True)


# ─── Watchdog Event Handler ───────────────────────────────────────────────────

class FileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Must be inside State/Property subfolder — skip files in state folder root
        if is_supported(path) and len(path.relative_to(WATCH_DIR).parts) >= 3:
            time.sleep(1)
            ingest_file(path)

    def on_moved(self, event):
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if is_supported(path) and len(path.relative_to(WATCH_DIR).parts) >= 3:
            time.sleep(1)
            ingest_file(path)


# ─── Startup Processing ───────────────────────────────────────────────────────

def process_existing_files():
    """Process any files already sitting in State/Property subfolders."""
    existing = [
        f for f in WATCH_DIR.rglob("*")
        if f.is_file()
        and is_supported(f)
        and len(f.relative_to(WATCH_DIR).parts) >= 3  # must be in State/Property/file
    ]
    if existing:
        log.info(f"Found {len(existing)} existing file(s) — ingesting now...")
        for file_path in existing:
            ingest_file(file_path)
    else:
        log.info("Watched folder is empty — drop files into State/Property subfolders to ingest")


# ─── Watcher Entry Point ──────────────────────────────────────────────────────

def _start_observer() -> Observer:
    """
    Internal: set up folders, ingest existing files, start the observer.
    Returns the running Observer so the caller can manage its lifecycle.
    """
    _load_properties()
    create_property_folders()
    process_existing_files()

    observer = Observer()
    observer.schedule(FileHandler(), str(WATCH_DIR), recursive=True)
    observer.start()
    log.info("[ACTIVE] Watcher running — drop files into State/Property subfolders")
    log.info("[SUPPORTED] .pdf  .xlsx  .xls  .csv  .txt")
    log.info("[STRUCTURE] watched_folder / <State> / <Property Name> / file.pdf")
    return observer


def start_watcher():
    """
    Blocking mode — used by 'python main.py ingest'.
    Runs until Ctrl+C.
    """
    observer = _start_observer()
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Shutting down watcher...")
        observer.stop()
    observer.join()


def start_watcher_background():
    """
    Non-blocking mode — used by the dashboard to run the watcher as a
    background thread alongside Flask. Returns immediately; the observer
    runs as a daemon and stops when the process exits.
    """
    observer = _start_observer()
    return observer
