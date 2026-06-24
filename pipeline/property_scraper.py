"""
pipeline/property_scraper.py
-----------------------------
Vaulter AI Stage 2 — Property Intelligence Scraper

Reads the Vaulter Project Master from data/project_master/.
Accepts any file type: CSV, Excel (.xlsx), PDF, or plain text.
Falls back to the built-in property list if no file is found
or the file cannot be parsed.

For each property it runs three searches:
  1. Google News — property name + location + category keywords
     (zoning decisions, sale announcements, developer activity)
  2. Google News — city + state + broader market conditions
     (land market trends, new home permits, local development)
  3. City-Data   — local market stats, population growth, housing trends

Every chunk stored in ChromaDB is tagged with property name, category,
and state so Stage 3 (Claude) can filter results per property.

To update the property list:
  1. Export Vaulter Project Master from Smartsheet in any format
  2. Drop the file into data/project_master/ — any filename is fine
  3. Re-run — the scraper picks it up automatically

Called by:
  python main.py property-scrape
  python main.py property-scrape "Magic Ranch 10"
  python main.py properties
  pipeline/scheduler.py (runs daily at 6am)
"""

import csv
import hashlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    LOG_DIR, RAW_WEB_DIR, DATA_DIR,
    CHROMA_DIR, CHROMA_COLLECTION_NAME,
    LOG_LEVEL,
)

# ΓöÇΓöÇΓöÇ Logging ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [PROPERTY] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "property_scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

PROJECT_MASTER_DIR = DATA_DIR / "project_master"
REGISTRY_FILE      = DATA_DIR / "property_scrape_registry.json"

# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Built-in Fallback Property List
# Extracted from Vaulter Project Master — exported June 15, 2026
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

