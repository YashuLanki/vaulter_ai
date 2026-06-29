"""
proximity_tool.py
-----------------
Vaulter AI — Proximity Search MCP Tool

Drop this file into the vaulter_ai/ root directory.
Reads all configuration from data/config.json — no hardcoding.
Reads properties from data/Vaulter_Project_Master.csv (or .xlsx/.pdf).

Called by the proximity_search MCP tool in mcp_server.py.
"""

import csv
import glob
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("vaulter.proximity")

# ── State normalization ────────────────────────────────────────────────────
STATE_ABBR = {
    "Arizona": "AZ", "California": "CA", "Colorado": "CO",
    "New Mexico": "NM", "Texas": "TX", "Alabama": "AL", "Alaska": "AK",
    "Arkansas": "AR", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}
VALID_ABBR = set(STATE_ABBR.values())


def _norm_state(raw: str) -> str:
    raw = raw.strip()
    if raw in VALID_ABBR:
        return raw
    if raw in STATE_ABBR:
        return STATE_ABBR[raw]
    for full, abbr in STATE_ABBR.items():
        if full.startswith(raw) or raw.startswith(full[:5]):
            return abbr
    return ""


# ── Config loader ──────────────────────────────────────────────────────────
def _load_config() -> tuple:
    """
    Load categories and settings directly from config.py.
    config.py is the central Vaulter AI config — no separate config.json needed.
    Returns (categories, settings).
    """
    try:
        from config import (
            PROXIMITY_CATEGORIES,
            PROXIMITY_DEFAULT_RADIUS_MILES,
            PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY,
            PROXIMITY_GEOCODING_TIMEOUT,
            PROXIMITY_PLACES_REQUEST_DELAY,
        )
        categories = PROXIMITY_CATEGORIES
        settings = {
            "default_radius_miles":         PROXIMITY_DEFAULT_RADIUS_MILES,
            "summary_results_per_category": PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY,
            "geocoding_timeout_seconds":    PROXIMITY_GEOCODING_TIMEOUT,
            "places_request_delay_seconds": PROXIMITY_PLACES_REQUEST_DELAY,
        }
        return categories, settings
    except ImportError as e:
        raise ImportError(
            f"Missing proximity settings in config.py: {e}\n"
            f"Add PROXIMITY_CATEGORIES and settings to config.py."
        )


