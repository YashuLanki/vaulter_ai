"""
analysis/analyzer.py
---------------------
Vaulter AI Stage 3 — Claude Analysis Engine
"""

# NOTE: This module is not currently used by the MCP server architecture.
# mcp_server.py returns raw context directly to Claude, which reasons over it.
# Keep this file if you plan to build a non-Claude-Desktop interface (e.g. a
# web dashboard or API) that needs Python to call Claude directly.
# To re-activate: import and call these functions from mcp_server.py or a new
# interface layer.
#


import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from analysis.rag_engine import (
    free_search,
    get_property_context,
    get_cross_property_context,
    get_recent_emails,
    get_recent_web_intelligence,
    format_context_for_claude,
)
from analysis.prompts import (
    SYSTEM_PROMPT,
    build_qa_prompt,
    build_property_summary_prompt,
    build_risk_scan_prompt,
    build_market_summary_prompt,
    build_email_highlights_prompt,
    build_portfolio_overview_prompt,
    build_dashboard_prompt,
)

log = logging.getLogger("vaulter.analyzer")


# ─── Claude Client ────────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY not set.\n"
            "Add it to your confidentials/.env file:\n"
            "  ANTHROPIC_API_KEY=sk-ant-...\n"
        )
    return anthropic.Anthropic(api_key=api_key)


def _call_claude(user_prompt: str, max_tokens: int = 1500) -> str:
    client = _get_client()
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text
    except anthropic.AuthenticationError:
        raise ValueError("Invalid Anthropic API key. Check ANTHROPIC_API_KEY in confidentials/.env")
    except anthropic.RateLimitError:
        raise RuntimeError("Rate limit hit — wait a moment and try again.")
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        raise


# ─── Intent Detection ─────────────────────────────────────────────

DASHBOARD_KEYWORDS = [
    "dashboard", "visualize", "visualization", "chart", "graph",
    "summary table", "display as chart", "show as chart", "show as graph",
    "plot", "bar chart", "pie chart",
]

PORTFOLIO_KEYWORDS = [
    "all properties", "every property", "list properties", "show properties",
    "show all properties", "list all properties", "which properties",
    "what properties", "how many properties",
    "properties by state", "properties in arizona", "properties in california",
    "properties in texas", "properties in new mexico", "properties in colorado",
    "all arizona", "all california", "all texas", "all new mexico", "all colorado",
    "properties by stage", "properties by category",
    "in rezone", "are rezone", "rezone properties", "rezone stage",
    "in acquisition", "are acquisition", "acquisition properties",
    "in disposition", "are disposition", "disposition properties",
    "in final engineering", "are final engineering", "final engineering properties",
    "in pre-plat", "are pre-plat", "pre-plat properties",
    "in site maintenance", "are site maintenance", "site maintenance properties",
    "in development", "are development", "development properties",
    "list rezone", "list acquisition", "list disposition",
    "list final engineering", "list pre-plat", "list site maintenance",
    "what stage", "current stage", "which stage",
    "full portfolio", "entire portfolio", "whole portfolio",
    "portfolio breakdown", "portfolio overview", "portfolio summary",
]


def wants_dashboard(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in DASHBOARD_KEYWORDS)


def wants_portfolio_list(question: str) -> bool:
    q = question.lower()

    if any(kw in q for kw in PORTFOLIO_KEYWORDS):
        return True

    stage_names = ["rezone", "acquisition", "disposition", "final engineering",
                   "pre-plat", "site maintenance", "development"]
    for stage in stage_names:
        if stage in q and any(w in q for w in ["list", "show", "which", "what", "all", "give"]):
            return True
        if f"{stage} propert" in q:
            return True

    return False


def get_portfolio_context(question: str = "") -> str:
    """
    Load the full property list from the Project Master.
    Filters by stage if the question mentions one.
    """
    try:
        from pipeline.property_scraper import load_properties
        props, _ = load_properties()

        q = question.lower()
        stage_map = {
            "rezone":            "Rezone",
            "acquisition":       "Acquisition",
            "disposition":       "Disposition",
            "final engineering": "Final Engineering",
            "pre-plat":          "Pre-Plat",
            "site maintenance":  "Site Maintenance",
            "development":       "Development",
        }
        target_stage = None
        for key, val in stage_map.items():
            if key in q:
                target_stage = val
                break

        if target_stage:
            filtered = [p for p in props if p.get("category", "").lower() == target_stage.lower()]
            if not filtered:
                return f"No active properties found in the {target_stage} stage."
            by_state: dict = {}
            for p in filtered:
                by_state.setdefault(p.get("state", "Unknown"), []).append(p)
            lines = [f"PROPERTIES IN {target_stage.upper()} STAGE — {len(filtered)} properties:\n"]
            for state in sorted(by_state):
                lines.append(f"{state} ({len(by_state[state])}):")
                for p in by_state[state]:
                    lines.append(f"  - {p['name']} | {p.get('city', '')}")
                lines.append("")
            return "\n".join(lines)

        by_state = {}
        for p in props:
            by_state.setdefault(p.get("state", "Unknown"), []).append(p)
        lines = [f"FULL VAULTER AI PORTFOLIO — {len(props)} active properties:\n"]
        for state in sorted(by_state):
            lines.append(f"{state} ({len(by_state[state])} properties):")
            for p in by_state[state]:
                lines.append(f"  - {p['name']} | {p.get('category', 'Unknown')} | {p.get('city', '')}")
            lines.append("")
        return "\n".join(lines)

    except Exception as e:
        log.warning(f"Could not load portfolio context: {e}")
        return ""