BUILTIN_PROPERTIES = [
    # ΓöÇΓöÇ Arizona ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    {"name": "Rita Ranch",                 "city": "Tucson",        "state": "Arizona",    "category": "Acquisition"},
    {"name": "Eloy 310 (Interlink 8/10)",  "city": "Eloy",          "state": "Arizona",    "category": "Pre-Plat"},
    {"name": "Kirby Hughes & Luckett",     "city": "Phoenix",       "state": "Arizona",    "category": "Pre-Plat"},
    {"name": "Picacho Crossing Ph II",     "city": "Picacho",       "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Magic Ranch 10",             "city": "Florence",      "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Magic Ranch 80",             "city": "Florence",      "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Mesquite Trails",            "city": "Maricopa",      "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Mesquite Trails Ph 2, 3, 4", "city": "Maricopa",      "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Heritage",                   "city": "Arizona",       "state": "Arizona",    "category": "Final Engineering"},
    {"name": "Heartland 53",               "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "Lucky Hunt",                 "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "Magic Ranch 50",             "city": "Florence",      "state": "Arizona",    "category": "Disposition"},
    {"name": "Marabella",                  "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "Rodeo Ranch",                "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "Mountain View Ranch",        "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "El Mirage & Lower Buckeye",  "city": "El Mirage",     "state": "Arizona",    "category": "Disposition"},
    {"name": "Hidden Canyon",              "city": "Arizona",       "state": "Arizona",    "category": "Disposition"},
    {"name": "Airport & Ocotillo",         "city": "Chandler",      "state": "Arizona",    "category": "Site Maintenance"},
    {"name": "Heartland 125",              "city": "Arizona",       "state": "Arizona",    "category": "Site Maintenance"},
    {"name": "Heartland 255",              "city": "Arizona",       "state": "Arizona",    "category": "Site Maintenance"},
    {"name": "Heartland 81",               "city": "Arizona",       "state": "Arizona",    "category": "Site Maintenance"},
    {"name": "Walker Butte 1200",          "city": "Coolidge",      "state": "Arizona",    "category": "Site Maintenance"},
    # ΓöÇΓöÇ California ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    {"name": "Cabazon",                    "city": "Cabazon",       "state": "California", "category": "Acquisition"},
    {"name": "Banning",                    "city": "Banning",       "state": "California", "category": "Rezone"},
    {"name": "Affresco East",              "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Apple Valley & Ohna",        "city": "Apple Valley",  "state": "California", "category": "Pre-Plat"},
    {"name": "Auburn & Verbena",           "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Fuchsia & Dos Palmas",       "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Hook & Cobalt/S&C",          "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Hopland & Cordova",          "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Kemper Campbell",            "city": "Victorville",   "state": "California", "category": "Pre-Plat"},
    {"name": "South 20E",                  "city": "California",    "state": "California", "category": "Pre-Plat"},
    {"name": "Affresco West",              "city": "California",    "state": "California", "category": "Final Engineering"},
    {"name": "Antelope & Ellis",           "city": "Lancaster",     "state": "California", "category": "Final Engineering"},
    {"name": "Griffin Ranch",              "city": "California",    "state": "California", "category": "Final Engineering"},
    {"name": "Wilson & Florida",           "city": "California",    "state": "California", "category": "Final Engineering"},
    {"name": "Rosamond & 40th St.",        "city": "Rosamond",      "state": "California", "category": "Disposition"},
    {"name": "Calhoun 29",                 "city": "California",    "state": "California", "category": "Disposition"},
    {"name": "Calhoun 30 (Triangle)",      "city": "California",    "state": "California", "category": "Site Maintenance"},
    {"name": "Bell Mountain 49",           "city": "California",    "state": "California", "category": "Site Maintenance"},
    {"name": "Panther & Crippin",          "city": "California",    "state": "California", "category": "Site Maintenance"},
    {"name": "Silverlakes",                "city": "Helendale",     "state": "California", "category": "Site Maintenance"},
    # ΓöÇΓöÇ New Mexico ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    {"name": "Mesa Del Sol",               "city": "Albuquerque",   "state": "New Mexico", "category": "Acquisition"},
    {"name": "Los Senderos",               "city": "New Mexico",    "state": "New Mexico", "category": "Acquisition"},
    # ΓöÇΓöÇ Colorado ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    {"name": "Mead (WCR 34 & Hwy 25)",     "city": "Mead",          "state": "Colorado",   "category": "Pre-Plat"},
    # ΓöÇΓöÇ Texas ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    {"name": "Long Branch (Wilson 155)",   "city": "Texas",         "state": "Texas",      "category": "Rezone"},
    {"name": "Pacific & Pinson - Forney",  "city": "Forney",        "state": "Texas",      "category": "Pre-Plat"},
    {"name": "Horseshoe Bay Lots",         "city": "Horseshoe Bay", "state": "Texas",      "category": "Site Maintenance"},
    {"name": "Triad",                      "city": "Texas",         "state": "Texas",      "category": "Development"},
]

CITY_OVERRIDES = {p["name"]: p["city"] for p in BUILTIN_PROPERTIES}

# Category-specific keywords for the property-level news query
CATEGORY_KEYWORDS = {
    "Acquisition":       "land acquisition purchase price market",
    "Pre-Plat":          "zoning entitlement land development subdivision",
    "Final Engineering": "residential development subdivision engineering permits",
    "Disposition":       "land sale listing price market comparable",
    "Site Maintenance":  "land holding market conditions value",
    "Rezone":            "rezoning land use entitlement approval",
    "Development":       "residential development new homes construction",
}

# Broader market keywords for the city-level news query
MARKET_KEYWORDS = "land market new homes permits subdivision development real estate"

SEARCH_YEAR = 2026

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# File Detection & Parsing
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def find_project_file() -> Path | None:
    PROJECT_MASTER_DIR.mkdir(parents=True, exist_ok=True)
    files = [f for f in PROJECT_MASTER_DIR.iterdir()
             if f.is_file() and not f.name.startswith(".")]
    if not files:
        return None
    if len(files) > 1:
        log.warning(f"Multiple files found in project_master/ — using: {files[0].name}")
    return files[0]


def parse_csv(path: Path) -> list[dict]:
    properties = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() if h else h for h in reader.fieldnames]
        for row in reader:
            row      = {k: (v.strip() if v else "") for k, v in row.items()}
            name     = row.get("Project Name", "")
            category = row.get("Project Category", "")
            state    = row.get("State", "")
            if not name or not category or name.lower() == "template":
                continue
            properties.append({
                "name":     name,
                "city":     CITY_OVERRIDES.get(name, state),
                "state":    state,
                "category": category,
            })
    return properties


