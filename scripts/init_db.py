"""Create the SQLite database from schema.sql and report the tables.

Usage:  python -m scripts.init_db
Idempotent: safe to run repeatedly.
"""
from __future__ import annotations

from financial_analysis_agent.utils import db


def main() -> None:
    path = db.init_db()
    with db.connect() as conn:
        tables = db.list_tables(conn)
    print(f"Database ready at: {path}")
    print(f"Tables ({len(tables)}): {', '.join(tables)}")


if __name__ == "__main__":
    main()
