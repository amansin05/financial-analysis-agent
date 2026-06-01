# Financial Analysis Agent

An agent for earnings-call summaries, exact metric extraction, grounded Q&A, and
multi-company theme analysis. Fully-local, fully-free stack:

- **SQLite** — system of record (calls, segments, financials, analyses, graph edges)
- **ChromaDB** — vector search for Q&A
- **NetworkX** — multi-hop graph analysis, built from SQLite edges on demand

Two principles: **numbers from structured data (SEC XBRL), meaning from LLMs**; and
**classify before you process** (triage docs before paying for vision).

> Architecture diagrams (rendered + Mermaid source): **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

![Full structure with triage split](docs/img/connected_full_structure_with_triage_split.svg)

## Status: all phases complete ✅ (0–4)

- **Phase 0** — env, deps, SQLite schema, verified API connectivity.
- **Phase 1** — single-document summarizer. Transcript sources (Finnhub, API Ninjas)
  were paywalled/down, so we ship on the SEC **EDGAR 8-K** path (item 2.02 →
  Exhibit 99.1 press release) — no key, fully free. URL → segments → Groq summary.
- **Phase 2** — exact financials from **XBRL** (source='xbrl', the only trusted
  numbers), LLM-extracted figures **reconciled** against XBRL with mismatches
  flagged, sentiment scoring, and every claim grounded to a `citations` row.
- **Phase 3** — **ChromaDB** Q&A. Segments embedded locally (all-MiniLM-L6-v2),
  similarity search → authoritative text from SQLite → grounded Groq answer.
  Hybrid: numeric questions also draw on XBRL-verified financials (+ computed
  margins); the model admits when the context lacks the answer (no hallucination).
- **Phase 4** — **NetworkX** graph + multi-company monitoring. LLM-extracted
  topics/mentions/executives (grounded) populate SQLite edge tables; the graph is
  built from those edges on demand for theme-propagation, topic pervasiveness,
  centrality, shared-themes (multi-hop), and community detection. A monitor adds
  an earnings-calendar trigger, watchlist refresh, and grounded alerts.

## Layout

```
.
├── pyproject.toml                      # installable package (src layout) + pytest cfg
├── Makefile                            # common tasks (make help)
├── requirements.txt
├── .env / .env.example                 # secrets (gitignored) / template
├── README.md
├── .financial_analysis_agent_cache/    # SQLite + ChromaDB (gitignored, never committed)
├── dev-ui/
│   └── index.html                      # React SPA (served by the API)
├── docs/
│   ├── ARCHITECTURE.md                 # 7 diagrams (rendered + Mermaid source)
│   └── img/                            # rendered .svg/.png + .mmd sources
├── tests/                              # pytest (pure-logic unit tests)
└── src/financial_analysis_agent/
    ├── utils/
    │   ├── config.py                   # loads .env, exposes settings + ROOT
    │   ├── db.py                       # connect/init_db/list_tables
    │   ├── htmltext.py                 # stdlib HTML -> text
    │   └── schema.sql                  # SQLite DDL (idempotent, packaged)
    ├── services/                       # external API clients
    │   ├── edgar.py                    # 8-K earnings releases + XBRL + company search
    │   ├── finnhub.py                  # calendar, profiles
    │   ├── groq.py                     # LLM analysis layer
    │   └── apininjas.py                # transcripts (dormant)
    ├── pipelines/
    │   ├── ingest/                     # WRITE path
    │   │   ├── triage.py · filings.py · store.py   # classify · parse · upsert
    │   │   ├── summarize.py · xbrl.py · analyze.py  # summary · exact #s · reconcile
    │   │   ├── entities.py             # topics/mentions/execs -> edge tables
    │   │   └── pipeline.py             # run_full(ticker) / backfill — orchestrator
    │   └── retrieve/                   # READ path
    │       ├── vectorstore.py          # ChromaDB index + similarity query
    │       ├── qa.py                   # grounded Q&A (+ XBRL bridge, company auto-detect)
    │       └── graph.py                # NetworkX graph + multi-hop queries
    └── server/
        └── app.py                      # FastAPI (REST + serves the SPA)

scripts/   # thin CLIs over the package: init_db · check_connectivity · ingest_8k ·
           # ingest_call · analyze_financials · index_segments · ask · extract_entities ·
           # graph_query · monitor · backfill · serve
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"   # installs the package + deps + pytest
#   or:  make dev
```

