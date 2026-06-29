"""
analysis/screener.py
--------------------
Vaulter AI — Adaptive Four-Stage Listing Screener

Stage 0 — Calibration  : Claude skims a sample of the export and generates
                         hard rules (Stage 1) and scoring dimensions (Stage 2)
                         specific to this file's structure and content.
                         No hardcoded rules — adapts to any CoStar export.

Stage 1 — Hard Rules   : Python applies Claude-generated rules instantly.
                         Any listing hitting 2+ rules is auto-eliminated.

Stage 2 — Scoring      : Python scores survivors on Claude-generated dimensions.
                         Top N advance to Stage 4.

Stage 3 — Safety Net   : Claude quickly checks rejects for wrongly-eliminated
                         listings (partial flood, hidden entitlement path,
                         portfolio adjacency, rising submarket signal).

Stage 4 — Deep Analysis: Claude fully analyzes finalists + any Stage 3 rescues.
                         Renders interactive React dashboard.

Stages 0–2 run in Python inside the MCP tool.
Stages 3–4 run in Claude Desktop after the tool returns.
"""

import json
import logging
import re

log = logging.getLogger("vaulter.screener")

# Stage 0 sends ALL rows to Claude using key fields only (~20k tokens, well within context)
DEFAULT_TOP_N           = 10   # top finalists forwarded to Stage 4 full analysis

# ── Vaulter investment thesis ──────────────────────────────────────
# Injected into Stage 0 calibration and dashboard instructions so
# all pricing analysis is evaluated through the correct lens.
INVESTMENT_THESIS = (
    "Vaulter is an opportunistic and value-add land investment firm focused on "
    "predevelopment value-add. Pricing must be evaluated from an investor perspective "
    "expecting a 2.5x-3x MOIC or more — NOT from a user or spec developer perspective. "
    "A site that looks expensive to an end user may still be a strong buy if the "
    "acquisition price leaves enough spread to a developer or user exit. Conversely, "
    "a site priced at or near developer/user comp levels leaves no room for Vaulter to "
    "create value and should score low. When generating pricing rules and dimensions, "
    "ask: does the basis allow for a 2.5x-3x return by selling to a developer or user "
    "after predevelopment work — not whether the price matches current comp transactions."
)


# ══════════════════════════════════════════════════════════════════
# Utilities — extract rows and parse price/acres
# ══════════════════════════════════════════════════════════════════

def extract_rows(chunks: list) -> list[str]:
    """
    Break CoStar chunks into individual listing rows.
    Each row is a pipe-separated string with 5+ separators.
    Deduplicates on the first 80 characters.
    """
    rows: list[str] = []
    seen: set[str]  = set()
    for chunk in chunks:
        text = chunk["text"] if isinstance(chunk, dict) else str(chunk)
        for line in text.splitlines():
            line = line.strip()
            if line.count("|") >= 5:
                key = line[:80].lower()
                if key not in seen:
                    seen.add(key)
                    rows.append(line)
    return rows


def _get_address(row: str) -> str:
    """Return the first readable text segment from a pipe-separated row."""
    for part in [p.strip() for p in row.split("|")][:5]:
        if (
            len(part) >= 8
            and not part.replace(".", "").replace(",", "").isdigit()
            and not (len(part) == 2 and part.isalpha())
            and not re.fullmatch(r"\d{5}", part)
        ):
            return part[:80]
    return "Unknown listing"


# CoStar standard export column indices (0-based, from 290-column export)
# Used by _get_price_acres to extract values by position rather than regex.
# These are stable across CoStar exports for the same template.
_COSTAR_PRICE_IDX = 15   # "For Sale Price"
_COSTAR_ACRES_IDX = 126  # "Land Area (AC)"


def _get_price_acres(row: str) -> tuple:
    """
    Extract price, acreage, and $/acre from a raw CoStar pipe-separated row.

    Primary method: field position lookup using known CoStar column indices.
    This is exact — no regex guessing. Falls back to regex scanning if the
    row doesn't have enough fields (e.g. partial chunks).

    CoStar standard export (290 columns):
      Field 15  = For Sale Price
      Field 126 = Land Area (AC)
    """
    price = None
    acres = None

    parts = [p.strip() for p in row.split("|")]

    # ── Primary: column position lookup ───────────────────────────
    def _parse_float(s: str):
        try:
            return float(s.replace(",", "")) if s else None
        except ValueError:
            return None

    if len(parts) > _COSTAR_PRICE_IDX:
        p = _parse_float(parts[_COSTAR_PRICE_IDX])
        if p and 100_000 <= p <= 500_000_000:
            price = int(p)

    if len(parts) > _COSTAR_ACRES_IDX:
        a = _parse_float(parts[_COSTAR_ACRES_IDX])
        if a and 0.5 <= a <= 2000.0:
            acres = a

    # ── Fallback: regex scan (for partial/reordered rows) ─────────
    if price is None:
        for pm in re.findall(r"(\d{6,9}(?:\.\d+)?)", row):
            try:
                p = float(pm)
                if 100_000 <= p <= 500_000_000:
                    price = int(p)
                    break
            except ValueError:
                pass

    if acres is None:
        # Only look at decimal values (acreage almost always has decimals)
        # and exclude lat/lon range (>30 with 6+ decimal places)
        for pa in re.findall(r"\b(\d+\.\d{1,3})\b", row):
            try:
                a = float(pa)
                if 0.5 <= a <= 2000.0 and not (30.0 <= a <= 50.0 and len(pa.split(".")[-1]) >= 5):
                    acres = a
                    break
            except ValueError:
                pass

    ppa = int(price / acres) if price and acres and acres > 0 else None
    return price, acres, ppa


# ══════════════════════════════════════════════════════════════════
# Stage 0 — Claude calibration
# ══════════════════════════════════════════════════════════════════

