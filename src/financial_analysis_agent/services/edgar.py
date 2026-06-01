"""SEC EDGAR client for exact XBRL financials.

No API key, but SEC requires a descriptive User-Agent with contact info and
asks for <=10 requests/second. We wrap the two endpoints Phase 2 needs:
ticker->CIK resolution and the companyconcept / companyfacts XBRL data.
"""
from __future__ import annotations

import requests

from financial_analysis_agent.utils import config

DATA_BASE = "https://data.sec.gov"
WWW_BASE = "https://www.sec.gov"


class EdgarClient:
    def __init__(self, user_agent: str | None = None, timeout: float = 20.0):
        self.user_agent = user_agent or config.EDGAR_USER_AGENT
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    def _get_json(self, url: str) -> dict:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, url: str) -> str:
        """Fetch a document as UTF-8 text (SEC serves UTF-8; force it so smart
        quotes / em-dashes / ® don't come back as replacement chars)."""
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text

    def _company_tickers(self) -> list[dict]:
        """The full SEC ticker<->CIK<->name table (cached per client instance)."""
        if getattr(self, "_ct_cache", None) is None:
            data = self._get_json(f"{WWW_BASE}/files/company_tickers.json")
            self._ct_cache = list(data.values())
        return self._ct_cache

    def ticker_to_cik(self, ticker: str) -> str | None:
        """Resolve a ticker to a zero-padded 10-digit CIK string."""
        ticker = ticker.upper()
        for row in self._company_tickers():
            if row.get("ticker", "").upper() == ticker:
                return str(row["cik_str"]).zfill(10)
        return None

    def search_company(self, query: str, limit: int = 8) -> list[dict]:
        """Resolve a ticker OR company name to candidate companies (any SEC filer).

        Exact ticker matches rank first, then name matches (prefix before substring).
        Returns [{ticker, cik, name}] -- works for any company in SEC EDGAR.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        exact, prefix, sub = [], [], []
        for row in self._company_tickers():
            tk = row.get("ticker", "")
            name = row.get("title", "")
            cand = {"ticker": tk, "cik": str(row["cik_str"]).zfill(10), "name": name}
            nl = name.lower()
            if tk.lower() == q:
                exact.append(cand)
            elif nl.startswith(q) or tk.lower().startswith(q):
                prefix.append(cand)
            elif q in nl:
                sub.append(cand)
        return (exact + prefix + sub)[:limit]

    def company_facts(self, cik: str) -> dict:
        """All XBRL facts for a company. cik may be raw or zero-padded."""
        cik10 = str(cik).zfill(10)
        return self._get_json(f"{DATA_BASE}/api/xbrl/companyfacts/CIK{cik10}.json")

    def company_concept(self, cik: str, taxonomy: str, tag: str) -> dict:
        """A single XBRL concept, e.g. us-gaap / RevenueFromContractWithCustomerExcludingAssessedTax."""
        cik10 = str(cik).zfill(10)
        return self._get_json(
            f"{DATA_BASE}/api/xbrl/companyconcept/CIK{cik10}/{taxonomy}/{tag}.json"
        )

    # ------------------- 8-K earnings releases (Phase 1 source) -------------------

    def recent_filings(self, cik: str) -> list[dict]:
        """Parse the submissions API into a flat list of recent filing dicts."""
        cik10 = str(cik).zfill(10)
        data = self._get_json(f"{DATA_BASE}/submissions/CIK{cik10}.json")
        rec = data.get("filings", {}).get("recent", {})
        cols = ("form", "accessionNumber", "primaryDocument", "filingDate",
                "reportDate", "items", "primaryDocDescription")
        n = len(rec.get("form", []))
        out = []
        for i in range(n):
            out.append({k: (rec.get(k) or [None] * n)[i] for k in cols})
        return out

    def earnings_8ks(self, cik: str, limit: int | None = None) -> list[dict]:
        """All (or the N most recent) 8-Ks whose items include 2.02, newest first."""
        out = [f for f in self.recent_filings(cik)
               if f["form"] == "8-K" and "2.02" in (f.get("items") or "")]
        return out[:limit] if limit else out

    def latest_earnings_8k(self, cik: str) -> dict | None:
        """Most recent 8-K whose items include 2.02 (Results of Operations)."""
        found = self.earnings_8ks(cik, limit=1)
        return found[0] if found else None

    def filing_items(self, cik: str, accession: str) -> list[dict]:
        """List files (name + size) in a filing's folder via its index.json."""
        cik_int = str(int(cik))  # folder path uses the un-padded CIK
        acc_nodash = accession.replace("-", "")
        url = f"{WWW_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
        data = self._get_json(url)
        return [
            {"name": it["name"], "size": int(it.get("size") or 0)}
            for it in data.get("directory", {}).get("item", [])
        ]

    @staticmethod
    def _pick_press_release(items: list[dict], primary_doc: str | None) -> str | None:
        """Choose the EX-99.1 press-release .htm from a filing's file list.

        Filers name exhibits inconsistently (AAPL: a8-kex991..., MSFT: msft-ex99_1,
        NVDA: q1fy27pr). So we score each candidate .htm on multiple filename
        signals AND its byte size -- the real release is the largest content doc,
        never the tiny 8-K cover -- and demote secondary docs like CFO commentary.
        """
        primary = (primary_doc or "").lower()
        candidates = []
        for it in items:
            low = it["name"].lower()
            if not low.endswith((".htm", ".html")):
                continue
            if low == primary:                       # the 8-K cover itself
                continue
            if "index" in low:                       # index / header pages
                continue
            stem = low.rsplit(".", 1)[0]
            if stem.startswith("r") and stem[1:].isdigit():  # R1.htm XBRL viewer
                continue
            candidates.append(it)

        if not candidates:
            return None

        def score(it: dict) -> tuple[int, int]:
            low = it["name"].lower()
            stem = low.rsplit(".", 1)[0]
            s = 0
            if any(k in low for k in ("ex99", "ex-99", "ex_99", "991")):
                s += 100                              # explicit exhibit-99 naming
            if any(k in low for k in ("press", "release", "earnings")):
                s += 60
            if stem.endswith("pr") or "-pr" in low or "_pr" in low:
                s += 40                               # NVDA-style 'pr' suffix
            if "commentary" in low or "cfo" in low or "script" in low:
                s -= 50                               # secondary docs
            return (s, it["size"])                    # size breaks ties

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        # If nothing scored positive, only accept it if it's clearly substantial
        # (a real release is big); otherwise signal "not found" so we fall back.
        if score(best)[0] <= 0 and best["size"] < 6000:
            return None
        return best["name"]

    def release_from_filing(self, ticker: str, cik: str, filing: dict) -> dict | None:
        """Fetch the EX-99.1 press-release text + metadata for a specific 8-K filing."""
        acc = filing["accessionNumber"]
        items = self.filing_items(cik, acc)
        exhibit = self._pick_press_release(items, filing.get("primaryDocument"))
        # Fall back to the filing's primary document if no EX-99 file is found.
        doc = exhibit or filing.get("primaryDocument")
        if not doc:
            return None
        cik_int = str(int(cik))
        acc_nodash = acc.replace("-", "")
        url = f"{WWW_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
        html = self._get_text(url)
        return {
            "ticker": ticker.upper(),
            "cik": cik,
            "accession": acc,
            "filing_date": filing.get("filingDate"),
            "report_date": filing.get("reportDate"),
            "exhibit": doc,
            "is_exhibit_99": exhibit is not None,
            "url": url,
            "html": html,
        }

    def earnings_release(self, ticker: str) -> dict | None:
        """Convenience: ticker -> latest 8-K item 2.02 -> EX-99.1 text + metadata."""
        cik = self.ticker_to_cik(ticker)
        if not cik:
            return None
        filing = self.latest_earnings_8k(cik)
        if not filing:
            return None
        return self.release_from_filing(ticker, cik, filing)

    def earnings_releases(self, ticker: str, limit: int = 4) -> list[dict]:
        """The N most recent earnings press releases (newest first) for a ticker."""
        cik = self.ticker_to_cik(ticker)
        if not cik:
            return []
        out = []
        for filing in self.earnings_8ks(cik, limit=limit):
            rel = self.release_from_filing(ticker, cik, filing)
            if rel:
                out.append(rel)
        return out

    def ping(self) -> tuple[bool, str]:
        """Connectivity check: resolve a known ticker to its CIK."""
        try:
            cik = self.ticker_to_cik("AAPL")
            if cik:
                return True, f"ticker_to_cik(AAPL) -> {cik}"
            return False, "AAPL not found in company_tickers.json"
        except requests.HTTPError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:120]}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
