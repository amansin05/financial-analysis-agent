"""Launch the UI: FastAPI backend + React SPA at http://127.0.0.1:8000

Usage:
    python -m scripts.serve            # http://127.0.0.1:8000
    python -m scripts.serve --port 8080
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the Earnings Call Agent UI.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = ap.parse_args()

    print(f"Earnings Call Agent UI -> http://{args.host}:{args.port}")
    uvicorn.run("financial_analysis_agent.server.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
