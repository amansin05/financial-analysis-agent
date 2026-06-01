"""Central config: loads .env once and exposes typed settings.

Every other module imports from here so we never read os.environ ad hoc.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the src/ package.
ROOT = Path(__file__).resolve().parents[3]

# Load .env from the project root (no-op if already loaded / missing).
load_dotenv(ROOT / ".env")


def _get(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required env var {name!r}. Add it to {ROOT / '.env'}."
        )
    return val


# --- API credentials ---
FINNHUB_API_KEY = _get("FINNHUB_API_KEY")
FINNHUB_WEBHOOK_SECRET = _get("FINNHUB_WEBHOOK_SECRET")
API_NINJAS_KEY = _get("API_NINJAS_KEY")
GROQ_API_KEY = _get("GROQ_API_KEY")
GROQ_MODEL = _get("GROQ_MODEL", "llama-3.3-70b-versatile")
EDGAR_USER_AGENT = _get(
    "EDGAR_USER_AGENT",
    "Earnings Call Agent (learning project) contact@example.com",
)

# --- Local paths (resolved relative to project root) ---
DB_PATH = ROOT / _get("DB_PATH", ".financial_analysis_agent_cache/earnings.db")
CHROMA_PATH = ROOT / _get("CHROMA_PATH", ".financial_analysis_agent_cache/chroma")


def require(name: str) -> str:
    """Fetch a setting by attribute name, raising if it's empty.

    Usage: require("FINNHUB_API_KEY")
    """
    val = globals().get(name)
    if not val:
        raise RuntimeError(f"Config {name!r} is not set. Check your .env.")
    return str(val)