CALIBRATION_PROMPT = """You are setting up an automated screening pipeline for commercial real estate listings.

Here are {n} sample rows from a CoStar export (each row is pipe-separated fields):

{sample}

Your job: analyze this data and define screening criteria specific to THIS file.

STAGE 1 — HARD RULES (automatic dealbreakers, no AI involved):
Define 4-8 rules that instantly eliminate listings. Rules must be binary and
checkable from keywords in the raw row text. A listing triggering 2+ rules is cut.
Good rules catch: flood zones, zoning mismatches, outlying markets with no
infrastructure, residential brokers selling commercial land, missing utility data
in risky locations, unrealistic pricing for the use type.

STAGE 2 — SCORING DIMENSIONS (ranking survivors):
Define 3-6 dimensions to score surviving listings 0 to max_points each.
Dimensions must be measurable from keywords in this data.
Good dimensions measure: submarket growth tier, zoning match to use,
utility provider reliability, flood risk absence, pricing vs market norms.

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON:
{{
  "data_fields_observed": ["list what fields you actually see in these rows"],
  "hard_rules": [
    {{
      "id": "snake_case_id",
      "description": "Plain English: what this catches and why it matters as a dealbreaker",
      "keywords": ["keyword1", "keyword2"],
      "match_type": "any_present | none_present | conflict",
      "conflict_keywords": ["only needed for conflict type"]
    }}
  ],
  "scoring_dimensions": [
    {{
      "id": "snake_case_id",
      "description": "Plain English: what this measures and why it matters",
      "max_points": 2,
      "high_score_keywords": ["keywords indicating a GOOD score"],
      "low_score_keywords": ["keywords indicating a BAD score"]
    }}
  ]
}}

match_type values:
  any_present  — flag if ANY keyword in 'keywords' appears in the listing text
  none_present — flag if NONE of the keywords appear (catches missing data like no utility)
                 if conflict_keywords provided, also requires one of those to be present
  conflict     — flag if ANY keyword from 'keywords' AND ANY from 'conflict_keywords' both appear

IMPORTANT: calibrate to THIS dataset. Use the actual submarket names, zoning codes,
utility providers, and use types you see in these rows. Do not use generic defaults."""


CALIBRATION_PROMPT = """You are setting up an automated screening pipeline for commercial real estate listings.

Here are {n} sample rows from a CoStar export (pipe-separated fields):

{sample}

Analyze this data and define screening criteria specific to THIS file.

Output your response using ONLY the line formats below — no JSON, no markdown, no extra explanation.
Use >>> as the separator between fields on each line.

FORMAT:
FIELDS: field1, field2, field3, ...
RULE: rule_id >>> plain English description of what this catches and why >>> match_type >>> keyword1, keyword2, keyword3 >>> conflict_keyword1, conflict_keyword2
DIM: dim_id >>> plain English description of what this measures >>> max_points >>> high_kw1, high_kw2 >>> low_kw1, low_kw2

FIELDS line: list the data fields you actually see in these rows (one line).

RULE lines (4-8 rules): automatic dealbreakers. A listing hitting 2+ rules is cut.
  match_type options:
    any_present  — flag if ANY keyword appears in the listing
    none_present — flag if NONE of the keywords appear (use conflict column to require a condition)
    conflict     — flag if ANY keyword from column 4 AND ANY from column 5 both appear
  Leave the last column empty if not needed.

DIM lines (3-6 dimensions): scoring criteria for survivors.
  max_points: integer (2 or 3)
  Column 4: keywords that indicate a HIGH (good) score on this dimension
  Column 5: keywords that indicate a LOW (bad) score on this dimension

IMPORTANT: use the actual submarket names, zoning codes, utility providers, and use types
you see in these rows. Do not use generic placeholders.

INVESTMENT CONTEXT — apply this when generating any pricing-related rules or dimensions:
{thesis}

Example output (do not copy — generate from the actual data above):
FIELDS: flood zone, zoning, submarket, utility provider, proposed land use
RULE: flood_high >>> 100-year floodplain creates lender resistance and mitigation cost >>> any_present >>> 100-year floodplain, sfha, high risk areas >>>
RULE: zoning_mismatch >>> Residential zoning marketed as commercial with no entitlement path >>> conflict >>> r-43, r-1, r-2 >>> commercial, retail, office, industrial
DIM: submarket_tier >>> Growth tier of the submarket >>> 3 >>> loop 303, west i-10, east valley >>> outlying, far west, gila bend"""


def _parse_calibration_response(response: str) -> dict:
    """
    Parse the line-based calibration response from Claude.
    Format uses >>> as separator — no JSON, no escaping issues.
    """
    fields: list[str] = []
    rules:  list[dict] = []
    dims:   list[dict] = []

    valid_match_types = {"any_present", "none_present", "conflict"}

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # ── FIELDS line ───────────────────────────────────────────
        if line.upper().startswith("FIELDS:"):
            fields = [f.strip() for f in line[7:].split(",") if f.strip()]

        # ── RULE line ─────────────────────────────────────────────
        elif line.upper().startswith("RULE:"):
            parts = [p.strip() for p in line[5:].split(">>>")]
            if len(parts) < 4:
                continue   # skip malformed lines
            rule_id     = re.sub(r"\W+", "_", parts[0].lower()).strip("_") or f"rule_{len(rules)}"
            description = parts[1] if len(parts) > 1 else rule_id
            match_type  = parts[2].strip().lower() if len(parts) > 2 else "any_present"
            if match_type not in valid_match_types:
                match_type = "any_present"
            keywords         = [k.strip().lower() for k in parts[3].split(",") if k.strip()]
            conflict_keywords= [k.strip().lower() for k in parts[4].split(",") if k.strip()] \
                               if len(parts) > 4 else []
            if keywords:   # only add if there are actual keywords
                rules.append({
                    "id":                rule_id,
                    "description":       description,
                    "match_type":        match_type,
                    "keywords":          keywords,
                    "conflict_keywords": conflict_keywords,
                })

        # ── DIM line ──────────────────────────────────────────────
        elif line.upper().startswith("DIM:"):
            parts = [p.strip() for p in line[4:].split(">>>")]
            if len(parts) < 3:
                continue   # skip malformed lines
            dim_id      = re.sub(r"\W+", "_", parts[0].lower()).strip("_") or f"dim_{len(dims)}"
            description = parts[1] if len(parts) > 1 else dim_id
            try:
                max_pts = int(parts[2])
                max_pts = max(1, min(max_pts, 5))   # clamp to 1-5
            except (ValueError, IndexError):
                max_pts = 2
            high_kw = [k.strip().lower() for k in parts[3].split(",") if k.strip()] \
                      if len(parts) > 3 else []
            low_kw  = [k.strip().lower() for k in parts[4].split(",") if k.strip()] \
                      if len(parts) > 4 else []
            dims.append({
                "id":                  dim_id,
                "description":         description,
                "max_points":          max_pts,
                "high_score_keywords": high_kw,
                "low_score_keywords":  low_kw,
            })

    return {
        "data_fields_observed": fields,
        "hard_rules":           rules,
        "scoring_dimensions":   dims,
    }


