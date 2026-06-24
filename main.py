"""
main.py
-------
Vaulter AI Property Intelligence System
----------------------------------------
Single entry point for the entire system.

Usage:
  python main.py ingest                             — start the PDF watcher (Stage 1)
  python main.py stats                              — show database statistics
  python main.py query <text>                       — search the document database

  python main.py scrape                             — scrape all web sources (Stage 2)
  python main.py scrape "CBRE Market Reports"       — scrape one source by name
  python main.py web-sources                        — list all configured web sources
  python main.py email                              — pull new Outlook emails
  python main.py email --days 30                    — pull emails from last 30 days
  python main.py property-scrape                    — scrape news for all 48 properties
  python main.py property-scrape "Magic Ranch 10"   — scrape one property
  python main.py properties                         — list all properties from Project Master
  python main.py schedule                           — start the background scheduler
  python main.py auth                               — authorize Outlook (run once on setup)

  python main.py mcp                                — start the MCP server (Stage 3)
  python main.py mcp <port>                         — start MCP server on a custom port
"""

import os
import sys
import logging
from pathlib import Path

# ─── Lock working directory to project root ───────────────────────
os.chdir(str(Path(__file__).parent))

from config import LOG_DIR

# ─── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "vaulter.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("vaulter")


# ══════════════════════════════════════════════════════════════════
# Stage 1 — PDF Ingestion
# ══════════════════════════════════════════════════════════════════

def cmd_ingest():
    from ingestion.watcher import start_watcher
    log.info("=" * 60)
    log.info("  Vaulter AI Property Intelligence System")
    log.info("  Stage 1 — PDF Ingestion Pipeline")
    log.info(f"  Watching : {Path('data/watched_folder').resolve()}")
    log.info(f"  Database : {Path('data/chroma_db').resolve()}")
    log.info(f"  OCR      : Tesseract (auto-activated for scanned PDFs)")
    log.info("=" * 60)
    start_watcher()


def cmd_stats():
    import json
    from ingestion.embedder import get_stats
    from ingestion.registry import load_registry
    from config import DATA_DIR, CHROMA_DIR, CHROMA_COLLECTION_NAME

    stats    = get_stats()
    registry = load_registry()

    def _load_json(path):
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            return {}

    web_registry       = _load_json(DATA_DIR / "web_registry.json")
    prop_registry      = _load_json(DATA_DIR / "property_scrape_registry.json")
    email_registry_raw = _load_json(DATA_DIR / "email_registry.json")
    email_count        = len(email_registry_raw)

    web_chunks  = {}
    prop_chunks = {}
    email_total = 0
    results     = {"metadatas": []}

    try:
        import chromadb
        client     = chromadb.PersistentClient(path=str(CHROMA_DIR))
        collection = client.get_or_create_collection(CHROMA_COLLECTION_NAME)
        if collection.count() > 0:
            results = collection.get(
                limit=min(collection.count(), 9999),
                include=["metadatas"]
            )
            for meta in results["metadatas"]:
                t      = meta.get("type", "")
                source = meta.get("source", "unknown")
                if t == "web_scrape":
                    web_chunks[source] = web_chunks.get(source, 0) + 1
                elif t == "property_intelligence":
                    prop  = meta.get("property", "unknown")
                    stype = meta.get("source_type", "")
                    key   = f"{prop} ({stype})"
                    prop_chunks[key] = prop_chunks.get(key, 0) + 1
                elif t in ("email",) or t.startswith("email_attachment_"):
                    email_total += 1
    except Exception:
        pass

    W = 57
    print(f"\n{'=' * W}")
    print(f"  Vaulter AI — Database Stats")
    print(f"{'=' * W}")
    print(f"  Total chunks in ChromaDB : {stats['total_chunks']}")
    print()

    print(f"  Stage 1 — PDF Documents ({len(registry)})")
    if registry:
        for _, info in registry.items():
            ocr_tag = " [OCR]" if info.get("ocr_used") else ""
            print(f"    * {info['filename']}{ocr_tag}")
            print(f"      {info['chunks']} chunks | {info['pages']} pages | ingested {info['ingested_at'][:10]}")
    else:
        print("    (none yet — drop PDFs into data/watched_folder/State/Property/)")
    print()

    print(f"  Stage 2 — Web Scrapes ({len(web_chunks)} sources)")
    if web_chunks:
        for source, count in sorted(web_chunks.items()):
            last = web_registry.get(source, {}).get("last_scraped", "")[:10]
            print(f"    * {source} — {count} chunks | last scraped {last}")
    else:
        print("    (none yet — run 'python main.py scrape')")
    print()

    prop_names = set()
    for key in prop_chunks:
        name = key.rsplit(" (", 1)[0] if " (" in key else key
        prop_names.add(name)
    print(f"  Stage 2 — Property Intelligence ({len(prop_names)} properties scraped)")
    if prop_names:
        for name in sorted(prop_names):
            total = sum(v for k, v in prop_chunks.items() if k.rsplit(" (", 1)[0] == name)
            types = [k.rsplit(" (", 1)[1].rstrip(")") for k in prop_chunks if " (" in k and k.rsplit(" (", 1)[0] == name]
            print(f"    * {name} — {total} chunks ({', '.join(types) or 'scraped'})")
    else:
        print("    (none yet — run 'python main.py property-scrape')")
    print()

    print(f"  Stage 2 — Emails ({email_total} chunks from {email_count} messages)")
    if email_total == 0:
        print("    (none yet — run 'python main.py auth' then 'python main.py email')")
    else:
        email_type_counts = {}
        for meta in results["metadatas"]:
            t = meta.get("type", "")
            if t == "email":
                email_type_counts["body text"] = email_type_counts.get("body text", 0) + 1
            elif t.startswith("email_attachment_"):
                label = t.replace("email_attachment_", "")
                email_type_counts[label] = email_type_counts.get(label, 0) + 1
        for label, count in sorted(email_type_counts.items(), key=lambda x: -x[1]):
            print(f"    · {label}: {count} chunks")
    print()
    print(f"{'=' * W}\n")


