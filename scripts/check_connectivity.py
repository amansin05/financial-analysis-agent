"""Phase 0 'hello world': verify each API responds and the DB exists.

Usage:  python -m scripts.check_connectivity

Exits 0 only if every check passes, so it doubles as a CI smoke test.
"""
from __future__ import annotations

import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.services.apininjas import APINinjasClient
from financial_analysis_agent.services.edgar import EdgarClient
from financial_analysis_agent.services.finnhub import FinnhubClient
from financial_analysis_agent.services.groq import GroqClient


def check_db() -> tuple[bool, str]:
    try:
        db.init_db()
        with db.connect() as conn:
            tables = db.list_tables(conn)
        expected = {"companies", "calls", "segments", "financials", "analyses"}
        missing = expected - set(tables)
        if missing:
            return False, f"missing tables: {', '.join(sorted(missing))}"
        return True, f"{len(tables)} tables present"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    checks = [
        ("SQLite ", check_db),
        ("EDGAR  ", lambda: EdgarClient().ping()),
        ("Finnhub", lambda: FinnhubClient().ping()),
        ("APININja", lambda: APINinjasClient().ping()),
        ("Groq   ", lambda: GroqClient().ping()),
    ]

    print("Phase 0 connectivity check")
    print("=" * 48)
    all_ok = True
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001  (e.g. missing key)
            ok, detail = False, f"{type(e).__name__}: {e}"
        all_ok = all_ok and ok
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name}  {detail}")

    print("=" * 48)
    print("All checks passed." if all_ok else "Some checks failed (see above).")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