# ── Dynamic column selector ───────────────────────────────────────────────────
# No hardcoded column names or indices.
# select_signal_columns() analyzes the actual file and picks columns that have
# real variance across listings — the ones that will drive meaningful rules and
# scoring dimensions. Works with any CoStar export regardless of template.

def select_signal_columns(df) -> list[tuple[int, str]]:
    """
    Analyze all columns in the dataframe and return (index, name) pairs for
    the columns most useful for investment screening — high fill rate,
    meaningful variety, and relevance to deal evaluation.

    Signal score = fill_rate × variety_score × relevance_boost where:
      - fill_rate:       fraction of non-null values (0-1)
      - variety_score:   rewards 3-100 unique values for categoricals,
                         high coefficient of variation for numerics.
                         Penalizes binary columns (Yes/No, True/False) unless
                         they are highly relevant (flood, SFHA etc.)
      - relevance_boost: multiplier for columns whose name contains terms
                         commonly associated with investment decisions

    Skips: coordinates, phone/fax/contact fields, IDs, columns that are
    entirely the same value or entirely unique values.

    Returns up to 20 highest-signal columns.
    """
    import pandas as pd
    n = len(df)
    scores = []

    skip_patterns = [
        "latitude", "longitude", "phone", "fax",
        "panel number", "map identifier", "map date",
        "parcel number", "propertyid", " id",
        "city state zip", "firm id",
        "scale",           # always "Independent" — no signal
        "continent",       # always "Americas" — no signal
        "subcontinent",    # always "North America" — no signal
        "country",         # always "United States" — no signal
        "constr status",   # always "Existing" for land — no signal
        "building status", # always "Existing" for land — no signal
    ]

    # Terms that boost relevance — these columns matter for deal screening
    relevance_terms = [
        "price", "acre", "flood", "sfha", "fema", "zone", "zoning",
        "submarket", "cluster", "utility", "electric", "water", "sewer",
        "days on market", "sale", "proposed", "land use", "county",
        "corridor", "market", "broker", "agent", "company",
    ]

    for i, col in enumerate(df.columns):
        col_lower = col.lower()

        # Skip columns whose names indicate they are IDs, coordinates, or contacts
        if any(p in col_lower for p in skip_patterns):
            continue

        series = df[col].dropna()
        if len(series) == 0:
            continue

        fill_rate    = len(series) / n
        unique_count = series.nunique()

        # Skip degenerate columns
        if fill_rate < 0.10:
            continue
        if unique_count <= 1:
            continue
        # Skip near-all-unique columns (phone numbers, addresses, IDs)
        if unique_count > 50 and (unique_count / len(series)) > 0.90:
            continue

        is_numeric = pd.api.types.is_numeric_dtype(series)

        if is_numeric:
            try:
                mean = float(series.mean())
                std  = float(series.std())
                cv   = std / mean if mean != 0 else 0
                variety_score = min(abs(cv), 3.0) / 3.0
            except Exception:
                variety_score = 0.3
        else:
            # Categorical variety: sweet spot is 3-100 unique values
            # Binary (2 unique) is only valuable if it's a relevant flag
            if unique_count == 2:
                variety_score = 0.3   # low — binary is usually Yes/No noise
            elif unique_count <= 20:
                variety_score = 1.0   # best — meaningful categories
            elif unique_count <= 100:
                variety_score = 0.7
            else:
                variety_score = 0.3

        # Relevance boost for investment-relevant column names
        relevance_boost = 2.0 if any(t in col_lower for t in relevance_terms) else 1.0

        # Extra boost for binary flood/SFHA columns (critical despite being binary)
        if unique_count == 2 and any(t in col_lower for t in ["sfha", "flood", "sfha"]):
            relevance_boost = 3.0

        signal = fill_rate * variety_score * relevance_boost
        scores.append((signal, i, col))

    if not scores:
        return []

    scores.sort(reverse=True)

    # No hardcoded column count. Use a statistical cutoff instead: keep any
    # column whose signal is at least 25% of the top column's signal.
    # This naturally adapts — a sparse dataset might yield 8 columns,
    # a rich one might yield 40+. The cutoff finds the "elbow" where
    # signal drops off rather than picking an arbitrary fixed number.
    top_signal = scores[0][0]
    threshold  = top_signal * 0.25
    selected   = [(idx, col) for sig, idx, col in scores if sig >= threshold]

    # Always include price and land area regardless of signal score — these
    # are fundamental to investment analysis even if they score lower due to
    # high cardinality artifacts from pipe-row reconstruction.
    must_include_terms = ["price", "area (ac", "area (sf"]
    selected_names = {col.lower() for _, col in selected}
    for i, col in enumerate(df.columns):
        col_lower = col.lower()
        if any(t in col_lower for t in must_include_terms) and col_lower not in selected_names:
            series = df[col].dropna()
            if len(series) / n > 0.50 and series.nunique() > 1:
                selected.append((i, col))
                selected_names.add(col_lower)

    # Final cleanup: drop anything that turned out single-valued
    clean = [(idx, col) for idx, col in selected
             if col in df.columns and df[col].dropna().nunique() > 1]

    log.info(f"[SCREENER] Stage 0: {len(clean)} columns selected (signal threshold: {threshold:.3f})")
    return clean


