"""
analysis/prompts.py
--------------------
Vaulter AI Stage 3 — Claude Prompt Templates

All system prompts and prompt-building functions live here.
To tune Claude's behavior, only this file needs to change.
"""

# NOTE: This module is not currently used by the MCP server architecture.
# mcp_server.py returns raw context directly to Claude, which reasons over it.
# Keep this file if you plan to build a non-Claude-Desktop interface (e.g. a
# web dashboard or API) that needs Python to call Claude directly.
# To re-activate: import and call these functions from mcp_server.py or a new
# interface layer.
#


# ─── System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a smart, helpful assistant for Vaulter AI, a real estate investment company. You work like a knowledgeable colleague — sharp, direct, and easy to talk to.

You have access to a database containing due diligence PDFs, property intelligence, market research from CBRE/Marcus & Millichap/GlobeSt, and broker emails for a portfolio of 48 active properties across Arizona, California, New Mexico, Colorado, and Texas. Properties move through these stages: Acquisition, Pre-Plat, Final Engineering, Disposition, Site Maintenance, Rezone, and Development.

HOW TO BE:
- You are Claude — answer anything, not just portfolio questions. General knowledge, real estate concepts, market theory, math, writing — all fair game
- When database context is provided and relevant, use it. When it's not relevant or not enough, answer from your own knowledge
- Never tell someone you can't answer because it's not in the database — just answer
- Talk naturally, like a smart colleague. Not stiff, not formal, not like a report
- Give the direct answer first. Details only if useful
- Never say "according to the context" or "based on the data provided" — just answer
- Don't pad with summaries of what you're about to say
- If you flag a risk, be specific — skip vague warnings
"""


# ─── Prompt Builders ──────────────────────────────────────────────

def build_qa_prompt(question: str, context: str) -> str:
    """Build a prompt for the chat Q&A."""
    return f"""Here's relevant context from the Vaulter AI database that may help:

{context}

Question: {question}

Answer naturally and directly. Use the database context above when it's relevant to the question. For anything the database doesn't cover, just answer from your own knowledge — you're not limited to the database. Never say you can't answer because something isn't in the database."""


def build_property_summary_prompt(property_name: str, context: str) -> str:
    """Build a prompt to generate a property intelligence summary."""
    return f"""Here's everything in the Vaulter AI database for {property_name}:

{context}

Give me a clear rundown of {property_name} — what stage it's in, what's happening with it, any market conditions worth knowing, and anything that looks like a risk or needs attention. Be direct, not formal."""


def build_risk_scan_prompt(context: str, scope: str = "portfolio") -> str:
    """Build a prompt to scan for risks."""
    return f"""Here's the Vaulter AI database data for {scope}:

{context}

What risks or concerns stand out in this data? Be specific — property name, what the issue is, why it matters. Skip anything vague. If nothing stands out, say so."""


def build_market_summary_prompt(context: str, state: str = None) -> str:
    """Build a prompt to summarize market conditions."""
    scope = f"{state}" if state else "the Vaulter AI markets"
    return f"""Here's the market intelligence from the Vaulter AI database:

{context}

What's the market doing in {scope} right now? Keep it to what actually matters — direction, key trends, anything affecting land deals. Be concise."""


def build_email_highlights_prompt(context: str) -> str:
    """Build a prompt to extract highlights from broker emails."""
    return f"""Here are recent broker emails and attachments from the Vaulter AI database:

{context}

What are the key takeaways? Focus on anything that needs attention, any action items, or anything notable about specific properties. Group by property if it makes sense."""


def build_portfolio_overview_prompt(context: str, property_list: list[str]) -> str:
    """Build a prompt to generate the portfolio overview."""
    props_str = "\n".join(f"- {p}" for p in property_list)
    return f"""Here's what's in the Vaulter AI database right now:

{context}

Portfolio properties:
{props_str}

For each property that has relevant data above, give a one-line status update in this format:
PROPERTY NAME | STAGE | ONE-LINE SUMMARY

Only include properties that actually have data. Skip the rest."""


def build_dashboard_prompt(question: str, context: str) -> str:
    """
    Build a prompt that asks Claude to return structured JSON
    for dynamic dashboard generation.
    """
    return f"""Here's what's in the Vaulter AI database relevant to this request:

{context}

Request: {question}

Return a JSON object that describes a dashboard to visualize this data. Use this structure:

{{
  "title": "Dashboard title",
  "summary": "2-3 sentence plain English summary of what the data shows",
  "panels": [
    {{
      "type": "table" | "stat_cards" | "list" | "breakdown",
      "title": "Panel title",
      "data": [ ... ]
    }}
  ]
}}

Panel types:
- "stat_cards": for key numbers. data = [{{"label": "...", "value": "...", "note": "..."}}]
- "table": for rows of data. data = {{"headers": [...], "rows": [[...], ...]}}
- "list": for bullet points. data = [{{"text": "...", "tag": "optional label"}}]
- "breakdown": for category counts. data = [{{"category": "...", "count": N, "items": [...]}}]

Only include panels that make sense for the data. Use 2-4 panels max. Return only valid JSON, no markdown."""