# ── Project Master loader ──────────────────────────────────────────────────
def _load_project_master(data_dir: Path) -> dict:
    """
    Returns {property_name: state_abbr} from CSV/Excel/PDF in data/.
    Priority: CSV > Excel > PDF (CSV is most reliable from Smartsheet export).
    """
    properties = {}

    # Project Master lives in data/project_master/
    pm_dir = data_dir / "project_master"
    search_dir = pm_dir if pm_dir.exists() else data_dir

    candidates = sorted(
        [f for f in glob.glob(str(search_dir / "*"))
         if Path(f).suffix.lower() in (".csv", ".xlsx", ".xls", ".pdf")
         and not Path(f).name.startswith(".")],
        key=lambda f: {".csv": 0, ".xlsx": 1, ".xls": 2, ".pdf": 3}.get(
            Path(f).suffix.lower(), 9)
    )
    pm_files = [f for f in candidates if any(
        kw in Path(f).name.lower()
        for kw in ["project", "master", "vaulter", "portfolio"]
    )] or candidates

    if not pm_files:
        log.warning("[PROXIMITY] No Project Master file found in data/")
        return properties

    chosen = pm_files[0]
    ext = Path(chosen).suffix.lower()
    log.info(f"[PROXIMITY] Reading Project Master: {Path(chosen).name}")

    try:
        if ext == ".csv":
            with open(chosen, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    name = (row.get("Project Name") or "").strip()
                    state = _norm_state((row.get("State") or "").strip())
                    if name and name != "Template" and "@" not in name and state:
                        properties[name] = state

        elif ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(chosen, read_only=True, data_only=True)
                ws = wb.active
                header = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i == 0:
                        header = [str(c or "").strip() for c in row]
                        continue
                    rd = dict(zip(header, [str(c or "").strip() for c in row]))
                    name = rd.get("Project Name", "").strip()
                    state = _norm_state(rd.get("State", "").strip())
                    if name and name != "Template" and "@" not in name and state:
                        properties[name] = state
                wb.close()
            except ImportError:
                log.warning("[PROXIMITY] openpyxl not installed — can't read Excel")

        elif ext == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(chosen) as pdf:
                    for page in pdf.pages:
                        table = page.extract_table()
                        if not table:
                            continue
                        for row in table:
                            if not row or len(row) < 2:
                                continue
                            name = (row[0] or "").strip()
                            if not name or "@" in name or name in (
                                    "Project Name", "Template", "Project Sponsor"):
                                continue
                            state = ""
                            for ci in [3, 2, 1]:
                                if ci < len(row):
                                    s = _norm_state((row[ci] or "").strip())
                                    if s:
                                        state = s
                                        break
                            if name and state and name not in properties:
                                properties[name] = state
            except ImportError:
                log.warning("[PROXIMITY] pdfplumber not installed — can't read PDF")

    except Exception as e:
        log.warning(f"[PROXIMITY] Project Master read error: {e}")

    return properties


# ── Distance / direction helpers ───────────────────────────────────────────
def _dist_dir(origin_lat, origin_lon, dest_lat, dest_lon):
    """Return (distance_miles, cardinal_direction)."""
    try:
        from geopy.distance import geodesic
        dist = round(geodesic(
            (origin_lat, origin_lon), (dest_lat, dest_lon)).miles, 2)
    except Exception:
        R = 3958.8
        lat1, lat2 = math.radians(origin_lat), math.radians(dest_lat)
        dlat = lat2 - lat1
        dlon = math.radians(dest_lon - origin_lon)
        a = (math.sin(dlat/2)**2
             + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2)
        dist = round(R * 2 * math.asin(math.sqrt(a)), 2)

    lat1, lat2 = math.radians(origin_lat), math.radians(dest_lat)
    dlon = math.radians(dest_lon - origin_lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dist, dirs[round(bearing / 22.5) % 16]


# ── Highway extraction ─────────────────────────────────────────────────────
HIGHWAY_PATTERNS = [
    (r"\bInterstate\s+(\d+[A-Z]?)\b",           "Interstate"),
    (r"\bI-(\d+[A-Z]?)\b",                       "Interstate"),
    (r"\bUS(?:\s+|-)Highway\s+(\d+[A-Z]?)\b",   "US Highway"),
    (r"\bU\.S\.\s+(\d+[A-Z]?)\b",               "US Highway"),
    (r"\bUS-(\d+[A-Z]?)\b",                      "US Highway"),
    (r"\bState\s+Highway\s+(\d+[A-Z]?)\b",       "State Highway"),
    (r"\bState\s+Route\s+(\d+[A-Z]?)\b",         "State Route"),
    (r"\bSH-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bTX-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bAZ-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bCA-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bCO-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bNM-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bFarm\s+to\s+Market\s+Road\s+(\d+[A-Z]?)\b", "Farm to Market Road"),
    (r"\bFM\s+(\d+[A-Z]?)\b",                    "Farm to Market Road"),
    (r"\bFM-(\d+[A-Z]?)\b",                      "Farm to Market Road"),
    (r"\bCounty\s+Road\s+(\d+[A-Z]?)\b",         "County Road"),
    (r"\bCR\s+(\d+[A-Z]?)\b",                    "County Road"),
]


def _extract_highways(records: list, lat: float, lon: float,
                      color: str = "#717D7E") -> list:
    """
    Extract named highways and roads from the addresses of already-found
    businesses. Reliable because Google always includes road names in addresses.
    """
    category = "Transportation & Infrastructure"
    icon = "🛣️"
    highway_mentions = {}

    for r in records:
        addr = r.get("address", "") + " " + r.get("notes", "")
        for pattern, road_type in HIGHWAY_PATTERNS:
            for num in re.findall(pattern, addr, re.IGNORECASE):
                if road_type == "Interstate":
                    label = f"I-{num}"
                elif road_type == "US Highway":
                    label = f"US-{num}"
                elif road_type in ("State Highway", "State Route"):
                    label = f"State Highway {num}"
                elif road_type == "Farm to Market Road":
                    label = f"FM {num}"
                elif road_type == "County Road":
                    label = f"CR {num}"
                else:
                    label = f"{road_type} {num}"

                if label not in highway_mentions:
                    highway_mentions[label] = {"road_type": road_type, "dists": []}
                highway_mentions[label]["dists"].append(r["distance_miles"])

    results = []
    for label, info in highway_mentions.items():
        min_dist = round(min(info["dists"]), 2)
        count = len(info["dists"])
        results.append({
            "name":           label,
            "category":       category,
            "icon":           icon,
            "color":          color,
            "address":        f"{count} business{'es' if count > 1 else ''} on this road within radius",
            "latitude":       lat,
            "longitude":      lon,
            "distance_miles": min_dist,
            "direction":      "N/A",
            "distance_label": f"~{min_dist} mi (nearest business on road)",
            "rating":         "",
            "source":         "Derived from Google Places addresses",
            "notes":          info["road_type"],
        })

    results.sort(key=lambda x: x["distance_miles"])
    return results


# ── Main proximity search ──────────────────────────────────────────────────
def run_proximity_search(property_name: str,
                         radius_miles: float,
                         vaulter_dir: Path,
                         api_key: str) -> str:
    """
    Core proximity search logic. Called by the MCP tool in mcp_server.py.
    Reads all config from data/config.json — no hardcoding.
    Returns a formatted string summary.
    """
    import requests

    data_dir = vaulter_dir / "data"
    prox_dir = vaulter_dir / "data" / "proximity_output"
    prox_dir.mkdir(exist_ok=True)

    # ── Load config from config.py ───────────────────────────────
    try:
        categories, settings = _load_config()
    except (ImportError, Exception) as e:
        return f"Configuration error: {e}"

    # Use config default radius if caller passed 0 or didn't specify
    if not radius_miles:
        radius_miles = settings["default_radius_miles"]

    delay   = settings["places_request_delay_seconds"]
    timeout = settings["geocoding_timeout_seconds"]
    top_n   = settings["summary_results_per_category"]

    # ── Load Project Master ───────────────────────────────────────
    properties = _load_project_master(data_dir)

    # ── Match property ────────────────────────────────────────────
    matched_name = matched_state = None
    if property_name in properties:
        matched_name = property_name
        matched_state = properties[property_name]
    else:
        matches = [(n, s) for n, s in properties.items()
                   if property_name.lower() in n.lower()]
        if len(matches) == 1:
            matched_name, matched_state = matches[0]
        elif len(matches) > 1:
            return (f"Multiple properties match '{property_name}':\n"
                    + "\n".join(f"  - {n}" for n, _ in matches)
                    + "\nPlease be more specific.")
        else:
            avail = "\n  ".join(sorted(properties)) if properties else \
                "(no Project Master found in data/)"
            return (f"'{property_name}' not found in Project Master.\n\n"
                    f"Available properties:\n  {avail}")

    # ── Geocode ───────────────────────────────────────────────────
    clean = re.sub(r"\s*\(.*?\)", "", matched_name).strip()
    clean = re.sub(r"\s+\d+$", "", clean).strip().replace(" & ", " and ")
    geocode_query = f"{clean}, {matched_state}"

    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": geocode_query, "key": api_key},
            timeout=timeout,
        )
        data = resp.json()
        if data.get("status") != "OK":
            return f"Geocoding failed for '{geocode_query}': {data.get('status')}"
        loc = data["results"][0]["geometry"]["location"]
        lat, lon = loc["lat"], loc["lng"]
    except Exception as e:
        return f"Geocoding error: {e}"

    radius_m = int(radius_miles * 1609.34)

    # ── Google Places search ──────────────────────────────────────
    all_records, seen_ids = [], set()

    for cat in categories:
        label  = cat["label"]
        icon   = cat.get("icon", "📍")
        color  = cat.get("color", "#888888")
        gtypes = cat.get("google_types", [])

        for ptype in gtypes:
            try:
                resp = requests.get(
                    "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                    params={
                        "location": f"{lat},{lon}",
                        "radius":   radius_m,
                        "type":     ptype,
                        "key":      api_key,
                    },
                    timeout=timeout,
                )
                for r in resp.json().get("results", []):
                    pid = r.get("place_id", "")
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    rloc = r.get("geometry", {}).get("location", {})
                    dlat, dlon = rloc.get("lat"), rloc.get("lng")
                    if dlat is None:
                        continue
                    dist, direction = _dist_dir(lat, lon, dlat, dlon)
                    if dist > radius_miles:
                        continue
                    all_records.append({
                        "name":           r.get("name", "Unknown"),
                        "category":       label,
                        "icon":           icon,
                        "color":          color,
                        "address":        r.get("vicinity", ""),
                        "latitude":       dlat,
                        "longitude":      dlon,
                        "distance_miles": dist,
                        "direction":      direction,
                        "distance_label": f"{direction} - {dist} mi",
                        "rating":         r.get("rating", ""),
                        "source":         "Google Places",
                        "notes":          ", ".join(r.get("types", [])),
                    })
                time.sleep(delay)
            except Exception as e:
                log.warning(f"[PROXIMITY] Places error ({ptype}): {e}")

    # ── Highway extraction ────────────────────────────────────────
    transport_color = "#717D7E"
    for cat in categories:
        if "transport" in cat["label"].lower() or "infrastructure" in cat["label"].lower():
            transport_color = cat.get("color", "#717D7E")
            break
    highway_records = _extract_highways(all_records, lat, lon, transport_color)
    all_records.extend(highway_records)

    # ── De-duplicate ──────────────────────────────────────────────
    seen_keys, deduped = set(), []
    for r in all_records:
        key = (r["name"].lower().strip(),
               round(r["latitude"], 4), round(r["longitude"], 4))
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(r)
    deduped.sort(key=lambda x: x["distance_miles"])

    # ── Export GeoJSON ────────────────────────────────────────────
    features = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "name":           f"[Subject] {matched_name}",
            "category":       "Subject Property",
            "distance_miles": 0,
            "distance_label": "Subject Property",
            "marker-color":   "#FFD700",
        },
    }]
    for r in deduped:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [r["longitude"], r["latitude"]]},
            "properties": {
                "name":           f"{r['icon']} {r['name']}",
                "category":       r["category"],
                "address":        r["address"],
                "distance_miles": r["distance_miles"],
                "distance_label": r["distance_label"],
                "rating":         r["rating"],
                "marker-color":   r["color"],
            },
        })

    slug     = matched_name.replace(" ", "_").replace("/", "-").replace("&", "and")
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    gj_path  = prox_dir / f"{slug}_{ts}.geojson"
    csv_path = prox_dir / f"{slug}_{ts}.csv"

    gj_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2),
        encoding="utf-8"
    )

    # ── Export CSV ────────────────────────────────────────────────
    fieldnames = ["name", "category", "address", "latitude", "longitude",
                  "distance_miles", "direction", "distance_label",
                  "rating", "source", "notes"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "name":           f"[Subject] {matched_name}",
            "category":       "Subject Property",
            "latitude":       lat,
            "longitude":      lon,
            "distance_miles": 0,
            "direction":      "N/A",
            "distance_label": "Subject Property",
            "source":         "Vaulter Project Master",
        })
        for r in deduped:
            w.writerow(r)

    log.info(f"[PROXIMITY] {matched_name} — {len(deduped)} results → {gj_path.name}")

    return (
        f"Proximity search complete for {matched_name}.\n"
        f"Radius: {radius_miles} miles | {len(deduped)} unique results found.\n\n"
        f"Files saved to data/proximity_output/:\n"
        f"  CSV     -> {csv_path.name}\n"
        f"  GeoJSON -> {gj_path.name} (drag into Felt)"
    )