# Cache so we only compute once per screener run
_SIGNAL_COLUMNS_CACHE: list[tuple[int, str]] | None = None
_SIGNAL_COLUMNS_HEADERS: list[str] | None = None


def _build_row_for_prompt(row: str, col_indices: list[tuple[int, str]]) -> str:
    """
    Build a compact representation of a single listing using only the
    dynamically selected high-signal columns.
    No hardcoded field names — uses whatever columns the data has.
    """
    parts = [p.strip() for p in row.split("|")]
    fields = []
    for idx, name in col_indices:
        if idx < len(parts):
            val = parts[idx].strip()
            if val and val not in ("nan", "None", ""):
                val = val.replace(">>>", "---").replace("\n", " ")[:60]
                fields.append(f"{name}: {val}")
    return " | ".join(fields) if fields else row[:300]


def _get_signal_columns(rows: list[str], headers: list[str]) -> list[tuple[int, str]]:
    """
    Get signal columns from the row data.
    Uses the dataframe-based selector if headers are available,
    falls back to a heuristic scan of the pipe rows otherwise.
    """
    global _SIGNAL_COLUMNS_CACHE, _SIGNAL_COLUMNS_HEADERS

    if _SIGNAL_COLUMNS_CACHE is not None and _SIGNAL_COLUMNS_HEADERS == headers:
        return _SIGNAL_COLUMNS_CACHE

    if headers:
        import pandas as pd
        # Reconstruct a dataframe from the pipe rows + headers for signal analysis
        try:
            parsed = []
            for row in rows:
                parts = [p.strip() for p in row.split("|")]
                # Pad or trim to match headers
                if len(parts) < len(headers):
                    parts += [""] * (len(headers) - len(parts))
                parsed.append(parts[:len(headers)])
            df_tmp = pd.DataFrame(parsed, columns=headers)
            # Replace empty strings and "nan"/"None" with actual NaN
            df_tmp = df_tmp.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "NaT": pd.NA})
            # Convert numeric-looking columns
            for col in df_tmp.columns:
                try:
                    df_tmp[col] = pd.to_numeric(df_tmp[col])
                except Exception:
                    pass
            result = select_signal_columns(df_tmp)
            _SIGNAL_COLUMNS_CACHE   = result
            _SIGNAL_COLUMNS_HEADERS = headers
            return result
        except Exception:
            pass

    # Fallback: pick columns by position that tend to have variety
    # (first 5, middle 5, and a few known useful positions)
    n_parts = len(rows[0].split("|")) if rows else 20
    indices = list(range(min(5, n_parts))) + list(range(n_parts//2, min(n_parts//2+5, n_parts)))
    return [(i, f"col_{i}") for i in indices]


def _clean_row_for_prompt(row: str) -> str:
    """
    Backward-compatible wrapper — used when headers are not available.
    Sanitises the row and truncates to a reasonable length.
    """
    return row.replace(">>>", "---").replace("\n", " ")[:400]


def calibrate_pipeline(
    sample_rows: list[str],
    api_key: str,
    headers: list[str] | None = None,
) -> dict:
    """
    Stage 0: dynamically select the highest-signal columns from the actual
    data, then send ALL rows (compressed to those columns) to Claude.

    No hardcoded column names or indices. Works with any export format:
      1. Python scores every column by fill rate × variance
      2. Top 20 highest-signal columns are selected automatically
      3. All rows are compressed to just those columns (~10-15k tokens)
      4. Claude sees the full dataset and generates rules + dimensions
         based on real data relationships, not hardcoded assumptions

    Falls back to sensible defaults if the API call fails.
    """
    import anthropic

    # Step 1: select high-signal columns dynamically
    col_indices = _get_signal_columns(sample_rows, headers or [])
    col_names   = [name for _, name in col_indices]
    log.info(f"[SCREENER] Stage 0: selected {len(col_indices)} signal columns: {col_names}")

    # Step 2: compress all rows to selected columns only
    sample_text = "\n".join(
        f"Row {i+1}: {_build_row_for_prompt(row, col_indices)}"
        for i, row in enumerate(sample_rows)
    )

    # Step 3: build calibration prompt — column names are dynamic, from the data
    prompt = CALIBRATION_PROMPT.format(
        n=len(sample_rows),
        sample=sample_text,
        thesis=INVESTMENT_THESIS,
    )

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw          = message.content[0].text.strip()
        calibration  = _parse_calibration_response(raw)

        # If parsing produced nothing useful, fall back
        if not calibration["hard_rules"] and not calibration["scoring_dimensions"]:
            log.warning("[SCREENER] Stage 0 response parsed but yielded no rules — using fallback")
            return _fallback_calibration()

        log.info(
            f"[SCREENER] Stage 0 complete: "
            f"{len(calibration['hard_rules'])} rules, "
            f"{len(calibration['scoring_dimensions'])} dimensions"
        )
        return calibration

    except Exception as e:
        log.warning(f"[SCREENER] Stage 0 failed ({e}) — using fallback defaults")
        return _fallback_calibration()


def _fallback_calibration() -> dict:
    """
    Fallback if Stage 0 API call fails.
    Phoenix-area defaults — better than nothing.
    """
    return {
        "data_fields_observed": ["flood_zone", "zoning", "use_type", "utility", "submarket"],
        "hard_rules": [
            {
                "id": "flood_high",
                "description": "100-year floodplain / SFHA — expensive mitigation, lender resistance",
                "keywords": ["100-year floodplain", "sfha", "high risk areas", "1% annual chance"],
                "match_type": "any_present",
                "conflict_keywords": [],
            },
            {
                "id": "zoning_use_mismatch",
                "description": "Residential zoning marketed as commercial — no entitlement path",
                "keywords": ["r-43", "r-1", "r-2", "r-3", "single family residential"],
                "match_type": "conflict",
                "conflict_keywords": ["commercial", "retail", "office", "industrial"],
            },
            {
                "id": "agricultural_outlying",
                "description": "Agricultural use in outlying market — no development signal",
                "keywords": ["agricultural"],
                "match_type": "conflict",
                "conflict_keywords": ["outlying", "gila bend", "tonopah", "wittmann"],
            },
            {
                "id": "no_utility_outlying",
                "description": "No major utility in outlying market — extension cost risk",
                "keywords": ["arizona public service", "aps", "salt river project", "srp"],
                "match_type": "none_present",
                "conflict_keywords": ["outlying", "gila bend", "tonopah", "wittmann"],
            },
            {
                "id": "residential_broker_commercial",
                "description": "Residential brokerage selling commercial land",
                "keywords": ["keller williams", "kw realty", "remax", "coldwell banker"],
                "match_type": "conflict",
                "conflict_keywords": ["commercial", "industrial", "retail", "office"],
            },
        ],
        "scoring_dimensions": [
            {
                "id": "submarket_tier",
                "description": "Submarket growth tier — high-growth corridors score highest",
                "max_points": 3,
                "high_score_keywords": ["loop 303", "west i-10", "east valley", "surprise",
                                        "peoria", "queen creek", "gilbert", "goodyear", "buckeye"],
                "low_score_keywords": ["outlying", "gila bend", "tonopah", "southwest outlying"],
            },
            {
                "id": "zoning_match",
                "description": "Zoning match to marketed use — PAD and exact matches score highest",
                "max_points": 3,
                "high_score_keywords": ["pad", "planned area development", "g-i",
                                        "general industrial", "c-2", "c-3"],
                "low_score_keywords": ["r-43", "r-1", "r-2", "agricultural", "a-1"],
            },
            {
                "id": "utility_provider",
                "description": "Utility provider — APS/SRP = industrial-capable power",
                "max_points": 2,
                "high_score_keywords": ["arizona public service", "aps", "salt river project", "srp"],
                "low_score_keywords": [],
            },
            {
                "id": "flood_risk",
                "description": "Flood risk absence — clean parcels score higher",
                "max_points": 2,
                "high_score_keywords": ["minimal flood hazard", "500-year floodplain",
                                        "moderate to low risk"],
                "low_score_keywords": ["100-year floodplain", "high risk areas", "sfha"],
            },
        ],
    }


# ══════════════════════════════════════════════════════════════════
# Stage 1 — Apply Claude-generated hard rules
# ══════════════════════════════════════════════════════════════════

def _matches_rule(text_lower: str, rule: dict) -> bool:
    """Check whether a listing row triggers one hard rule."""
    keywords    = [k.lower() for k in rule.get("keywords", [])]
    conflict_kw = [k.lower() for k in rule.get("conflict_keywords", [])]
    match_type  = rule.get("match_type", "any_present")

    if match_type == "any_present":
        return any(k in text_lower for k in keywords)

    elif match_type == "none_present":
        absent = not any(k in text_lower for k in keywords)
        if conflict_kw:
            return absent and any(k in text_lower for k in conflict_kw)
        return absent

    elif match_type == "conflict":
        primary_hit  = any(k in text_lower for k in keywords)
        conflict_hit = any(k in text_lower for k in conflict_kw)
        return primary_hit and conflict_hit

    return False


def stage1_hard_rules(
    listing_text: str, rules: list[dict]
) -> tuple[bool, list[str]]:
    """
    Apply all hard rules to one listing row.
    Returns (eliminated, [triggered rule descriptions]).
    Eliminated = True when 2+ rules trigger.
    """
    triggered: list[str] = []
    t = listing_text.lower()
    for rule in rules:
        if _matches_rule(t, rule):
            # .get() guards against a missing description key
            triggered.append(rule.get("description", rule.get("id", "Unknown rule")))
    return len(triggered) >= 2, triggered


# ══════════════════════════════════════════════════════════════════
# Stage 2 — Score with Claude-generated dimensions
# ══════════════════════════════════════════════════════════════════

def stage2_score(
    listing_text: str, dimensions: list[dict]
) -> tuple[int, int, dict, list[str]]:
    """
    Score one listing across all Claude-generated dimensions.

    IMPORTANT — missing data handling:
    When neither a high nor low keyword is found for a dimension, that means
    the data needed to evaluate it is absent or unrecognized — NOT a neutral
    "average" signal. Giving half credit for missing data silently inflates
    scores for listings with incomplete information (e.g. "price not disclosed"
    scoring as well as a listing with a verified favorable price).

    Missing data now scores 0 points (no credit without evidence) and is
    tracked in unverifiable_dims so Stage 4 can flag these listings for
    manual verification rather than treating them as confirmed strong.

    Returns (total_score, max_possible_score, breakdown_dict, unverifiable_dims).
    """
    t         = listing_text.lower()
    breakdown: dict[str, int] = {}
    unverifiable: list[str] = []
    total     = 0
    max_total = 0

    for i, dim in enumerate(dimensions):
        max_pts = dim.get("max_points", 2)
        high_kw = [k.lower() for k in dim.get("high_score_keywords", [])]
        low_kw  = [k.lower() for k in dim.get("low_score_keywords", [])]
        dim_id  = dim.get("id", f"dim_{i}")   # safe fallback if id missing
        max_total += max_pts

        has_high = any(k in t for k in high_kw)
        has_low  = any(k in t for k in low_kw)

        if has_high and not has_low:
            pts = max_pts
        elif has_high and has_low:
            pts = max(1, max_pts // 2)   # mixed signals — genuinely ambiguous
        elif has_low:
            pts = 0
        else:
            # No keyword matched either way — data missing or unrecognized.
            # Zero credit, not neutral. Flagged for Stage 4 review.
            pts = 0
            unverifiable.append(dim_id)

        breakdown[dim_id] = pts
        total += pts

    return total, max_total, breakdown, unverifiable


# ══════════════════════════════════════════════════════════════════
# Orchestrate Stages 0 → 1 → 2
# ══════════════════════════════════════════════════════════════════

def run_pipeline(
    costar_chunks: list,
    api_key: str,
    top_n: int = DEFAULT_TOP_N,
    headers: list[str] | None = None,
) -> dict:
    """
    Run Stages 0, 1, and 2 on a set of CoStar chunks.

    Stage 0: One Claude API call — generates rules + dimensions from a sample.
    Stage 1: Python hard-rule elimination. 2+ rule hits = eliminated.
    Stage 2: Python scores ALL survivors, assigns verdicts to ALL of them.
             No top_n cap here — verdicts are assigned based on score percentage,
             not rank. The dashboard decides what detail level to show.

    Verdict thresholds (of max possible score):
      >= 65% → Pursue
      >= 35% → Scrutinize
      <  35% → Pass (survived hard rules but scored too low)

    Stage 1 rejects → always Pass (hard dealbreaker hit).
    """
    rows = extract_rows(costar_chunks)
    log.info(f"[SCREENER] {len(rows)} rows extracted from {len(costar_chunks)} chunks")

    if not rows:
        return {
            "total": 0, "calibration": {}, "hard_rules": [], "scoring_dimensions": [],
            "max_score": 10, "finalists": [], "stage1_rejects": [], "stage2_rejects": [],
            "stage1_eliminated": 0, "stage2_eliminated": 0,
            "error": "No listing rows found in the CoStar export.",
        }

    # ── Stage 0 — Calibration ─────────────────────────────────────
    # Pass headers to calibrate_pipeline so the signal column selector can
    # reconstruct a dataframe and score each column by fill rate × variance.
    # If headers are empty, the selector falls back to heuristic column picking.
    hdr = headers or []
    log.info(f"[SCREENER] Stage 0: analyzing {len(rows)} rows with dynamic column selection ({len(hdr)} headers available)")
    calibration = calibrate_pipeline(rows, api_key, headers=hdr)
    hard_rules  = calibration.get("hard_rules") or []
    score_dims  = calibration.get("scoring_dimensions") or []
    max_score   = sum(d.get("max_points", 2) for d in score_dims) or 10

    # ── Stage 1 — Hard Rules ──────────────────────────────────────
    survivors:      list[dict] = []
    stage1_rejects: list[dict] = []

    for row in rows:
        eliminated, flags = stage1_hard_rules(row, hard_rules)
        price, acres, ppa = _get_price_acres(row)
        record = {
            "address": _get_address(row),
            "raw":     row,
            "price":   price,
            "acres":   acres,
            "ppa":     ppa,
            "flags":   flags,
        }
        if eliminated:
            record["stage"]              = "stage1_reject"
            record["elimination_reason"] = "; ".join(flags)
            record["verdict"]            = "Pass"
            record["reason"]             = "; ".join(flags[:2])
            stage1_rejects.append(record)
        else:
            if flags:
                record["single_flag"] = flags[0]
            survivors.append(record)

    log.info(f"[SCREENER] Stage 1: {len(stage1_rejects)} eliminated, {len(survivors)} survive")

    # ── Stage 2 — Score and Rank ALL survivors ────────────────────
    # No top_n cap. Every survivor gets scored and a verdict assigned
    # based purely on score percentage vs max_score.
    # unverifiable_dims tracks dimensions where data was missing —
    # those listings get flagged, not silently scored as neutral.
    for record in survivors:
        total, _, breakdown, unverifiable = stage2_score(record["raw"], score_dims)
        record["score"]              = total
        record["max_score"]          = max_score
        record["score_breakdown"]    = breakdown
        record["unverifiable_dims"]  = unverifiable

    survivors.sort(key=lambda x: x["score"], reverse=True)

    # Up to top_n by score = Pursue (go to Stage 4 full analysis).
    # "Up to" matters: if fewer than top_n survivors exist, or several tie
    # at the cutoff score, we don't force exactly top_n — we take however
    # many genuinely qualify, capped at top_n.
    actual_n = min(top_n, len(survivors))
    finalists      = survivors[:actual_n]
    stage2_rejects = survivors[actual_n:]

    for r in finalists:
        r["stage"]   = "finalist"
        r["verdict"] = "Pursue"
        unverif = r.get("unverifiable_dims", [])
        flag_note = f" — UNVERIFIED: {', '.join(unverif)}" if unverif else ""
        r["reason"]  = f"Score {r['score']}/{max_score} — top finalist{flag_note}"

    for r in stage2_rejects:
        r["stage"]   = "scrutinize"
        r["verdict"] = "Scrutinize"
        r["reason"]  = f"Score {r['score']}/{max_score} — outside top {actual_n}, review in Stage 3"

    log.info(f"[SCREENER] Stage 2: {len(finalists)} finalists (up to {top_n}, Pursue), {len(stage2_rejects)} for Stage 3 safety net")

    return {
        "total":              len(rows),
        "calibration":        calibration,
        "hard_rules":         hard_rules,
        "scoring_dimensions": score_dims,
        "max_score":          max_score,
        "finalists":          finalists,
        "stage1_rejects":     stage1_rejects,
        "stage2_rejects":     stage2_rejects,
        "stage1_eliminated":  len(stage1_rejects),
        "stage2_eliminated":  len(stage2_rejects),
        "top_n":              top_n,
        "error":              None,
    }


def _assign_verdict(listing: dict, max_score: int) -> tuple[str, str]:
    """
    Assign a Pursue / Scrutinize / Pass verdict to a finalist listing
    based on its score relative to the max possible score.

    Thresholds:
      >= 65% of max → Pursue
      >= 35% of max → Scrutinize
      <  35% of max → Pass

    Returns (verdict, reason_string).
    """
    score = listing.get("score", 0)
    pct   = score / max_score if max_score > 0 else 0

    # Build a short reason from score breakdown + any single flag
    bd        = listing.get("score_breakdown", {})
    low_dims  = [k.replace("_", " ") for k, v in bd.items() if v == 0]
    flag      = listing.get("single_flag", "")
    reason_parts = [f"Score {score}/{max_score}"]
    if low_dims:
        reason_parts.append(f"low on {', '.join(low_dims[:2])}")
    if flag:
        reason_parts.append(flag[:80])
    reason = " — ".join(reason_parts)

    if pct >= 0.65:
        return "Pursue", reason
    elif pct >= 0.35:
        return "Scrutinize", reason
    else:
        return "Pass", reason


# ══════════════════════════════════════════════════════════════════
# Format output for Claude — Stages 3, 4, and dashboard
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# Format output for Claude — pre-structured data + dashboard render
# ══════════════════════════════════════════════════════════════════

def format_output(
    result: dict,
    portfolio: list,
    web_intel: str,
    email_intel: str,
) -> str:
    """
    Assemble pipeline results into a structured string for Claude (Stages 3-5).

    Stage 3 — Safety net:  Claude reviews Stage 1 + 2 rejects for mistakes.
    Stage 4 — Full analysis: Claude analyzes each Pursue finalist in depth.
    Stage 5 — Dashboard: ALL 200+ listings shown.
      - Pursue:     full analysis per card (reasons, price/AC, submarket, Google Earth note)
      - Scrutinize: address + 2 flags only (compact)
      - Pass:       address + 2 flags only (compact)

    Scrutinize and Pass are kept deliberately compact so all 200+ listings
    fit in Claude's context without overflow.
    """
    import json as _json

    ms     = result.get("max_score", 10)
    top_n  = result.get("top_n", DEFAULT_TOP_N)
    SEP    = "═" * 62
    out    = []
    thesis = INVESTMENT_THESIS

    # ── Separate by verdict ───────────────────────────────────────
    finalists  = result.get("finalists",     [])
    s1_rejects = result.get("stage1_rejects", [])
    s2_rejects = result.get("stage2_rejects", [])
    all_listings = finalists + s1_rejects + s2_rejects

    pursue_list     = sorted(
        [l for l in all_listings if l.get("verdict") == "Pursue"],
        key=lambda l: l.get("score", 0), reverse=True
    )
    scrutinize_list = [l for l in all_listings if l.get("verdict") == "Scrutinize"]
    pass_list       = [l for l in all_listings if l.get("verdict") == "Pass"]

    n_total      = result["total"]
    n_pursue     = len(pursue_list)
    n_scrutinize = len(scrutinize_list)
    n_pass       = len(pass_list)

    # ── Pipeline summary ──────────────────────────────────────────
    out.append(SEP)
    out.append("  PIPELINE COMPLETE")
    out.append(SEP)
    out.append(f"  Total listings   : {n_total}")
    out.append(f"  Stage 1 cut      : {result['stage1_eliminated']}  (hard rules)")
    out.append(f"  Stage 2 ranked   : {len(finalists) + len(s2_rejects)}  (scored + sorted)")
    out.append(f"  Pursue           : {n_pursue}")
    out.append(f"  Scrutinize       : {n_scrutinize}")
    out.append(f"  Pass             : {n_pass}")
    out.append("")

    # ── Calibration summary ───────────────────────────────────────
    rules = result.get("hard_rules", [])
    dims  = result.get("scoring_dimensions", [])
    cal   = result.get("calibration", {})
    out.append(SEP)
    out.append("  STAGE 0 CALIBRATION")
    out.append(SEP)
    if cal.get("data_fields_observed"):
        out.append(f"  Fields observed: {', '.join(cal['data_fields_observed'][:10])}")
    out.append(f"  Hard rules ({len(rules)}): " + " | ".join(r.get("id","") for r in rules))
    out.append(f"  Score dims ({len(dims)}): " + " | ".join(d.get("id","") for d in dims))
    out.append("")

    # ── PURSUE — full detail ──────────────────────────────────────
    pursue_data = []
    for i, lst in enumerate(pursue_list):
        unverif = lst.get("unverifiable_dims", [])
        entry = {
            "id":               i + 1,
            "address":          lst.get("address", "Unknown"),
            "verdict":          "Pursue",
            "score":            lst.get("score"),
            "max_score":        ms,
            "submarket":        lst.get("submarket", ""),
            "flags":            lst.get("flags", []),
            "reason":           lst.get("reason", ""),
            "unverified_dims":  unverif,
            "data_confidence":  "LOW — missing data on key dimensions" if len(unverif) >= 2 else
                                 "MEDIUM — one dimension unverified" if unverif else
                                 "HIGH — all dimensions confirmed",
        }
        if lst.get("price"):
            entry["price_fmt"] = f"${lst['price']:,.0f}"
        else:
            entry["price_fmt"] = "NOT DISCLOSED"
        if lst.get("acres"):
            entry["acres"] = lst["acres"]
        if lst.get("ppa"):
            entry["ppa_fmt"] = f"${lst['ppa']:,.0f}/AC"
        else:
            entry["ppa_fmt"] = "CANNOT CALCULATE — missing price or acreage"
        pursue_data.append(entry)

    out.append(SEP)
    out.append(f"  PURSUE — {n_pursue} listings (full detail, sorted by score)")
    out.append(SEP)
    out.append(_json.dumps(pursue_data, indent=2))
    out.append("")

    # ── SCRUTINIZE — compact: address + 2 flags only ──────────────
    scrutinize_data = []
    for lst in scrutinize_list:
        flags = lst.get("flags", [])
        if not flags:
            reason = lst.get("reason", "")
            flags = [f.strip() for f in reason.split("—") if f.strip()][:2]
        scrutinize_data.append({
            "address": lst.get("address", "Unknown"),
            "verdict": "Scrutinize",
            "flags":   flags[:2],
        })

    out.append(SEP)
    out.append(f"  SCRUTINIZE — {n_scrutinize} listings (address + 2 flags)")
    out.append(SEP)
    out.append(_json.dumps(scrutinize_data, indent=2))
    out.append("")

    # ── PASS — compact: address + 2 flags only ───────────────────
    pass_data = []
    for lst in pass_list:
        flags = lst.get("flags", [])
        if not flags:
            reason = lst.get("reason", "")
            flags = [f.strip() for f in reason.split("—") if f.strip()][:2]
        pass_data.append({
            "address": lst.get("address", "Unknown"),
            "verdict": "Pass",
            "flags":   flags[:2],
        })

    out.append(SEP)
    out.append(f"  PASS — {n_pass} listings (address + 2 flags)")
    out.append(SEP)
    out.append(_json.dumps(pass_data, indent=2))
    out.append("")

    # ── Stage 1 rejects detail (for Stage 3 safety net) ──────────
    s1_detail = [
        {"address": r.get("address","Unknown"), "rules_hit": r.get("flags",[])}
        for r in s1_rejects
    ]
    out.append(SEP)
    out.append(f"  STAGE 1 REJECTS — {len(s1_detail)} listings (for Stage 3 safety net review)")
    out.append(SEP)
    out.append(_json.dumps(s1_detail, indent=2))
    out.append("")

    # ── Portfolio context ─────────────────────────────────────────
    out.append(SEP)
    out.append(f"  PORTFOLIO CONTEXT — {len(portfolio)} active properties")
    out.append(SEP)
    by_state: dict = {}
    for p in portfolio:
        by_state.setdefault(p.get("state","?"), []).append(p.get("city",""))
    for state, cities in sorted(by_state.items()):
        out.append(f"  {state}: {chr(44).join(set(c for c in cities if c))[:120]}")
    out.append("")

    # ── Market intelligence ───────────────────────────────────────
    if web_intel:
        out.append(SEP)
        out.append("  MARKET INTELLIGENCE")
        out.append(SEP)
        out.append(web_intel[:1200])
        out.append("")

    # ── Instructions for Stages 3, 4, and 5 ─────────────────────
    out.append(SEP)
    out.append("  INSTRUCTIONS — STAGES 3, 4, AND 5")
    out.append(SEP)
    out.append(f"""
INVESTMENT CONTEXT (apply to all analysis):
{thesis}

YOU MUST COMPLETE THREE STAGES IN ORDER:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE 3 — SAFETY NET (suggested approach — use your judgment on how to present this)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Review ALL listings outside the Pursue list above — that means every
Scrutinize and every Stage 1 reject. For each one check:
  - Did the hard rule fire correctly, or was it a false positive?
  - Did the scoring rank it too low? (e.g. unusual zoning code, non-standard
    flood notation, broker from a different market but legitimate)
  - Is there a hidden entitlement path the scoring missed?
  - Is there portfolio adjacency to an existing Vaulter property?
  - Is the submarket rising faster than the scoring captured?
If any listing should have ranked higher, rescue it and add it to the Pursue list.
These rescues go directly into Stage 4 alongside the listings already in Pursue.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE 4 — DEEP ANALYSIS (suggested approach — use your judgment on how to present this)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run full analysis on: the Pursue listings above (up to {top_n}, could be fewer
if the dataset has less than {top_n} qualifying listings) + any Stage 3 rescues.

IMPORTANT — check data_confidence and unverified_dims on each listing FIRST.
A score is not the same as a confirmed strength. "unverified_dims" lists which
scoring dimensions had no matching data (price not disclosed, missing zoning,
etc.) — these scored 0, not a neutral midpoint, but the listing may still rank
high on other genuinely strong dimensions. Treat LOW/MEDIUM confidence listings
as "needs verification before treating as a top priority," not as confirmed wins.
Call this out explicitly in your analysis for any listing with unverified_dims.

For each one cover:
  - Data confidence check: flag if price, acres, or PPA could not be calculated
  - Why this site meets the 2.5x-3x MOIC thesis (only if price/acres are confirmed)
  - Zoning status and entitlement path
  - Flood risk assessment
  - Infrastructure / utility confirmation
  - Broker credibility signal
  - Key risk or condition to verify before LOI
  - Google Earth check: highway access, surrounding uses, existing structures
Store this analysis per listing — it becomes the card content in Stage 5.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE 5 — RENDER REACT DASHBOARD (render this immediately after stages 3+4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Render a React artifact showing ALL {n_total} listings — not a sample, not the
top N of each section. If a section is long (Scrutinize/Pass can run 100+),
implement pagination or a "show more" expand button inside that section rather
than silently cutting the list short. Every listing in the JSON data above must
be reachable in the rendered UI, even if not all visible without a click.

DASHBOARD LAYOUT:
  • Dark navy header (#1A2456): "CoStar Screening Results — {n_total} listings"
  • Summary bar: Pursue ({n_pursue}) | Scrutinize ({n_scrutinize}) | Pass ({n_pass})
  • Three accordion sections, collapsed by default, useState for open/close
  • For Scrutinize and Pass: render in pages of ~20 cards with a "Show more"
    button, OR a scrollable virtualized list — your choice, but no listing
    should be dropped or omitted from the data passed to the component

PURSUE section (green #15803D, light green background):
  Shows the original finalists (up to {top_n}) + any Stage 3 rescues.
  Each card shows:
    • Address (bold) | Score {ms}/{ms} | Price (or "NOT DISCLOSED") | $/AC if available
    • Submarket
    • Data confidence badge: HIGH (green) / MEDIUM (amber) / LOW (red) based on data_confidence field
    • Full analysis from Stage 4 (3-5 bullets: MOIC thesis, zoning, flood, infra, key risk)
    • If unverified_dims is non-empty, show a warning line: "Unverified: {{dims}} — confirm before treating as priority"
    • Badge: "Stage 3 Rescue" in gold if the listing was rescued from outside the original Pursue list
    • Italic footer: "Verify on Google Earth: highway access, surrounding uses, existing structures"

SCRUTINIZE section (amber #D97706, light amber background):
  All listings that scored well but didn't make the Pursue cutoff and weren't rescued.
  Show all of them (paginated/expandable as needed). Each card shows:
    • Address (bold)
    • 2 flag chips — why it didn't rank higher, no deep analysis

PASS section (gray #64748B, light gray background):
  Stage 1 hard rule eliminations + low scorers not rescued.
  Show all of them (paginated/expandable as needed). Each card shows:
    • Address (bold)
    • 2 flag chips — what rule or score killed it

STYLING:
  • 2-column card grid on wide screens, 1-column on narrow
  • Compact cards — no nested scroll within sections
  • Section header shows count badge
  • All {n_total} listings must be present in the component's data, reachable
    via pagination/expansion — do not truncate the underlying list
""")

    return "\n".join(out)