def parse_excel(path: Path) -> list[dict]:
    import openpyxl
    wb      = openpyxl.load_workbook(path, data_only=True)
    ws      = wb.active
    rows    = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(c).strip() if c else "" for c in rows[0]]
    col     = {h: i for i, h in enumerate(headers)}
    name_i  = col.get("Project Name",     0)
    cat_i   = col.get("Project Category", 2)
    state_i = col.get("State",            3)
    properties = []
    for row in rows[1:]:
        name     = str(row[name_i]).strip()  if row[name_i]  else ""
        category = str(row[cat_i]).strip()   if row[cat_i]   else ""
        state    = str(row[state_i]).strip() if row[state_i] else ""
        if not name or not category or name.lower() == "template":
            continue
        properties.append({
            "name":     name,
            "city":     CITY_OVERRIDES.get(name, state),
            "state":    state,
            "category": category,
        })
    return properties


def _clean_ocr_name(raw: str) -> str:
    """Strip OCR artifacts from a property name."""
    import re
    name = re.sub(r'^[\d\s]*', '', raw).strip()            # leading digits/spaces
    name = re.sub(r'^[a-zA-Z]{1,2}\s+', '', name).strip()   # leading 1-2 char noise
    name = re.sub(r'\s+1[.,]\s+.*$', '', name).strip()      # trailing "1. SomeName"
    name = re.sub(r'\s+1[.,]$', '', name).strip()            # trailing "1." or "1,"
    name = re.sub(r'^\.\s*', '', name).strip()              # leading dot
    name = name.replace("Mesauite", "Mesquite")               # common OCR typo
    return name.strip()


# OCR alias map — maps OCR-mangled names to their correct known names
OCR_ALIASES = {
    "pacific & pinson forney":  "Pacific & Pinson - Forney",
    "mesauite trails":          "Mesquite Trails",
    "magie ranch 50":           "Magic Ranch 50",
    "magic ranch 50":           "Magic Ranch 50",
}

def _match_known_name(cleaned: str) -> str:
    """Fuzzy-match a cleaned OCR name to a known property name."""
    cl = cleaned.lower()

    # Check exact alias map first
    if cl in OCR_ALIASES:
        return OCR_ALIASES[cl]

    # Then fuzzy match against known names
    for known in CITY_OVERRIDES:
        if known.lower() in cl or cl in known.lower():
            return known
    return cleaned


def _parse_pdf_text(path: Path) -> tuple[list[dict], set]:
    """
    Standard pdfplumber text/table extraction for text-based PDFs.
    Detects struck-through rows by checking for horizontal line objects
    that overlap with the row's vertical position.
    Returns (properties, skipped_names).
    """
    import pdfplumber
    properties, seen = [], set()
    skipped          = set()

    CATEGORIES   = {"Acquisition", "Pre-Plat", "Final Engineering",
                    "Disposition", "Site Maintenance", "Rezone", "Development"}
    STATES       = {"Arizona", "California", "New Mexico", "Colorado", "Texas"}
    # Only process pages that have Project Name / State columns
    VALID_HEADERS = {"project name", "project category", "state"}

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue

            # Skip sponsor/submitter pages by checking the header row
            header_row = [str(c).strip().lower() if c else "" for c in table[0]]
            if not any(h in VALID_HEADERS for h in header_row):
                continue

            # Get horizontal strikethrough lines on this page
            # Lines with near-zero height and meaningful width
            h_lines     = [l for l in page.lines
                          if abs(l.get("height", 1)) < 1 and l.get("width", 0) > 20]
            struck_tops = {round(l["top"]) for l in h_lines}

            # Get word-level positions to find each row's vertical position
            words = page.extract_words()
            word_rows = {}
            for w in words:
                bucket = round(w["top"] / 5) * 5
                word_rows.setdefault(bucket, []).append(w)

            # Build a map: name ΓåÆ is_struck
            struck_names = set()
            for bucket, row_words in word_rows.items():
                name_words = [w for w in row_words if w["x0"] < page.width * 0.35]
                if not name_words:
                    continue
                row_top = name_words[0]["top"]
                if any(abs(st - row_top) < 6 for st in struck_tops):
                    name_text = " ".join(w["text"] for w in sorted(name_words, key=lambda x: x["x0"]))
                    # Extract just the property name (strip leading row numbers)
                    import re
                    name_text = re.sub(r"^\d+\s*", "", name_text).strip()
                    struck_names.add(name_text)

            # Parse the table
            for row in table[1:]:
                if not row or len(row) < 3:
                    continue
                name     = str(row[0]).strip() if row[0] else ""
                category = str(row[2]).strip() if row[2] else ""
                state    = str(row[3]).strip() if len(row) > 3 and row[3] else ""

                if (not name or not category
                        or name.lower() in ("project name", "template", "")
                        or category not in CATEGORIES
                        or name in seen):
                    continue

                # Check if this name was struck through
                if name in struck_names or any(name in sn or sn in name for sn in struck_names):
                    log.info(f"  ✓ Skipped (struck-through): {name}")
                    skipped.add(name)
                    seen.add(name)
                    continue

                seen.add(name)
                properties.append({
                    "name":     name,
                    "city":     CITY_OVERRIDES.get(name, state),
                    "state":    state,
                    "category": category,
                })

    if skipped:
        log.info(f"Skipped {len(skipped)} struck-through (inactive) properties")

    return properties, skipped