# ─── Public Analysis Functions ────────────────────────────────────

def answer_question(question: str) -> dict:
    log.info(f"Q&A: {question[:80]}...")

    if wants_portfolio_list(question):
        portfolio_ctx = get_portfolio_context(question)
        chunks  = free_search(question, n=8)
        db_ctx  = format_context_for_claude(chunks)
        context = (portfolio_ctx + "\n\n" + db_ctx) if portfolio_ctx else db_ctx
    else:
        chunks  = free_search(question, n=15)
        context = format_context_for_claude(chunks)

    if wants_dashboard(question):
        return generate_dashboard(question, context)

    prompt = build_qa_prompt(question, context)
    answer = _call_claude(prompt, max_tokens=1200)

    return {
        "answer":       answer,
        "is_dashboard": False,
        "dashboard":    None,
        "timestamp":    _now(),
    }


def generate_dashboard(question: str, context: str = None) -> dict:
    log.info(f"Dashboard: {question[:80]}...")

    if context is None:
        if wants_portfolio_list(question):
            portfolio_ctx = get_portfolio_context(question)
            chunks  = free_search(question, n=8)
            db_ctx  = format_context_for_claude(chunks)
            context = (portfolio_ctx + "\n\n" + db_ctx) if portfolio_ctx else db_ctx
        else:
            chunks  = free_search(question, n=20)
            context = format_context_for_claude(chunks)

    prompt = build_dashboard_prompt(question, context)
    raw    = _call_claude(prompt, max_tokens=2000)

    try:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        dashboard = json.loads(clean.strip())
    except Exception as e:
        log.warning(f"Dashboard JSON parse failed: {e}")
        return {
            "answer":       raw,
            "is_dashboard": False,
            "dashboard":    None,
            "timestamp":    _now(),
        }

    return {
        "answer":       dashboard.get("summary", ""),
        "is_dashboard": True,
        "dashboard":    dashboard,
        "timestamp":    _now(),
    }


def get_property_summary(property_name: str) -> dict:
    log.info(f"Property summary: {property_name}")
    chunks  = get_property_context(property_name, n=20)
    context = format_context_for_claude(chunks)
    prompt  = build_property_summary_prompt(property_name, context)
    summary = _call_claude(prompt, max_tokens=1500)
    return {"property": property_name, "summary": summary, "timestamp": _now()}


def get_risk_scan(state: str = None) -> dict:
    scope   = state if state else "Full Portfolio"
    query   = "zoning denial environmental flood easement title dispute permit delay market softening legal issue"
    chunks  = get_cross_property_context(query, state=state, n=18)
    context = format_context_for_claude(chunks)
    prompt  = build_risk_scan_prompt(context, scope=scope)
    risks   = _call_claude(prompt, max_tokens=1400)
    return {"scope": scope, "risks": risks, "timestamp": _now()}


def get_market_summary(state: str = None) -> dict:
    scope      = state if state else "All Markets"
    query      = "land market new homes permits builder activity pricing trends supply demand"
    chunks     = get_cross_property_context(query, state=state, n=15)
    web_chunks = get_recent_web_intelligence(n=8)
    all_chunks = _merge_unique(chunks, web_chunks)[:18]
    context    = format_context_for_claude(all_chunks)
    prompt     = build_market_summary_prompt(context, state=state)
    summary    = _call_claude(prompt, max_tokens=1000)
    return {"scope": scope, "summary": summary, "timestamp": _now()}


def get_email_highlights() -> dict:
    chunks     = get_recent_emails(n=15)
    context    = format_context_for_claude(chunks)
    prompt     = build_email_highlights_prompt(context)
    highlights = _call_claude(prompt, max_tokens=1000)
    return {"highlights": highlights, "timestamp": _now()}


def get_portfolio_overview(property_list: list[str]) -> dict:
    query    = "property status update recent activity zoning permit engineering"
    chunks   = get_cross_property_context(query, n=20)
    context  = format_context_for_claude(chunks)
    prompt   = build_portfolio_overview_prompt(context, property_list)
    raw      = _call_claude(prompt, max_tokens=2000)
    overview = _parse_portfolio_overview(raw)
    return {"overview": overview, "raw": raw, "timestamp": _now()}


# ─── Helpers ──────────────────────────────────────────────────────

def _merge_unique(list_a: list[dict], list_b: list[dict]) -> list[dict]:
    seen, merged = set(), []
    for chunk in list_a + list_b:
        key = chunk["text"][:80]
        if key not in seen:
            seen.add(key)
            merged.append(chunk)
    return merged


def _parse_portfolio_overview(raw: str) -> list[dict]:
    rows = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if "|" not in line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            rows.append({"property": parts[0], "status": parts[1], "summary": parts[2]})
        elif len(parts) == 2:
            rows.append({"property": parts[0], "status": "", "summary": parts[1]})
    return rows


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
