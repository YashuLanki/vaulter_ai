"""
pipeline/outlook_auth.py
-------------------------
Vaulter AI Stage 2 — Outlook Authentication

Handles OAuth2 authentication with Microsoft Graph API.
Run via: python main.py auth

Uses MSAL PublicClientApplication with device code flow.
Token is cached in outlook_token.json automatically.

NEVER commit outlook_token.json to git.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    OUTLOOK_CLIENT_ID,
    OUTLOOK_TENANT_ID,
    OUTLOOK_TOKEN_FILE,
)

import msal

SCOPES = ["https://graph.microsoft.com/Mail.Read"]


def get_access_token() -> str:
    """
    Return a valid Microsoft Graph access token using device code flow.
    Caches the token in outlook_token.json for future runs.
    """
    if not OUTLOOK_CLIENT_ID:
        raise ValueError(
            "OUTLOOK_CLIENT_ID not set.\n"
            "Add it to your .env file:\n"
            "  OUTLOOK_CLIENT_ID=your-application-id\n"
        )

    # Use a serializable token cache so tokens persist between runs
    cache = msal.SerializableTokenCache()
    if OUTLOOK_TOKEN_FILE.exists():
        cache.deserialize(OUTLOOK_TOKEN_FILE.read_text())

    # PublicClientApplication — correct for device code flow
    app = msal.PublicClientApplication(
        client_id=OUTLOOK_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}",
        token_cache=cache,
    )

    # Try silent refresh first if we have cached accounts
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    # Launch device code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    print("\n" + "=" * 60)
    print("Open this URL in your browser and enter the code shown:")
    print(f"\n  {flow['verification_uri']}\n")
    print(f"  Code: {flow['user_code']}")
    print("=" * 60 + "\n")

    # Blocks until user approves in the browser
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(
            f"Auth failed: {result.get('error_description', result.get('error'))}"
        )

    _save_cache(cache)
    print(f"✓ Token saved to {OUTLOOK_TOKEN_FILE}")
    return result["access_token"]


def run_auth_flow() -> str:
    """
    Public entry point called by main.py auth command.
    Alias for get_access_token() — initiates device code flow,
    blocks until the user signs in, and saves the token to disk.
    """
    return get_access_token()


def _save_cache(cache: msal.SerializableTokenCache):
    """Save token cache to disk if it changed."""
    if cache.has_state_changed:
        OUTLOOK_TOKEN_FILE.write_text(cache.serialize())
