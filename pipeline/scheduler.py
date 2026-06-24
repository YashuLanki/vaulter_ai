"""
pipeline/scheduler.py
----------------------
Vaulter AI Stage 2 — Background Scheduler

Keeps all data pipelines running automatically:
  - Web sources (CBRE, Marcus & Millichap, GlobeSt) — each on its own frequency
  - Outlook email — every 30 minutes
  - Property intelligence (all properties from Project Master CSV) — daily at 6 AM

Called by:  python main.py schedule
"""

import logging
import sys
from datetime import datetime as _dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import LOG_DIR, WEB_SOURCES, SCHEDULER_TIMEZONE, LOG_LEVEL

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ─── Logging ──────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [SCHEDULER] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Jobs
# ══════════════════════════════════════════════════════════════════

def job_scrape(source_name: str):
    """Scrape one configured web source."""
    log.info(f"Scheduled scrape: {source_name}")
    try:
        from pipeline.web_scraper import scrape_all
        scrape_all(target_name=source_name)
    except Exception as e:
        log.error(f"Scrape job failed for '{source_name}': {e}")


def job_email():
    """Pull new Outlook emails."""
    log.info("Scheduled email check")
    try:
        from pipeline.email_reader import process_all_emails
        process_all_emails()
    except ValueError as e:
        log.warning(f"Outlook not authorized — run 'python main.py auth' first. ({e})")
    except Exception as e:
        log.error(f"Email job failed: {e}")


def job_property_scrape():
    """Scrape news and market data for all properties in the Project Master CSV."""
    log.info("Scheduled property intelligence scrape")
    try:
        from pipeline.property_scraper import scrape_all_properties
        scrape_all_properties()
    except FileNotFoundError as e:
        log.warning(str(e))
    except Exception as e:
        log.error(f"Property scrape job failed: {e}")


# ══════════════════════════════════════════════════════════════════
# Scheduler
# ══════════════════════════════════════════════════════════════════

def start_scheduler():
    scheduler = BlockingScheduler(timezone=SCHEDULER_TIMEZONE)

    # ── Web sources — each at its own configured frequency ────────
    for source in WEB_SOURCES:
        scheduler.add_job(
            job_scrape,
            trigger=IntervalTrigger(hours=source["frequency_hours"]),
            args=[source["name"]],
            id=f"scrape_{source['name'].replace(' ', '_')}",
            name=f"Scrape: {source['name']}",
            next_run_time=_dt.now() + __import__('datetime').timedelta(seconds=30),   # run immediately on startup
            replace_existing=True,
        )
        log.info(f"Scheduled '{source['name']}' — every {source['frequency_hours']}h")

    # ── Outlook email — every 30 minutes ─────────────────────────
    scheduler.add_job(
        job_email,
        trigger=IntervalTrigger(minutes=30),
        id="check_email",
        name="Email: Outlook Pull",
        next_run_time=_dt.now() + __import__('datetime').timedelta(seconds=30),
        replace_existing=True,
    )
    log.info("Scheduled Outlook email — every 30 min")

    # ── Property intelligence — daily at 6:00 AM ─────────────────
    scheduler.add_job(
        job_property_scrape,
        trigger=CronTrigger(hour=6, minute=0),
        id="property_scrape",
        name="Property Intelligence Scrape",
        replace_existing=True,
    )
    log.info("Scheduled property intelligence scrape — daily at 6:00 AM")

    log.info("=" * 60)
    log.info("  Vaulter AI Stage 2 Scheduler STARTED")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 60)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
        scheduler.shutdown()