def cmd_query(question: str):
    from ingestion.embedder import query_documents
    results = query_documents(question, n_results=5)
    print(f"\nTop results for: '{question}'\n")
    if not results:
        print("No results found — database may be empty.")
        return
    for r in results:
        print(f"[{r['filename']} | chunk {r['chunk']} | score {r['score']} | ocr={r['ocr']}]")
        print(r["text"][:300])
        print("-" * 50)


# ══════════════════════════════════════════════════════════════════
# Stage 2 — Web & Email Pipeline
# ══════════════════════════════════════════════════════════════════

def cmd_scrape(target_name: str = None):
    from pipeline.web_scraper import scrape_all
    scrape_all(target_name=target_name)


def cmd_web_sources():
    from config import WEB_SOURCES
    print(f"\nConfigured web sources ({len(WEB_SOURCES)}):\n")
    for s in WEB_SOURCES:
        print(f"  {s['name']}")
        print(f"    URL       : {s['url']}")
        print(f"    Frequency : every {s['frequency_hours']}h")
        print()


def cmd_email(lookback_days: int = None):
    from pipeline.email_reader import process_all_emails
    from config import OUTLOOK_LOOKBACK_DAYS
    days = lookback_days or OUTLOOK_LOOKBACK_DAYS
    log.info(f"Pulling emails from last {days} days...")
    process_all_emails(lookback_days=days)


def cmd_auth():
    from pipeline.outlook_auth import run_auth_flow
    run_auth_flow()


def cmd_property_scrape(target: str = None):
    from pipeline.property_scraper import scrape_all_properties
    scrape_all_properties(target_name=target)


def cmd_properties():
    from pipeline.property_scraper import load_all_properties
    props, sold = load_all_properties()
    print(f"\nVaulter AI Portfolio — {len(props)} active properties\n")
    by_state = {}
    for p in props:
        by_state.setdefault(p.get("state", "Unknown"), []).append(p)
    for state in sorted(by_state):
        print(f"  {state} ({len(by_state[state])}):")
        for p in by_state[state]:
            print(f"    · {p['name']} | {p.get('category', '')} | {p.get('city', '')}")
    if sold:
        print(f"\n  Sold / Inactive ({len(sold)}):")
        for p in sold:
            print(f"    · {p.get('name', '?')} | {p.get('state', '')}")


def cmd_schedule():
    from pipeline.scheduler import start_scheduler
    start_scheduler()


# ══════════════════════════════════════════════════════════════════
# Stage 3 — MCP Server
# ══════════════════════════════════════════════════════════════════

def cmd_mcp(port: int = None):
    from config import MCP_PORT, MCP_API_KEY
    run_port = port or MCP_PORT

    if not MCP_API_KEY:
        log.warning("=" * 60)
        log.warning("  WARNING: MCP_API_KEY is not set in your .env file.")
        log.warning("  Your MCP server has no authentication.")
        log.warning("  Generate a key: python -c \"import secrets; print(secrets.token_hex(24))\"")
        log.warning("  Then add MCP_API_KEY=<key> to confidentials/.env")
        log.warning("=" * 60)

    log.info("=" * 60)
    log.info("  Vaulter AI — MCP Server")
    log.info(f"  Transport  : stdio (Claude Desktop launches this process directly)")
    log.info(f"  Auth       : {'MCP_API_KEY set' if MCP_API_KEY else 'DISABLED — set MCP_API_KEY in .env'}")
    log.info("  Connect via claude.ai → Settings → Connectors")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 60)

    try:
        from mcp_server import run_mcp_server
        run_mcp_server(port=run_port)
    except ImportError as e:
        log.error(f"Missing dependency: {e}")
        log.error("Run: pip install mcp[cli] uvicorn")


# ══════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "ingest":
        cmd_ingest()

    elif args[0] == "stats":
        cmd_stats()

    elif args[0] == "query":
        if len(args) < 2:
            print("Usage: python main.py query <your question here>")
        else:
            cmd_query(" ".join(args[1:]))

    elif args[0] == "scrape":
        target = args[1] if len(args) > 1 else None
        cmd_scrape(target)

    elif args[0] == "web-sources":
        cmd_web_sources()

    elif args[0] == "email":
        days = None
        if "--days" in args:
            try:
                days = int(args[args.index("--days") + 1])
            except (IndexError, ValueError):
                print("Usage: python main.py email --days <number>")
                sys.exit(1)
        cmd_email(lookback_days=days)

    elif args[0] == "auth":
        cmd_auth()

    elif args[0] == "property-scrape":
        target = args[1] if len(args) > 1 else None
        cmd_property_scrape(target)

    elif args[0] == "properties":
        cmd_properties()

    elif args[0] == "schedule":
        cmd_schedule()

    elif args[0] == "mcp":
        port = int(args[1]) if len(args) > 1 else None
        cmd_mcp(port=port)

    elif args[0] in ("--help", "-h", "help"):
        print(__doc__)

    else:
        print(f"Unknown command: '{args[0]}'")
        print("Run 'python main.py --help' to see all commands.")
