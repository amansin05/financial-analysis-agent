-- ============================================================
-- Earnings Call Agent — SQLite schema (system of record)
-- Safe to run repeatedly: every object uses IF NOT EXISTS.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ---------- Core hierarchy ----------

CREATE TABLE IF NOT EXISTS companies (
    id      INTEGER PRIMARY KEY,
    ticker  TEXT,
    cik     TEXT UNIQUE,
    name    TEXT,
    sector  TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fiscal_year     INTEGER,
    fiscal_quarter  INTEGER,
    call_date       TEXT,
    source          TEXT,
    UNIQUE(company_id, fiscal_year, fiscal_quarter)   -- enables safe upserts
);

CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY,
    call_id       INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    seq           INTEGER,
    speaker_name  TEXT,
    speaker_role  TEXT,            -- CEO | CFO | Analyst | Operator | ...
    section       TEXT,            -- 'prepared' | 'qa'
    text          TEXT
);

CREATE TABLE IF NOT EXISTS financials (
    id       INTEGER PRIMARY KEY,
    call_id  INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    metric   TEXT,
    value    REAL,
    unit     TEXT,
    period   TEXT,
    source   TEXT                  -- 'xbrl' for verified, 'spoken' for LLM-extracted
);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY,
    call_id         INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    kind            TEXT,          -- summary | sentiment | qa_answer | chart_summary
    content         TEXT,
    model           TEXT,
    prompt_version  TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS citations (
    id           INTEGER PRIMARY KEY,
    analysis_id  INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    segment_id   INTEGER REFERENCES segments(id) ON DELETE SET NULL,
    quote        TEXT
);

-- ---------- Entities + edges (Phase 4) ----------

CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS firms (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS analysts (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT,
    firm_id    INTEGER REFERENCES firms(id) ON DELETE SET NULL,
    UNIQUE(full_name, firm_id)
);

CREATE TABLE IF NOT EXISTS topics (
    id     INTEGER PRIMARY KEY,
    label  TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS analyst_coverage (
    id          INTEGER PRIMARY KEY,
    analyst_id  INTEGER NOT NULL REFERENCES analysts(id) ON DELETE CASCADE,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    first_seen  TEXT,
    last_seen   TEXT,
    UNIQUE(analyst_id, company_id)
);

CREATE TABLE IF NOT EXISTS executive_tenure (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role        TEXT,
    start_date  TEXT,
    end_date    TEXT,
    UNIQUE(person_id, company_id, role)
);

CREATE TABLE IF NOT EXISTS mentions (
    id                 INTEGER PRIMARY KEY,
    call_id            INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    segment_id         INTEGER REFERENCES segments(id) ON DELETE SET NULL,
    target_type        TEXT,     -- company | person | topic
    target_company_id  INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    target_person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    target_topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    target_text        TEXT,
    sentiment          REAL
);

CREATE TABLE IF NOT EXISTS competitor_links (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    competitor_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    strength       REAL,
    UNIQUE(company_id, competitor_id)
);

-- ---------- Indexes for the hot join paths ----------

CREATE INDEX IF NOT EXISTS idx_calls_company       ON calls(company_id);
CREATE INDEX IF NOT EXISTS idx_segments_call       ON segments(call_id);
CREATE INDEX IF NOT EXISTS idx_segments_role       ON segments(speaker_role);
CREATE INDEX IF NOT EXISTS idx_financials_call     ON financials(call_id);
CREATE INDEX IF NOT EXISTS idx_analyses_call       ON analyses(call_id);
CREATE INDEX IF NOT EXISTS idx_citations_analysis  ON citations(analysis_id);
CREATE INDEX IF NOT EXISTS idx_mentions_call       ON mentions(call_id);
CREATE INDEX IF NOT EXISTS idx_coverage_company    ON analyst_coverage(company_id);
