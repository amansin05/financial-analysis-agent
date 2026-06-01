# Earnings Call Agent — Final Execution Plan

A phased build plan for a personal/learning earnings-call agent that does summaries,
metric extraction, Q&A chat, and multi-company monitoring. Built on a fully-local,
fully-free stack: **SQLite** (system of record), **ChromaDB** (vector search),
**NetworkX** (graph analysis).

---

## 1. The two principles everything follows

1. **Numbers from structured data, meaning from language models.**
   Exact figures (revenue, EPS, guidance) come from SEC **XBRL** — deterministic,
   free, never hallucinated. The LLM is reserved for interpretation (summaries,
   sentiment, Q&A), never for reading precise numbers off text or charts.

2. **Classify before you process.**
   A triage step inspects each document and routes it, so you never pay to run a
   vision model on something that doesn't need it. (Proven on a real Netflix 10-K:
   122 pages, clean text layer, zero real charts — the "vector-heavy" pages were
   just tables. Vision would have cost ~5x more for worse accuracy.)

---

## 2. The role of each chosen tool

| Tool | Role | Nature |
|------|------|--------|
| **SQLite** | System of record: all transcripts, financials, analyses, and graph *edges* | One file on disk, durable |
| **ChromaDB** | Vector search over segment text for Q&A and cross-call retrieval | Local persistent store |
| **NetworkX** | Multi-hop graph analysis (who-covers-whom, theme propagation, centrality) | In-memory, **built from SQLite edges on demand** |

Key relationship: **SQLite is the source of truth.** ChromaDB holds embeddings that
point *back* to SQLite rows (via `segment_id` in metadata). NetworkX is rebuilt from
SQLite's edge tables whenever you need graph analysis — it stores nothing permanently.

---

## 3. Full tech stack

- **Language / glue:** Python, `requests`, direct API calls (no LangChain).
- **PDF triage + text:** PyMuPDF (`fitz`) for inventory, text, rasterizing.
- **Tables:** pdfplumber.
- **Exact financials:** SEC EDGAR XBRL API (free; requires a `User-Agent` header).
- **Transcripts:** Finnhub `earnings-call-transcripts` endpoint, authenticated with
  your Finnhub API key. Use Finnhub's free tier for the surrounding data too (earnings
  calendar, company profiles). Fallbacks if the transcript endpoint is paywalled on
  your tier: API Ninjas (free tier includes transcripts), AlphaStreet (free, S&P 500),
  or self-transcribe public call audio with Whisper ($0, unlimited — see Phase 1).
- **LLM (analysis):** Claude or GPT for quality; Groq (Llama 4 Scout) for cheap/fast,
  including the only vision use.
- **Embeddings:** sentence-transformers (`all-MiniLM-L6-v2`) — local and free, the
  Chroma default. (Swap to an API embedder later if you want.)
- **Relational store:** SQLite.
- **Vector store:** ChromaDB (persistent client).
- **Graph:** NetworkX.
- **Scheduling:** a manual script first, then APScheduler or cron.

---

## 4. Data model (SQLite)

```sql
-- Core hierarchy
companies(id, ticker, cik UNIQUE, name, sector)
calls(id, company_id, fiscal_year, fiscal_quarter, call_date, source,
      UNIQUE(company_id, fiscal_year, fiscal_quarter))   -- safe upserts
segments(id, call_id, seq, speaker_name, speaker_role, section, text)
         -- speaker_role: CEO/CFO/Analyst/Operator ; section: 'prepared'|'qa'
financials(id, call_id, metric, value, unit, period, source)  -- source='xbrl' for verified
analyses(id, call_id, kind, content, model, prompt_version, created_at)
         -- kind: summary|sentiment|qa_answer|chart_summary
citations(id, analysis_id, segment_id, quote)            -- grounding link

-- Entities + edges (Phase 4)
people(id, full_name UNIQUE)
firms(id, name UNIQUE)
analysts(id, full_name, firm_id, UNIQUE(full_name, firm_id))
topics(id, label UNIQUE)
analyst_coverage(id, analyst_id, company_id, first_seen, last_seen,
                 UNIQUE(analyst_id, company_id))
executive_tenure(id, person_id, company_id, role, start_date, end_date,
                 UNIQUE(person_id, company_id, role))
mentions(id, call_id, segment_id, target_type, target_company_id,
         target_person_id, target_topic_id, target_text, sentiment)
competitor_links(id, company_id, competitor_id, strength,
                 UNIQUE(company_id, competitor_id))
```

---

## 5. The phases

Each phase is shippable on its own and reuses the previous one. Tools activate
progressively — you don't touch ChromaDB or NetworkX until you need them.

### Phase 0 — Setup
- Create the Python env and install: `requests pymupdf pdfplumber chromadb networkx
  sentence-transformers` + your LLM SDK (`groq` / `openai` / `anthropic`).
- Create the SQLite database with the schema above.
- Get keys: your **Finnhub API key** (transcripts + calendar + company data), a Groq
  key (free). EDGAR needs no key, just a descriptive `User-Agent`.
  - Store the key in an environment variable (`FINNHUB_API_KEY`), never hardcoded. Pass
    it via the `X-Finnhub-Token` header (or the `token` query param). List a company's
    available calls via the transcript-list endpoint, then pull one by its transcript
    ID.
  - **Note:** Finnhub's free tier is comfortable for the calendar and company data; the
    transcript endpoint itself may require a paid plan on some tiers. If it returns
    403/empty, switch the transcript source to a fallback (API Ninjas free tier, or the
    Whisper audio path) — the rest of the pipeline is unaffected. Watch the per-minute
    rate limit; space requests or cache.