def _max_dark_run(row_pixels, threshold: int = 100) -> int:
    """Find the longest continuous run of dark pixels in a row."""
    dark    = row_pixels < threshold
    max_run = run = 0
    for d in dark:
        if d:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run


def _word_has_strikethrough(word_data: dict, img_array) -> bool:
    """
    Detect if a word has a strikethrough line by looking for a long continuous
    dark horizontal run in the middle 50% of the word height.
    A strikethrough covers >60% of the word width as a solid dark band.
    """
    import numpy as np
    x, y, w, h = word_data["left"], word_data["top"], word_data["width"], word_data["height"]
    if w < 10 or h < 8:
        return False
    # Only scan middle 50% of height to avoid top/bottom table borders
    y_start = y + h // 4
    y_end   = y + 3 * h // 4
    runs    = []
    for row_y in range(y_start, y_end):
        if row_y >= img_array.shape[0]:
            continue
        row_px = img_array[row_y, x:x + w]
        runs.append(_max_dark_run(row_px))
    return max(runs, default=0) > w * 0.60


def _row_is_struck_through(words_in_row: list, img_array) -> bool:
    """
    Returns True if the majority of words in a row have strikethrough lines.
    """
    if not words_in_row:
        return False
    struck = sum(1 for w in words_in_row if _word_has_strikethrough(w, img_array))
    return struck >= max(1, len(words_in_row) * 0.5)


