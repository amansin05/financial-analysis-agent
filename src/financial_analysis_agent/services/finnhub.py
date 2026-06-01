"""Thin Finnhub client.

Auth via the X-Finnhub-Token header. We only wrap the few endpoints the
agent needs; everything returns parsed JSON and raises on HTTP errors.

Note: the transcript endpoint may require a paid tier. `ping()` uses the
free company-profile endpoint so connectivity checks don't depend on it.
"""
from __future__ import annotations

import requests

from financial_analysis_agent.utils import config

BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, api_key: str | None = None, timeout: float = 15.0):
        self.api_key = api_key or config.require("FINNHUB_API_KEY")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Finnhub-Token": self.api_key})

    def _get(self, path: str, **params) -> dict | list:
        resp = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # --- free-tier endpoints ---
    def company_profile(self, symbol: str) -> dict:
        """Free tier. Used as the connectivity ping."""
        return self._get("/stock/profile2", symbol=symbol)

    def earnings_calendar(self, _from: str, to: str) -> dict:
        """Upcoming/historical earnings dates. Phase 4 trigger source."""
        return self._get("/calendar/earnings", **{"from": _from, "to": to})

    # --- may require a paid tier ---
    def transcripts_list(self, symbol: str) -> dict:
        """List available transcript IDs for a symbol."""
        return self._get("/stock/transcripts/list", symbol=symbol)

    def transcript(self, transcript_id: str) -> dict:
        """Pull one transcript by its id."""
        return self._get("/stock/transcripts", id=transcript_id)

    def ping(self) -> tuple[bool, str]:
        """Connectivity check via a free endpoint. Returns (ok, detail)."""
        try:
            data = self.company_profile("AAPL")
            if data and data.get("name"):
                return True, f"profile2(AAPL) -> {data['name']}"
            return False, "empty profile response (key may lack access)"
        except requests.HTTPError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:120]}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