Fill in `.env` (copy from `.env.example`):
- `FINNHUB_API_KEY` — earnings calendar + company profiles
- `GROQ_API_KEY` — LLM analysis (default model `llama-3.3-70b-versatile`)
- `EDGAR_USER_AGENT` — SEC requires a descriptive UA with contact info (no key)

## Run

The package is installed, so the CLIs run via `python -m scripts.<name>` (or use the
`make` targets — `make help` lists them). Launch the UI and open http://127.0.0.1:8000:

```powershell
.venv\Scripts\python.exe -m scripts.serve        # or:  make serve  /  faa-serve
```

```powershell
# Create / migrate the database (idempotent)
.venv\Scripts\python.exe -m scripts.init_db

# Verify every API + the DB (exits 0 only if all pass)
.venv\Scripts\python.exe -m scripts.check_connectivity

# Phase 1: ingest latest earnings 8-K press release -> segments + Groq summary
.venv\Scripts\python.exe -m scripts.ingest_8k AAPL

# Phase 2: XBRL truth + reconciled spoken figures + sentiment (run after Phase 1)
.venv\Scripts\python.exe -m scripts.analyze_financials AAPL

# Phase 3: embed segments into ChromaDB, then ask grounded questions
.venv\Scripts\python.exe -m scripts.index_segments            # all calls (or pass a ticker)
.venv\Scripts\python.exe -m scripts.ask "What were gross and operating margins?" --ticker MSFT
.venv\Scripts\python.exe -m scripts.ask "Which company returned more cash to shareholders?"

# Phase 4: extract graph entities, run multi-hop analysis, monitor a watchlist
.venv\Scripts\python.exe -m scripts.extract_entities                 # topics/execs -> edge tables
.venv\Scripts\python.exe -m scripts.graph_query                      # pervasiveness, centrality, communities
.venv\Scripts\python.exe -m scripts.graph_query --theme "artificial intelligence"
.venv\Scripts\python.exe -m scripts.graph_query --shared MSFT NVDA
.venv\Scripts\python.exe -m scripts.graph_query --theme "artificial intelligence"  # over time
.venv\Scripts\python.exe -m scripts.monitor AAPL MSFT NVDA --calendar 60   # +alerts
.venv\Scripts\python.exe -m scripts.monitor NVDA --refresh                 # re-run full pipeline

# Backfill history: ingest the last N earnings quarters per ticker (full pipeline)
.venv\Scripts\python.exe -m scripts.backfill AAPL MSFT NVDA --quarters 4
```

All ingest/analysis is idempotent — re-running a ticker replaces its rows rather
than duplicating them.

Expected:

```
[OK  ] SQLite   14 tables present
[OK  ] EDGAR    ticker_to_cik(AAPL) -> 0000320193
[OK  ] Finnhub  profile2(AAPL) -> Apple Inc
[OK  ] Groq     model=llama-3.3-70b-versatile -> 'pong'
All checks passed.
```

## Roadmap

| Phase | Goal | Active tools |
|-------|------|--------------|
| **0** ✅ | Setup: env, DB schema, API connectivity | SQLite |
| **1** ✅ | Single-document summarizer (EDGAR 8-K path) | SQLite |
| **2** ✅ | XBRL metric extraction + reconciliation + grounding | SQLite |
| **3** ✅ | Q&A chat (embed segments, retrieve, grounded answers) | SQLite + ChromaDB |
| **4** ✅ | Multi-company monitoring + NetworkX graph | SQLite + ChromaDB + NetworkX |

### Multi-quarter history
`scripts.backfill` ingests the last N earnings quarters per ticker so theme
propagation is visible *over time* (e.g. AI traced across MSFT + NVDA over four
quarters each). XBRL is **period-matched** to each historical quarter via the
report date.

### Known limitations carried forward
- **No speaker segmentation** on the 8-K path (press releases have no speakers).
  The transcript path (`scripts/ingest_call.py` + API Ninjas client) is built and
  unit-tested, ready to activate when a transcript source is available.
- **Fiscal labels are calendar-derived** from the report date (override with
  `--fy/--fq`). XBRL `period` (the fiscal-period end date) is authoritative.
- **Fiscal-Q4 has no standalone XBRL quarter** — the 10-K reports Q4 only inside
  the annual figure, so backfilled FY-end quarters show no XBRL (honest 0/0)
  rather than a stale prior quarter. (Computing Q4 = annual − 9-month YTD is a
  possible future enhancement.)
- **LLM table-reading is intentionally distrusted** — figures stated only in a
  filing's "(In millions)" tables can be mis-scaled by the LLM; reconciliation
  flags these and XBRL remains the source of truth (see NVDA gross_profit).