def _parse_pdf_ocr(path: Path) -> list[dict]:
    """
    OCR-based extraction for image-based PDFs (e.g. Smartsheet exports).
    Uses pdf2image + pytesseract with column-position detection.
    Automatically skips rows with strikethrough text (inactive properties).
    """
    import re
    import numpy as np
    from pdf2image import convert_from_path
    import pytesseract
    from config import POPPLER_PATH, TESSERACT_PATH

    if TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)

    CATEGORIES = {"Acquisition", "Pre-Plat", "Final Engineering",
                  "Disposition", "Site Maintenance", "Rezone", "Development"}
    STATES     = {"Arizona", "California", "New Mexico", "Colorado", "Texas"}
    SKIP_NAMES = {"template", "project name", "dashboard link", "project category",
                  "state", "smartsheet", "vaulter project master", ""}

    convert_kwargs = {"dpi": 200}
    if POPPLER_PATH:
        convert_kwargs["poppler_path"] = str(POPPLER_PATH)

    pages      = convert_from_path(str(path), **convert_kwargs)
    properties = []
    seen       = set()
    skipped    = []

    for page_img in pages:
        img_array = np.array(page_img.convert("L"))
        data      = pytesseract.image_to_data(page_img, output_type=pytesseract.Output.DICT)
        page_w    = page_img.width

        # Group words into rows by vertical position (15px buckets)
        lines_by_top = {}
        for i, word in enumerate(data["text"]):
            word = word.strip()
            if not word or data["conf"][i] < 30:
                continue
            top  = (data["top"][i] // 15) * 15
            left = data["left"][i]
            lines_by_top.setdefault(top, []).append({
                "word": word, "left": left, "top": data["top"][i],
                "width": data["width"][i], "height": data["height"][i]
            })

        for top in sorted(lines_by_top.keys()):
            word_dicts = sorted(lines_by_top[top], key=lambda x: x["left"])

            # Check for strikethrough in name column words
            name_word_dicts = [w for w in word_dicts if w["left"] < page_w * 0.35]
            if _row_is_struck_through(name_word_dicts, img_array):
                struck_name = " ".join(w["word"] for w in name_word_dicts)[:50]
                skipped.append(struck_name)
                continue

            # Extract column text
            name_words  = " ".join(w["word"] for w in word_dicts if w["left"] < page_w * 0.35)
            cat_words   = " ".join(w["word"] for w in word_dicts if page_w * 0.35 <= w["left"] < page_w * 0.65)
            state_words = " ".join(w["word"] for w in word_dicts if w["left"] >= page_w * 0.65)

            # Clean and validate name
            name = _clean_ocr_name(name_words)
            name = re.sub(r"[^\w\s&/(),.-]", "", name).strip()

            if (not name or len(name) < 3 or name.lower() in SKIP_NAMES
                    or "Dashboard" in name or "Exported" in name
                    or name.isdigit()):
                continue

            name = _match_known_name(name)
            if name in seen:
                continue

            category = next((c for c in CATEGORIES if c in cat_words), "")
            state    = next((s for s in STATES if s in state_words), "")

            if not category:
                continue

            seen.add(name)
            properties.append({
                "name":     name,
                "city":     CITY_OVERRIDES.get(name, state),
                "state":    state,
                "category": category,
            })

    if skipped:
        log.info(f"Skipped {len(skipped)} struck-through (inactive) rows:")
        for s in skipped:
            log.info(f"  ✓ {s}")

    # Build set of matched known names that were skipped (for gap-fill filtering)
    skipped_known = set()
    for s in skipped:
        matched = _match_known_name(_clean_ocr_name(s))
        if matched in CITY_OVERRIDES:
            skipped_known.add(matched)

    return properties, skipped_known


def parse_pdf(path: Path) -> tuple[list[dict], set]:
    """
    Parse a PDF Project Master — handles both text-based and image-based PDFs.
    Returns (properties, skipped_names) where skipped_names are struck-through rows.
    """
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        has_text = any(len(page.chars) > 0 for page in pdf.pages)

    if has_text:
        log.info("PDF has text layer — using standard extraction")
        return _parse_pdf_text(path)
    else:
        log.info("PDF is image-based — using OCR extraction")
        return _parse_pdf_ocr(path)


def parse_text(path: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8", errors="replace")
    return [p for p in BUILTIN_PROPERTIES if p["name"] in content]


def filter_properties(file_props: list[dict], source_name: str, skipped_names: set = None) -> tuple[list[dict], str]:
    """
    Filter file-parsed properties:
    - Remove struck-through (inactive) properties
    - Log any skipped properties
    Returns (active_properties, source_description).
    """
    inactive = skipped_names or set()
    active   = [p for p in file_props if p["name"] not in inactive]

    if inactive:
        log.info(f"{len(inactive)} struck-through properties excluded: {', '.join(sorted(inactive))}")

    log.info(f"Loaded {len(active)} active properties from {source_name}")
    return active, source_name


def load_properties() -> tuple[list[dict], str]:
    """
    Load properties from whatever file is in data/project_master/.
    Accepts PDF, CSV, Excel, or text files.
    Struck-through properties are automatically excluded.
    Raises FileNotFoundError if no file is found.
    """
    file = find_project_file()
    if not file:
        raise FileNotFoundError(
            f"\nNo Project Master file found in {PROJECT_MASTER_DIR}\n"
            "Export the Vaulter Project Master from Smartsheet and drop it into:\n"
            f"  {PROJECT_MASTER_DIR}\n"
            "Supported formats: PDF, CSV, Excel (.xlsx)\n"
        )

    ext = file.suffix.lower()
    log.info(f"Loading properties from: {file.name}")
    try:
        skipped_names = set()
        if ext == ".csv":
            props = parse_csv(file)
        elif ext in (".xlsx", ".xlsm", ".xls"):
            props = parse_excel(file)
        elif ext == ".pdf":
            props, skipped_names = parse_pdf(file)
        else:
            props = parse_text(file)

        if props:
            return filter_properties(props, file.name, skipped_names)

        raise ValueError(f"Could not extract any properties from {file.name}")

    except (FileNotFoundError, ValueError):
        raise
    except Exception as e:
        raise ValueError(f"Failed to parse {file.name}: {e}") from e


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Helpers
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ



def load_all_properties() -> tuple[list[dict], list[dict]]:
    """
    Load ALL properties from data/project_master/ — both active and sold.

    Reads directly from whatever file is in data/project_master/.
    No hardcoding — everything comes from the Smartsheet export.

    Returns:
        (active_properties, sold_properties)

        active = properties NOT struck-through in the Smartsheet
        sold   = properties WITH strikethrough (inactive/closed deals)

    Each property dict has: name, city, state, category
    Sold properties also have: status = "sold"

    Raises FileNotFoundError if no file found in data/project_master/.
    """
    file = find_project_file()
    if not file:
        raise FileNotFoundError(
            f"\nNo Project Master file found in {PROJECT_MASTER_DIR}\n"
            "Export the Vaulter Project Master from Smartsheet and drop it into:\n"
            f"  {PROJECT_MASTER_DIR}\n"
            "Supported formats: PDF, CSV, Excel (.xlsx)\n"
        )

    ext           = file.suffix.lower()
    skipped_names = set()

    log.info(f"Loading all properties (active + sold) from: {file.name}")

    try:
        if ext == ".pdf":
            all_props, skipped_names = parse_pdf(file)
        elif ext == ".csv":
            all_props = parse_csv(file)
        elif ext in (".xlsx", ".xlsm", ".xls"):
            all_props = parse_excel(file)
        else:
            all_props = parse_text(file)

    except Exception as e:
        raise ValueError(f"Failed to parse {file.name}: {e}") from e

    # Active = everything parse_pdf returned (struck-through already excluded)
    active = all_props

    # Sold = struck-through names looked up in BUILTIN_PROPERTIES for full details
    # parse_pdf removes struck-through rows from all_props but tracks their names
    # in skipped_names, so we rebuild full dicts from BUILTIN_PROPERTIES
    builtin_map = {p["name"]: p for p in BUILTIN_PROPERTIES}
    sold = []
    for name in skipped_names:
        prop = builtin_map.get(name)
        if prop:
            sold.append({**prop, "status": "sold"})
        else:
            # Name not in builtin list — create minimal entry using CITY_OVERRIDES
            sold.append({
                "name":     name,
                "city":     CITY_OVERRIDES.get(name, "unknown"),
                "state":    "unknown",
                "category": "unknown",
                "status":   "sold",
            })

    log.info(
        f"  {len(active)} active properties, "
        f"{len(sold)} sold/struck-through properties"
    )
    return active, sold


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return {}

def save_registry(r: dict):
    REGISTRY_FILE.write_text(json.dumps(r, indent=2))

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def chunk_text(text: str) -> list[str]:
    from ingestion.chunker import chunk_text as _chunk_text
    return _chunk_text(text)

def simple_embed(text: str) -> list[float]:
    from ingestion.embedder import LocalHashEmbedding
    result = LocalHashEmbedding()([text])[0]
    return result.tolist() if hasattr(result, "tolist") else list(result)

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.exceptions.HTTPError:
        log.warning(f"HTTP {resp.status_code} — {url}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Connection failed — {url}")
    except requests.exceptions.Timeout:
        log.warning(f"Timeout — {url}")
    except Exception as e:
        log.error(f"Fetch error {url}: {e}")
    return None

def extract_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for sel in selectors:
        els = soup.select(sel)
        if els:
            raw     = " ".join(el.get_text(separator=" ", strip=True) for el in els)
            cleaned = " ".join(raw.split())
            if len(cleaned) > 100:
                return cleaned
    tags = soup.find_all(["p", "h2", "h3", "h4", "li"])
    return " ".join(" ".join(t.get_text(separator=" ", strip=True).split()) for t in tags)


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# ChromaDB
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def get_collection():
    from ingestion.embedder import get_collection as _get_collection
    return _get_collection()

def store_chunks(label, url, chunks, name, category, state, source_type, collection):
    ts = datetime.now().isoformat()
    ids, docs, metas, embeds = [], [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"prop_{name.replace(' ','_')}_{source_type}_{content_hash(chunk)}_{i}")
        docs.append(chunk)
        metas.append({
            "source":      label,
            "url":         url,
            "property":    name,
            "category":    category,
            "state":       state,
            "source_type": source_type,
            "chunk":       i,
            "type":        "property_intelligence",
            "scraped_at":  ts,
        })
        embeds.append(simple_embed(chunk))
    collection.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeds)


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Search URL Builders
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def build_property_news_url(prop: dict) -> str:
    """Targeted news search: property name + location + category keywords."""
    kw = CATEGORY_KEYWORDS.get(prop["category"], "land development")
    q  = f'{prop["name"]} {prop["city"]} {prop["state"]} {kw}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_market_news_url(prop: dict) -> str:
    """Broader market news: city + state + land market conditions."""
    q = f'{prop["city"]} {prop["state"]} {MARKET_KEYWORDS}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_city_data_url(prop: dict) -> str:
    """City-Data page for local stats, demographics, and housing trends."""
    city  = prop["city"].replace(" ", "-")
    state = prop["state"].replace(" ", "-")
    if city == state:
        return f"https://www.city-data.com/state/{state}.html"
    return f"https://www.city-data.com/city/{city}-{state}.html"

def build_zoning_news_url(prop: dict) -> str:
    """Zoning and planning commission news — entitlement decisions near the property."""
    q = f'{prop["city"]} {prop["state"]} planning commission zoning rezoning entitlement {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_competitor_news_url(prop: dict) -> str:
    """Homebuilder and competitor activity in the same market."""
    q = f'{prop["city"]} {prop["state"]} homebuilder subdivision land purchase DR Horton Lennar Taylor Morrison {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_economic_news_url(prop: dict) -> str:
    """Economic development and job growth — drives residential land demand."""
    q = f'{prop["city"]} {prop["state"]} jobs employer economic development population growth {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_permit_news_url(prop: dict) -> str:
    """Building permit and construction activity near the property."""
    q = f'{prop["city"]} {prop["state"]} building permits construction new homes residential {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_infrastructure_news_url(prop: dict) -> str:
    """Roads, utilities, and infrastructure projects near the property."""
    q = f'{prop["city"]} {prop["state"]} infrastructure roads utilities water sewer development {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_school_news_url(prop: dict) -> str:
    """School district news and ratings — affects residential demand."""
    q = f'{prop["city"]} {prop["state"]} school district rating growth new school {SEARCH_YEAR}'
    return f"https://news.google.com/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def build_niche_url(prop: dict) -> str:
    """Niche.com city overview — school ratings, livability, cost of living."""
    city  = prop["city"].lower().replace(" ", "-")
    state = prop["state"].lower().replace(" ", "-")
    if city == state:
        return f"https://www.niche.com/places-to-live/search/best-places-to-live/s/{state}/"
    return f"https://www.niche.com/places-to-live/{city}-{state}/"


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Core Fetch + Store
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def _try_source(
    key: str,
    label: str,
    url: str,
    selectors: list[str],
    prop: dict,
    collection,
    registry: dict,
    source_type: str,
) -> bool:
    """Fetch one URL, deduplicate, chunk, and store. Returns True if new data stored."""
    soup = fetch_page(url)
    if not soup:
        return False

    text = extract_text(soup, selectors)
    if len(text) < 150:
        log.info(f"  {label}: content too short — skipping")
        return False

    h = content_hash(text)
    if registry.get(key, {}).get("last_hash") == h:
        log.info(f"  {label}: unchanged — skipping")
        return False

    chunks = chunk_text(text)
    store_chunks(label, url, chunks,
                 prop["name"], prop["category"], prop["state"],
                 source_type, collection)
    registry[key] = {"last_hash": h, "last_scraped": datetime.now().isoformat()}
    log.info(f"  Γ£ô {label}: {len(chunks)} chunks stored")
    return True


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Scrape One Property — 10 sources
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def scrape_property(prop: dict, collection, registry: dict) -> dict:
    name = prop["name"]
    log.info(f"ΓöÇΓöÇ {name} ({prop['category']}, {prop['state']})")

    results = {
        "property_news":    False,
        "market_news":      False,
        "city_data":        False,
        "zoning_news":      False,
        "competitor_news":  False,
        "economic_news":    False,
        "permit_news":      False,
        "infrastructure":   False,
        "school_news":      False,
        "niche":            False,
    }

    NEWS_SELECTORS = ["article", "h3", "h4", ".JtKRv", "a[href*='article']"]

    sources = [
        # key                  label                           url builder                     selectors           source_type
        ("property_news",   f"{name} — Property News",    build_property_news_url(prop),    NEWS_SELECTORS,     "property_news"),
        ("market_news",     f"{name} — Market News",      build_market_news_url(prop),      NEWS_SELECTORS,     "market_news"),
        ("city_data",       f"{name} — City-Data",        build_city_data_url(prop),        ["#city-stats", ".city-section", "p", "h2", "h3", "li"], "local_stats"),
        ("zoning_news",     f"{name} — Zoning News",      build_zoning_news_url(prop),      NEWS_SELECTORS,     "zoning_news"),
        ("competitor_news", f"{name} — Competitor News",  build_competitor_news_url(prop),  NEWS_SELECTORS,     "competitor_news"),
        ("economic_news",   f"{name} — Economic News",    build_economic_news_url(prop),    NEWS_SELECTORS,     "economic_news"),
        ("permit_news",     f"{name} — Permit Activity",  build_permit_news_url(prop),      NEWS_SELECTORS,     "permit_news"),
        ("infrastructure",  f"{name} — Infrastructure",   build_infrastructure_news_url(prop), NEWS_SELECTORS,  "infrastructure"),
        ("school_news",     f"{name} — School News",      build_school_news_url(prop),      NEWS_SELECTORS,     "school_news"),
        ("niche",           f"{name} — Niche",            build_niche_url(prop),            [".niche__header", ".scalar", "p", "h2", "li"], "livability"),
    ]

    for key, label, url, selectors, source_type in sources:
        results[key] = _try_source(
            key=f"{name}_{key}",
            label=label,
            url=url,
            selectors=selectors,
            prop=prop,
            collection=collection,
            registry=registry,
            source_type=source_type,
        )
        time.sleep(1.0)

    return results


# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
# Public API
# ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

def scrape_all_properties(target_name: str | None = None):
    """Scrape all properties (or one by name) including sold ones."""
    RAW_WEB_DIR.mkdir(parents=True, exist_ok=True)

    try:
        active, sold = load_all_properties()
        properties   = active + sold
        source       = "Project Master (active + sold)"
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return

    log.info(f"Property source: {source}")

    if target_name:
        properties = [p for p in properties if p["name"].lower() == target_name.lower()]
        if not properties:
            log.error(
                f"Property '{target_name}' not found.\n"
                "Run 'python main.py properties' to see all available names."
            )
            return

    collection = get_collection()
    registry   = load_registry()
    totals     = {"news": 0, "city_data": 0, "skipped": 0, "failed": 0}

    for prop in properties:
        try:
            result  = scrape_property(prop, collection, registry)
            any_new = any(result.values())
            if result.get("property_news") or result.get("market_news"):
                totals["news"] += 1
            if result.get("city_data") or result.get("niche"):
                totals["city_data"] += 1
            if not any_new:
                totals["skipped"] += 1
        except Exception as e:
            log.error(f"Error on '{prop['name']}': {e}")
            totals["failed"] += 1

    save_registry(registry)
    log.info(
        f"Property scrape complete — "
        f"{totals['news']} news updated, {totals['city_data']} city-data updated, "
        f"{totals['skipped']} unchanged, {totals['failed']} failed"
    )


def list_properties():
    """Print all properties grouped by category, including sold ones."""
    try:
        active, sold = load_all_properties()
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        return

    total = len(active) + len(sold)
    print(f"\n{'ΓöÇ' * 60}")
    print(f"  Vaulter AI Project Master — {total} properties total")
    print(f"  Active: {len(active)}  |  Sold: {len(sold)}")
    print(f"{'ΓöÇ' * 60}")

    # Active properties grouped by category
    by_category = {}
    for p in active:
        by_category.setdefault(p["category"], []).append(p)

    for cat, props in sorted(by_category.items()):
        print(f"\n  {cat} ({len(props)})")
        for p in props:
            print(f"    ┬╖ {p['name']} — {p['city']}, {p['state']}")

    # Sold properties at the bottom
    if sold:
        print(f"\n  Sold / Inactive ({len(sold)})")
        for p in sold:
            print(f"    ┬╖ {p['name']} — {p.get('city', '?')}, {p.get('state', '?')} [SOLD]")

    print()
