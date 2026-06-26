"""
mcp_server.py
--------------
Vaulter AI — MCP Server

Single entry point that runs everything:
  - PDF watcher      (background thread)
  - Scheduler        (background thread — emails every 6h, web scrapes, property intel daily)
  - MCP server       (main thread — serves claude.ai requests)

Start with:
  python main.py mcp

Connect in claude.ai:
  Settings → Connectors → Add custom connector
  Name : Vaulter AI Property Intelligence
  URL  : http://YOUR_NGROK_URL (from ngrok http 8765)
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("vaulter.mcp")


# ══════════════════════════════════════════════════════════════════
# Background Services
# ══════════════════════════════════════════════════════════════════

def _start_watcher():
    """Start the PDF watcher in a background thread."""
    try:
        from ingestion.watcher import start_watcher_background
        log.info("[WATCHER] Starting PDF watcher...")
        start_watcher_background()
        log.info("[WATCHER] Running — watching data/watched_folder/")
    except Exception as e:
        log.warning(f"[WATCHER] Could not start: {e}")


def _start_scheduler():
    """Start the background scheduler in a background thread."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from datetime import datetime as _dt
        from config import WEB_SOURCES, SCHEDULER_TIMEZONE

        scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)

        # ── Web scraping — each source on its own frequency ───────
        for source in WEB_SOURCES:
            def _scrape(name=source["name"]):
                try:
                    from pipeline.web_scraper import scrape_all
                    scrape_all(target_name=name)
                except Exception as ex:
                    log.warning(f"[SCHEDULER] Scrape failed ({name}): {ex}")
                    # Never let a scrape error crash the MCP server
                    return
            scheduler.add_job(
                _scrape,
                trigger=IntervalTrigger(hours=source["frequency_hours"]),
                id=f"scrape_{source['name'].replace(' ', '_')}",
                next_run_time=_dt.now() + __import__('datetime').timedelta(seconds=60),
                replace_existing=True,
            )

        # ── Email — every 30 minutes ───────────────────────────────
        def _email():
            try:
                from pipeline.email_reader import process_all_emails
                process_all_emails()
            except Exception as ex:
                log.warning(f"[SCHEDULER] Email check failed: {ex}")
                return

        scheduler.add_job(
            _email,
            trigger=IntervalTrigger(minutes=30),
            id="check_email",
            next_run_time=_dt.now() + __import__('datetime').timedelta(seconds=60),
            replace_existing=True,
        )

        # ── Property intelligence — daily at 6 AM ─────────────────
        def _property_scrape():
            try:
                from pipeline.property_scraper import scrape_all_properties
                scrape_all_properties()
            except Exception as ex:
                log.warning(f"[SCHEDULER] Property scrape failed: {ex}")
                return
        scheduler.add_job(
            _property_scrape,
            trigger=CronTrigger(hour=6, minute=0),
            id="property_scrape",
            replace_existing=True,
        )

        scheduler.start()
        log.info("[SCHEDULER] Running — emails every 30min, web scrapes per source, property intel daily 6am")

        # Keep thread alive
        while True:
            time.sleep(60)

    except Exception as e:
        log.warning(f"[SCHEDULER] Could not start: {e}")
        # Log to stderr so Claude Desktop can see it
        import sys
        print(f"[SCHEDULER] Fatal error: {e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════
# MCP Tools
# ══════════════════════════════════════════════════════════════════

def create_mcp_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="Vaulter AI Property Intelligence",
        instructions="""You have access to Vaulter AI's complete property intelligence database.
This includes:
- 48 active properties across Arizona, California, New Mexico, Colorado, and Texas
- Due diligence PDFs (surveys, ALTA, title reports)
- Property intelligence scraped from Google News and City-Data for each property
- Market research from CBRE, Marcus & Millichap, JLL, and GlobeSt
- Broker emails and document attachments (Word, Excel, PowerPoint, PDF)
- Inbound CoStar exports and broker listing spreadsheets

Use these tools to answer questions about the portfolio, specific properties,
market conditions, risk flags, and broker communications.
Always use the most specific tool available for the question.

For screening inbound listings from a CoStar export or broker spreadsheet,
use screen_listings — it gathers data from every source in the system and
returns a complete dossier for you to evaluate each property."""
    )

    @mcp.tool()
    def search_database(query: str, n_results: int = 15) -> str:
        """
        Search the Vaulter AI database for any topic.
        Use this for general questions about properties, markets, emails, or documents.
        Args:
            query: What to search for
            n_results: Number of results (default 15, max 20)
        """
        try:
            from analysis.rag_engine import free_search, format_context_for_claude
            chunks  = free_search(query, n=min(max(1, n_results), 20))
            context = format_context_for_claude(chunks)
            return context if context else "No relevant data found for this query."
        except Exception as e:
            return f"Search failed: {e}"

    @mcp.tool()
    def get_property_info(property_name: str) -> str:
        """
        Get all available intelligence for a specific property.
        Args:
            property_name: Property name (e.g. "Magic Ranch 10", "Mesa Del Sol", "Rita Ranch")
        """
        try:
            from analysis.rag_engine import get_property_context, format_context_for_claude
            chunks  = get_property_context(property_name, n=20)
            context = format_context_for_claude(chunks)
            return context if context else f"No data found for {property_name}."
        except Exception as e:
            return f"Property lookup failed: {e}"

    @mcp.tool()
    def get_portfolio_list(group_by: str = "state") -> str:
        """
        Get the complete list of all 48 active Vaulter AI properties.
        Args:
            group_by: "state" or "stage" (default: "state")
        """
        try:
            from pipeline.property_scraper import load_properties
            props, _ = load_properties()
            groups: dict = {}
            key_field = "category" if group_by == "stage" else "state"
            for p in props:
                k = p.get(key_field, "Unknown")
                groups.setdefault(k, []).append(p)
            lines = [f"VAULTER AI PORTFOLIO — {len(props)} active properties (by {group_by}):\n"]
            for k in sorted(groups):
                lines.append(f"{k} ({len(groups[k])}):")
                for p in groups[k]:
                    lines.append(f"  - {p['name']} | {p.get('category','')} | {p.get('city','')}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed to load portfolio: {e}"

    @mcp.tool()
    def get_properties_by_stage(stage: str) -> str:
        """
        Get all properties currently in a specific stage.
        Args:
            stage: Acquisition, Pre-Plat, Final Engineering, Disposition, Site Maintenance, Rezone, Development
        """
        try:
            from pipeline.property_scraper import load_properties
            props, _ = load_properties()
            filtered = [p for p in props if p.get("category", "").lower() == stage.lower()]
            if not filtered:
                return f"No active properties found in the '{stage}' stage."
            by_state: dict = {}
            for p in filtered:
                by_state.setdefault(p.get("state", "Unknown"), []).append(p)
            lines = [f"PROPERTIES IN {stage.upper()} — {len(filtered)} total:\n"]
            for state in sorted(by_state):
                lines.append(f"{state} ({len(by_state[state])}):")
                for p in by_state[state]:
                    lines.append(f"  - {p['name']} | {p.get('city', '')}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"Stage filter failed: {e}"

    @mcp.tool()
    def check_inbox_now() -> str:
        """
        Pull new emails from Outlook right now and store them in the database.
        Use this when the user asks about new emails, anything in the inbox,
        or wants the latest broker communications.
        """
        try:
            from pipeline.email_reader import process_all_emails
            log.info("[MCP] Live email pull triggered by user")
            process_all_emails()
            from analysis.rag_engine import get_recent_emails, format_context_for_claude
            chunks  = get_recent_emails(n=10)
            context = format_context_for_claude(chunks)
            return context if context else "Inbox checked — no new emails found."
        except Exception as e:
            return f"Email check failed: {e}"

    @mcp.tool()
    def get_email_highlights(n_emails: int = 15) -> str:
        """
        Get recent broker email content from the database.
        Args:
            n_emails: Number of email chunks to retrieve (default 15)
        """
        try:
            from analysis.rag_engine import get_recent_emails, format_context_for_claude
            chunks  = get_recent_emails(n=n_emails)
            context = format_context_for_claude(chunks)
            return context if context else "No broker emails found in the database."
        except Exception as e:
            return f"Email retrieval failed: {e}"

    @mcp.tool()
    def get_risk_scan(state: str = None) -> str:
        """
        Search the database for risk-related content across the portfolio.
        Args:
            state: Optional state filter (e.g. "Arizona"). Leave empty for full portfolio.
        """
        try:
            from analysis.rag_engine import get_cross_property_context, format_context_for_claude
            query  = "zoning denial environmental flood easement title dispute permit delay market softening legal issue risk"
            chunks = get_cross_property_context(query, state=state, n=18)
            return format_context_for_claude(chunks) or "No risk-related data found."
        except Exception as e:
            return f"Risk scan failed: {e}"

    @mcp.tool()
    def get_market_intelligence(state: str = None) -> str:
        """
        Get market intelligence from web scrapes and property news.
        Args:
            state: Optional state filter (e.g. "California"). Leave empty for all markets.
        """
        try:
            from analysis.rag_engine import (
                get_cross_property_context,
                get_recent_web_intelligence,
                format_context_for_claude,
            )
            query      = "land market new homes permits builder activity pricing trends supply demand"
            chunks     = get_cross_property_context(query, state=state, n=12)
            web_chunks = get_recent_web_intelligence(n=6)
            seen, merged = set(), []
            for c in chunks + web_chunks:
                key = c["text"][:80]
                if key not in seen:
                    seen.add(key)
                    merged.append(c)
            return format_context_for_claude(merged[:18]) or "No market intelligence found."
        except Exception as e:
            return f"Market intelligence failed: {e}"

    @mcp.tool()
    def get_database_stats() -> str:
        """
        Get a summary of what is currently in the Vaulter AI database.
        Use this to show the user how much data has been ingested.
        """
        try:
            from ingestion.embedder import get_stats
            from ingestion.registry import load_registry
            from config import DATA_DIR
            import json

            stats    = get_stats()
            registry = load_registry()

            def _load_json(path):
                try:
                    return json.loads(path.read_text()) if path.exists() else {}
                except Exception:
                    return {}

            email_registry = _load_json(DATA_DIR / "email_registry.json")
            web_registry   = _load_json(DATA_DIR / "web_registry.json")

            lines = [
                f"Vaulter AI Database — {stats['total_chunks']:,} total chunks",
                f"  PDF documents ingested : {len(registry)}",
                f"  Web sources scraped    : {len(web_registry)}",
                f"  Emails processed       : {len(email_registry)}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Stats failed: {e}"

    @mcp.tool()
    def open_property_files(property_name: str) -> str:
        """
        Open File Explorer to show all actual documents and files for a property.
        Use this when the user wants to:
        - see, browse, open, or download files for a property
        - access the actual PDF, Word, Excel, or PowerPoint documents
        - view attachments or documents related to a property
        - click on or open files for a property
        Args:
            property_name: Property name (e.g. "Mesa Del Sol", "Magic Ranch 10", "Forney")
        """
        import subprocess
        from config import PROCESSED_DIR
        try:
            folder = None
            matches = []

            if PROCESSED_DIR.exists():
                for state_dir in PROCESSED_DIR.iterdir():
                    if not state_dir.is_dir():
                        continue
                    for prop_dir in state_dir.iterdir():
                        if not prop_dir.is_dir():
                            continue
                        if property_name.lower() in prop_dir.name.lower():
                            matches.append(prop_dir)

            if len(matches) == 1:
                folder = matches[0]
            elif len(matches) > 1:
                exact = [m for m in matches if m.name.lower() == property_name.lower()]
                folder = exact[0] if exact else matches[0]

            if folder and folder.exists():
                subprocess.Popen(f'explorer "{folder}"')
                files = [f for f in folder.iterdir() if f.is_file()]
                if files:
                    file_list = "\n".join(f"  - {f.name}" for f in sorted(files))
                    return f"Opened File Explorer to {folder.name} folder.\n\nFiles available:\n{file_list}"
                else:
                    return f"Opened File Explorer to {folder.name} folder — no files found yet."
            else:
                subprocess.Popen(f'explorer "{PROCESSED_DIR}"')
                return f"No folder found for '{property_name}'. Opened the processed documents folder instead."

        except Exception as e:
            return f"Could not open folder: {e}"

    @mcp.tool()
    def open_general_files() -> str:
        """
        Open File Explorer to the general documents folder.
        Use this when the user asks for files that are not tied to a specific property,
        such as market reports, CoStar exports, general spreadsheets, or any file
        that came from email but wasn't matched to a specific property.
        """
        import subprocess
        from config import PROCESSED_DIR
        try:
            general_dir = PROCESSED_DIR / "general"
            general_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(f'explorer "{general_dir}"')
            files = [f for f in general_dir.iterdir() if f.is_file()]
            if files:
                file_list = "\n".join(f"  - {f.name}" for f in sorted(files))
                return f"Opened File Explorer to general documents folder.\n\nFiles available:\n{file_list}"
            else:
                return "Opened general documents folder — no files there yet."
        except Exception as e:
            return f"Could not open folder: {e}"

    # ── FOUR-STAGE LISTING SCREENER ───────────────────────────────

    @mcp.tool()
    def screen_listings(source_file: str = "CostarExport.xlsx", top_n: int = 30) -> str:
        """
        Run the four-stage listing screener on a CoStar export or broker spreadsheet.

        Stage 1 (Python, instant) — eliminates obvious dealbreakers using hard rules:
          flood zone, zoning mismatch, agricultural outlying, no utility, wrong broker type.
        Stage 2 (Python, fast)    — scores survivors on submarket, zoning, utility, flood.
          Takes the top N finalists. No AI used in Stages 1 or 2.
        Stage 3 (Claude)          — quick reconsideration pass: checks if any reject was
          wrongly cut (partial flood, hidden entitlement path, portfolio adjacency,
          rising submarket signal).
        Stage 4 (Claude)          — full deep analysis on finalists + any Stage 3 rescues.
          Uses portfolio context, market intelligence, and broker email signals.
          Outputs a Pursue / Scrutinize / Pass verdict per listing.
        Dashboard (Claude)        — renders a React artifact with filter tabs, compact
          cards, expandable detail, sort options, and all rejects visible.

        Use when asked to:
        - Screen, filter, or analyze listings from a CoStar export
        - Find which properties Vaulter should pursue or pass on
        - Run an investment filter on inbound broker properties

        Args:
            source_file: Filename of the CoStar export (default: CostarExport.xlsx)
            top_n:       Max finalists to pass to Stage 4 (default: 30)
        """
        try:
            from analysis.screener import run_pipeline, format_output
            from analysis.rag_engine import (
                free_search,
                get_recent_emails,
                get_recent_web_intelligence,
            )
            from pipeline.property_scraper import load_properties
            from ingestion.embedder import get_collection
            from config import ANTHROPIC_API_KEY

            # ── Pull CoStar chunks from ChromaDB ──────────────────
            collection   = get_collection()
            costar_chunks: list = []

            if collection.count() > 0:
                # Primary: exact source filename match
                try:
                    res = collection.get(
                        where={"source": source_file},
                        limit=200,
                        include=["documents", "metadatas"],
                    )
                    if res and res.get("documents"):
                        for doc, meta in zip(res["documents"], res["metadatas"]):
                            costar_chunks.append({"text": doc, "meta": meta})
                except Exception:
                    pass

                # Fallback A: match by attachment type + partial filename
                if not costar_chunks:
                    try:
                        res = collection.get(
                            where={"source": "CostarExport.xlsx"},
                            limit=400,
                            include=["documents", "metadatas"],
                        )
                        if res and res.get("documents"):
                            stem = source_file.lower().replace(".xlsx", "").replace(".xls", "")
                            for doc, meta in zip(res["documents"], res["metadatas"]):
                                if stem in meta.get("source", "").lower():
                                    costar_chunks.append({"text": doc, "meta": meta})
                            if not costar_chunks:
                                # No filename match — use all Excel attachments
                                for doc, meta in zip(res["documents"], res["metadatas"]):
                                    costar_chunks.append({"text": doc, "meta": meta})
                    except Exception:
                        pass

                # Fallback B: semantic search
                if not costar_chunks:
                    costar_chunks = free_search(
                        f"CoStar land listing acre zoning submarket price {source_file}", n=25
                    )

            if not costar_chunks:
                return (
                    f"No CoStar data found for '{source_file}'.\n"
                    f"Run check_inbox_now to pull the latest emails, then try again.\n"
                    f"If the email has been checked, make sure the attachment was ingested "
                    f"(check database stats)."
                )

            log.info(f"[MCP] screen_listings: {len(costar_chunks)} chunks for '{source_file}'")

            # ── Stages 0, 1 & 2 — Python pipeline ────────────────
            result = run_pipeline(costar_chunks, api_key=ANTHROPIC_API_KEY, top_n=top_n)

            if result.get("error") and result["total"] == 0:
                return (
                    f"Pipeline error: {result['error']}\n"
                    f"Found {len(costar_chunks)} chunks but could not extract listing rows.\n"
                    f"The file may not have been ingested as row-per-line Excel data."
                )

            # ── Portfolio context ──────────────────────────────────
            portfolio: list = []
            try:
                portfolio, _ = load_properties()
            except Exception as e:
                log.warning(f"[MCP] Could not load portfolio: {e}")

            # ── Market intelligence ────────────────────────────────
            web_intel = ""
            try:
                web_queries = [
                    "Phoenix Arizona land market pricing per acre 2025 2026",
                    "Loop 303 West I-10 East Valley land absorption development activity",
                    "Arizona FEMA flood zone SFHA land development mitigation cost",
                    "Gila Bend outlying Arizona land infrastructure utilities extension",
                ]
                web_chunks   = get_recent_web_intelligence(n=12)
                seen_keys: set  = set()
                merged_web: list = []
                for c in web_chunks:
                    k = c["text"][:80]
                    if k not in seen_keys:
                        seen_keys.add(k)
                        merged_web.append(c)
                for q in web_queries:
                    for c in free_search(q, n=4):
                        k = c["text"][:80]
                        if k not in seen_keys:
                            seen_keys.add(k)
                            merged_web.append(c)

                web_lines: list = []
                for c in merged_web[:18]:
                    src   = c.get("source", "")
                    label = c.get("source_label", "Market Research")
                    date  = (c.get("scraped_at") or "")[:10] or "unknown"
                    web_lines.append(f"[{label} | {src} | {date}]")
                    web_lines.append(c["text"][:600])
                    web_lines.append("")
                web_intel = "\n".join(web_lines)
            except Exception as e:
                web_intel = f"Could not load market intelligence: {e}"

            # ── Email signals ──────────────────────────────────────
            email_intel = ""
            try:
                RE_KW = {
                    "costar", "acre", "listing", "zoning", "flood", "sfha",
                    "submarket", "phoenix", "arizona", "land", "parcel",
                    "infrastructure", "utility", "aps", "srp", "entitlement",
                    "days on market", "price reduction",
                }
                email_chunks = get_recent_emails(n=25)
                relevant = [
                    c for c in email_chunks
                    if any(kw in c["text"].lower() for kw in RE_KW)
                ]
                if relevant:
                    e_lines: list = []
                    for c in relevant[:10]:
                        src  = c.get("source", "")
                        subj = c.get("subject", "")
                        date = (c.get("scraped_at") or "")[:10] or "unknown"
                        hdr  = f"[Email | {src}"
                        if subj:
                            hdr += f" | Subject: {subj}"
                        hdr += f" | {date}]"
                        e_lines.append(hdr)
                        e_lines.append(c["text"][:500])
                        e_lines.append("")
                    email_intel = "\n".join(e_lines)
            except Exception:
                pass

            # ── Assemble and return ────────────────────────────────
            return format_output(result, portfolio, web_intel, email_intel)

        except Exception as e:
            log.error(f"[MCP] screen_listings failed: {e}", exc_info=True)
            return f"screen_listings failed: {e}"

    @mcp.tool()
    def run_google_places_export(property_name: str, radius_miles: float = 5.0) -> str:
        """
        Runs a Google Places API search for all businesses and employers near
        a Vaulter portfolio property and saves the results to a CSV and GeoJSON
        file in data/proximity_output/. This is the ONLY way to generate the
        proximity CSV — do not attempt this with web search, maps, or any other
        method. Always call this tool directly and immediately when the user
        asks to export proximity data, generate a proximity CSV, find what is
        near a property, or run a Google Places search for a property.

        This tool handles everything internally — geocoding, Google Places API
        calls across 17 categories, distance/direction calculations, highway
        extraction, CSV export, and GeoJSON export. Do not do any of these
        steps yourself. Just call this tool and tell the user where the files
        were saved.

        Args:
            property_name: Property name from the Vaulter Project Master
                           (e.g. "Pacific & Pinson - Forney", "Mesa Del Sol")
            radius_miles:  Search radius in miles (default: 5.0)
        """
        from proximity_tool import run_proximity_search
        from config import GOOGLE_PLACES_API_KEY
        from pathlib import Path

        api_key = GOOGLE_PLACES_API_KEY.strip()
        if not api_key:
            return "GOOGLE_PLACES_API_KEY not set. Add it to confidentials/.env and restart."

        return run_proximity_search(
            property_name=property_name,
            radius_miles=radius_miles,
            vaulter_dir=Path(__file__).parent,
            api_key=api_key,
        )

    return mcp


# ══════════════════════════════════════════════════════════════════
# Server Entry Point
# ══════════════════════════════════════════════════════════════════

def run_mcp_server(port: int = 8765):
    """
    Start background services then launch the MCP server.
    This is the single command that runs everything.
    """
    # ── Start background services ─────────────────────────────────
    def _safe_watcher():
        try:
            _start_watcher()
        except Exception as e:
            log.warning(f"[WATCHER] Fatal error: {e}")

    def _safe_scheduler():
        try:
            _start_scheduler()
        except Exception as e:
            log.warning(f"[SCHEDULER] Fatal error: {e}")

    watcher_thread = threading.Thread(target=_safe_watcher, daemon=True)
    watcher_thread.start()

    scheduler_thread = threading.Thread(target=_safe_scheduler, daemon=True)
    scheduler_thread.start()

    # ── Start MCP server (main thread) ────────────────────────────
    log.info("[MCP] Starting Vaulter AI MCP server...")
    mcp = create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()
