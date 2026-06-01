# Financial Analysis Agent — common tasks
# Usage: make <target>   (uses the project venv at .venv)

PY := .venv/Scripts/python.exe        # Windows venv; on POSIX use .venv/bin/python
TICKERS ?= AAPL MSFT NVDA
QUARTERS ?= 4

.PHONY: help install dev test lint serve check ingest analyze index entities graph monitor backfill clean

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:        ## Install the package (editable) + runtime deps
	$(PY) -m pip install -e .

dev:            ## Install with dev extras (pytest)
	$(PY) -m pip install -e ".[dev]"

test:           ## Run the test suite
	$(PY) -m pytest

check:          ## Verify DB + all API connectivity
	$(PY) -m scripts.check_connectivity

serve:          ## Launch the UI (http://127.0.0.1:8000)
	$(PY) -m scripts.serve

ingest:         ## Ingest latest 8-K for one ticker: make ingest T=AAPL
	$(PY) -m scripts.ingest_8k $(T)

analyze:        ## Phase 2 analysis for one ticker: make analyze T=AAPL
	$(PY) -m scripts.analyze_financials $(T)

index:          ## Embed segments into ChromaDB
	$(PY) -m scripts.index_segments

entities:       ## Extract graph entities (topics/execs)
	$(PY) -m scripts.extract_entities

graph:          ## Graph analysis report
	$(PY) -m scripts.graph_query

monitor:        ## Monitor the watchlist: make monitor
	$(PY) -m scripts.monitor $(TICKERS)

backfill:       ## Backfill quarters: make backfill TICKERS="AAPL MSFT" QUARTERS=4
	$(PY) -m scripts.backfill $(TICKERS) --quarters $(QUARTERS)

clean:          ## Remove caches (keeps .financial_analysis_agent_cache data)
	$(PY) -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('*.egg-info')]"
