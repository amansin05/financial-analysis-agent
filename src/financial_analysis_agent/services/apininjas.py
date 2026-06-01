"""API Ninjas client for earnings-call transcripts (free tier).

Endpoint: GET /v1/earningstranscript?ticker=&year=&quarter=
Auth:     X-Api-Key header.

The response includes:
  - transcript        : the full call as one string
  - transcript_split  : list of {speaker, text} dicts (pre-segmented) -- we
                        use this so Phase 1 needs no speaker-parsing of its own.
"""
from __future__ import annotations

import requests

from financial_analysis_agent.utils import config

BASE = "https://api.api-ninjas.com/v1"


class APINinjasClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or config.require("API_NINJAS_KEY")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.api_key})

    def _get(self, path: str, **params) -> dict | list:
        resp = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def earnings_transcript(self, ticker: str, year: int, quarter: int) -> dict:
        """Fetch one call transcript. Returns {} if none exists for that period."""
        data = self._get(
            "/earningstranscript", ticker=ticker, year=year, quarter=quarter
        )
        # API returns [] / "" when there's no transcript for the period.
        if not data:
            return {}
        return data if isinstance(data, dict) else {}

    def ping(self) -> tuple[bool, str]:
        """Connectivity check: pull a known historical transcript."""
        try:
            data = self.earnings_transcript("MSFT", 2023, 1)
            split = data.get("transcript_split") or []
            if split:
                return True, f"MSFT 2023Q1 -> {len(split)} speaker segments"
            if data.get("transcript"):
                return True, "MSFT 2023Q1 -> transcript (no split provided)"
            return False, "empty transcript (key valid but no data returned)"
        except requests.HTTPError as e:
            code = e.response.status_code
            hint = " (bad/missing key)" if code in (401, 403) else ""
            return False, f"HTTP {code}{hint}: {e.response.text[:120]}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
