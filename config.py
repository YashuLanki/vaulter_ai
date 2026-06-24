"""
config.py
---------
Central configuration for the Vaulter AI Property Intelligence System.
All paths, settings, and constants live here.

Cross-platform: automatically detects Windows or Mac and sets the correct paths.
To adapt this project to a new machine, only this file needs to be updated.

Secrets (.env and outlook_token.json) are stored in:
  Windows : C:/Users/<YourName>/Vaulter AI/confidentials/
  Mac     : <project_root>/confidentials/

NEVER put real credentials directly in this file.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ─── Project Root ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# ─── Secrets Folder ───────────────────────────────────────────────
if sys.platform == "win32":
    SECRETS_DIR = Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / "Vaulter AI" / "confidentials"
else:
    SECRETS_DIR = BASE_DIR / "confidentials"

SECRETS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(SECRETS_DIR / ".env")

# ─── Data Folders ─────────────────────────────────────────────────

DATA_DIR       = (BASE_DIR / "data").resolve()
WATCH_DIR      = DATA_DIR / "watched_folder"
PROCESSED_DIR  = DATA_DIR / "processed"
CHROMA_DIR     = DATA_DIR / "chroma_db"
LOG_DIR        = DATA_DIR / "logs"
REGISTRY_FILE  = DATA_DIR / "ingested_registry.json"

CHROMA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── Chunking Settings ────────────────────────────────────────────

CHUNK_SIZE    = 800
CHUNK_OVERLAP = 100

CHUNK_TIERS = [
    (10,  500,  50),
    (50,  800,  100),
    (100, 1200, 150),
    (999, 1500, 200),
]

def get_chunk_settings(page_count: int) -> tuple[int, int]:
    for max_pages, chunk_size, overlap in CHUNK_TIERS:
        if page_count <= max_pages:
            return chunk_size, overlap
    return 1500, 200

# ─── OCR Settings ─────────────────────────────────────────────────

if sys.platform == "win32":
    TESSERACT_PATH = str(Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / r"Packages\Tesseract-OCR\tesseract.exe")
    POPPLER_PATH   = str(Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / r"Packages\poppler-26.02.0\Library\bin")
else:
    TESSERACT_PATH = "/opt/homebrew/bin/tesseract"
    POPPLER_PATH   = "/opt/homebrew/bin"

# ─── ChromaDB ─────────────────────────────────────────────────────

CHROMA_COLLECTION_NAME = "vaulter_documents"

# ─── Embedding ────────────────────────────────────────────────────

EMBEDDING_DIM  = 384

# ══════════════════════════════════════════════════════════════════
# Stage 2 — Web & Email Pipeline
# ══════════════════════════════════════════════════════════════════

RAW_WEB_DIR   = DATA_DIR / "raw_web"
RAW_EMAIL_DIR = DATA_DIR / "raw_email"

WEB_SOURCES = [
    {
        "name": "CBRE US Market Outlook 2026",
        "url": "https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026",
        "frequency_hours": 24,
        "tags": ["p", "h2", "h3"],
    },
    {
        "name": "CBRE Capital Markets 2026",
        "url": "https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026/capital-markets",
        "frequency_hours": 24,
        "tags": ["p", "h2", "h3"],
    },
    {
        "name": "Marcus & Millichap Research",
        "url": "https://www.marcusmillichap.com/research",
        "frequency_hours": 24,
        "tags": ["p", "h3"],
    },
    {
        "name": "GlobeSt CRE News",
        "url": "https://www.globest.com/sectors/",
        "frequency_hours": 12,
        "tags": ["article", "p", "h2", "h3"],
    },
    {
        "name": "GlobeSt Homepage",
        "url": "https://www.globest.com/",
        "frequency_hours": 12,
        "tags": ["article", "p", "h2", "h3"],
    },
]

SCHEDULER_TIMEZONE = "America/Phoenix"

# ─── Outlook / Microsoft Graph ────────────────────────────────────
# Add to confidentials/.env:
#   OUTLOOK_CLIENT_ID=your-application-id
#   OUTLOOK_TENANT_ID=your-directory-id
#   OUTLOOK_CLIENT_SECRET=your-client-secret

OUTLOOK_CLIENT_ID     = os.getenv("OUTLOOK_CLIENT_ID", "")
OUTLOOK_TENANT_ID     = os.getenv("OUTLOOK_TENANT_ID", "")
OUTLOOK_CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET", "")
OUTLOOK_TOKEN_FILE    = SECRETS_DIR / "outlook_token.json"
OUTLOOK_FOLDERS       = ["Inbox"]
OUTLOOK_SENDER_WHITELIST = []
OUTLOOK_LOOKBACK_DAYS = 30

# ─── Anthropic / Claude API ───────────────────────────────────────
# Add to confidentials/.env:
#   ANTHROPIC_API_KEY=sk-ant-...

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ══════════════════════════════════════════════════════════════════
# Stage 3 — MCP Server
# ══════════════════════════════════════════════════════════════════

# Secret key that Claude.ai must send with every MCP request.
# Set this in confidentials/.env:
#   MCP_API_KEY=vaulter_mcp_your_random_string_here
#
# Generate one with: python -c "import secrets; print(secrets.token_hex(24))"

MCP_API_KEY = os.getenv("MCP_API_KEY", "")
MCP_PORT    = int(os.getenv("MCP_PORT", "8765"))

# ─── Logging ──────────────────────────────────────────────────────

LOG_LEVEL = "INFO"