- **Active tools:** SQLite.
- **Done when:** the DB file exists with all tables, and a "hello world" call to each
  API succeeds.

### Phase 1 — Single-call summarizer (text only)
- Fetch one transcript from **Finnhub** (list the company's calls, then pull one by its
  transcript ID) or a filing.
- **Zero-cost fallback (if the transcript endpoint is paywalled or you want unlimited
  coverage):** download the public earnings-call audio webcast and transcribe it
  locally with open-source **Whisper**. You handle speaker labeling yourself, but it's
  $0 and has no limits.
- Run the **triage** (PyMuPDF inventory: text layer? images? vector density?) and
  confirm the route.
- Filing path: extract text + tables.
- Put the whole transcript in the LLM context (it fits — no retrieval needed yet) and
  produce a structured summary into `analyses`.
- Populate `companies`, `calls`, `segments`.
- **Active tools:** SQLite.
- **Done when:** one call goes URL → stored summary with speaker-segmented text.

### Phase 2 — Metric extraction + grounding
- Fetch exact figures from **EDGAR XBRL**; store in `financials` with `source='xbrl'`.
- Have the LLM extract any spoken metrics, then **reconcile each against XBRL** and
  flag mismatches — never store an unverified number as truth.
- Add sentiment/tone analysis as a `kind='sentiment'` row.
- Stamp every analysis with `model` + `prompt_version`.
- **Active tools:** SQLite.
- **Done when:** numbers in `financials` are XBRL-verified and every claim links to a
  `citations` row.

### Phase 3 — Q&A chat (ChromaDB enters)
- Embed each segment with sentence-transformers and add to a **ChromaDB** collection,
  storing `segment_id`, `call_id`, `company` in the metadata.
- Q&A flow: embed the question → Chroma similarity search → fetch the matching
  `segments` from SQLite → answer with citations.
- (For a single call you can still stuff context; Chroma is what makes it scale across
  many calls.)
- Optional: give the LLM tools (function calling) — fetch a figure, compare a quarter.
- **Active tools:** SQLite + ChromaDB.
- **Done when:** "what did the CFO say about margins?" returns a grounded answer with
  the exact source segments.

### Phase 4 — Multi-company monitoring + NetworkX graph
- Add an earnings calendar trigger; ingest across many companies on a schedule.
- Populate edge tables in SQLite:
  - `analyst_coverage` — **free**, derived directly from `segments` where
    `speaker_role='Analyst'`.
  - `mentions`, `competitor_links`, `executive_tenure` — via LLM extraction, each
    grounded to a `segment_id`.
- **Build the NetworkX graph from SQLite edges** when you need analysis: load nodes
  (companies, analysts, people, topics) and edges, then run multi-hop queries,
  centrality, community detection, and theme-propagation traversals that are awkward
  as SQL.
- Add alerting (e.g., guidance cut, a watched phrase appears).
- **Active tools:** SQLite + ChromaDB + NetworkX (all three).
- **Done when:** you can answer a multi-hop question (e.g., "trace how 'AI capex'
  spread across companies quarter by quarter") that plain SQL can't.

---

## 6. How the three stores work together

```
                 ┌─────────────────────────────┐
   ingest  ───►  │   SQLite  (system of record) │
                 │  calls · segments · financials│
                 │  analyses · citations · edges │
                 └───────┬─────────────┬─────────┘
                         │             │
        embed segments   │             │  load edges on demand
                         ▼             ▼
                  ┌────────────┐  ┌──────────────┐
                  │  ChromaDB  │  │   NetworkX    │
                  │  vectors   │  │ in-memory     │
                  │ (Q&A/search│  │ graph         │
                  │  retrieval)│  │ (multi-hop    │
                  │            │  │  analysis)    │
                  └─────┬──────┘  └──────┬────────┘
                        │                │
        segment_id in metadata    results can be written
        ► joins back to SQLite     back to SQLite as needed
```

- **SQLite ↔ ChromaDB:** Chroma stores only vectors + metadata. The `segment_id` in
  metadata is the join key back to the full text and context in SQLite.
- **SQLite → NetworkX:** the graph is *derived*. You query the edge tables, build a
  `networkx.DiGraph`, run algorithms, and discard it (or cache results). Nothing
  graph-shaped is stored permanently outside SQLite.

---

## 7. Cost discipline (the biggest lever)

The cheapest optimization isn't a cheaper model — it's **not calling vision when the
document doesn't need it.** The triage step (Phase 1) is what enforces this. For
filings (most documents), the entire pipeline runs on free, local, deterministic tools:
PyMuPDF + pdfplumber + EDGAR XBRL + SQLite + local embeddings. The LLM is paid for only
where it adds real value — qualitative summary, sentiment, and Q&A.

---

## 8. Suggested build order, in one line

Phase 0 (setup) → Phase 1 (text summarizer, SQLite) → Phase 2 (XBRL + grounding) →
Phase 3 (ChromaDB Q&A) → Phase 4 (multi-company + NetworkX graph). Ship each before
starting the next.
