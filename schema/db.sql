-- California family-law corpus. SQLite dialect throughout: the same file is the
-- build artifact and the Cloudflare D1 import (D1 is SQLite).
--
-- Authority hierarchy (rules.authority_level), lower binds higher:
--   10 CA statute · 20 CA Rules of Court · 30 Judicial Council form
--   40 county local rule · 50 county standing/general order · 60 county local form
--   70 court admin procedure · 80 Family Court Services procedure · 90 self-help guidance
-- Encoded as an integer so retrieval can rank, and so a county rule can never be
-- presented as outranking a statute. See schema/topics.json for the full list.

PRAGMA foreign_keys = ON;

CREATE TABLE counties (
  id                         TEXT PRIMARY KEY,   -- slug: 'contra-costa'
  name                       TEXT NOT NULL,      -- 'Contra Costa' (must match src/lib/counties.ts in Prose)
  court_name                 TEXT NOT NULL,
  rules_url                  TEXT,
  forms_url                  TEXT,
  fcs_url                    TEXT,
  local_rules_effective_from TEXT,               -- ISO date, from the Judicial Branch index
  efiling_json               TEXT NOT NULL DEFAULT '{}',
  extraction_status          TEXT NOT NULL       -- none | partial | complete | needs_review
    CHECK (extraction_status IN ('none','partial','complete','needs_review'))
);

CREATE TABLE sources (
  id            TEXT PRIMARY KEY,                -- 'src_contra-costa_local_rules_2026-07-01'
  county_id     TEXT REFERENCES counties(id),    -- NULL for statewide sources
  kind          TEXT NOT NULL
    CHECK (kind IN ('local_rules','local_forms','standing_order','fcs','court_guidance','crc','statute','judicial_council_form')),
  url           TEXT NOT NULL,
  title         TEXT,
  sha256        TEXT NOT NULL,
  bytes         INTEGER,
  content_type  TEXT,
  r2_key        TEXT,                            -- sources/<sha256>.<ext> — immutable archive
  retrieved_at  TEXT NOT NULL,
  effective_from TEXT,
  effective_to   TEXT
);
CREATE INDEX idx_sources_county ON sources(county_id, kind);
CREATE UNIQUE INDEX idx_sources_sha ON sources(sha256, url);

-- One row per subdivision, not per document. parent_id preserves the (a)(1)(A)
-- hierarchy so a citation can be exact: "Contra Costa Local Rule 5.17(b)(2)".
CREATE TABLE rules (
  id               TEXT PRIMARY KEY,             -- 'contra-costa-5.17@2026-07-01'
  rule_key         TEXT NOT NULL,                -- 'contra-costa-5.17' — stable across editions
  parent_id        TEXT REFERENCES rules(id),
  county_id        TEXT REFERENCES counties(id), -- NULL = statewide
  authority_level  INTEGER NOT NULL,
  citation         TEXT NOT NULL,                -- precomputed, human-renderable
  rule_number      TEXT,                         -- '5.17'
  label            TEXT,                         -- 'b' / '2' for subdivisions
  title            TEXT,
  text             TEXT NOT NULL,
  topics_json      TEXT NOT NULL DEFAULT '[]',
  effective_from   TEXT NOT NULL,
  effective_to     TEXT,                         -- NULL = currently in force
  source_id        TEXT NOT NULL REFERENCES sources(id),
  page             INTEGER,
  extraction_method TEXT NOT NULL CHECK (extraction_method IN ('deterministic','llm')),
  confidence       REAL,
  review_status    TEXT NOT NULL
    CHECK (review_status IN ('verified','needs_review','ambiguous','superseded'))
);
CREATE INDEX idx_rules_county   ON rules(county_id, effective_to, authority_level);
CREATE INDEX idx_rules_parent   ON rules(parent_id);
CREATE INDEX idx_rules_number   ON rules(county_id, rule_number);
-- Point-in-time lookup: the query the whole versioning design exists to serve.
--   WHERE rule_key = ?1 AND effective_from <= ?2
--     AND (effective_to IS NULL OR effective_to > ?2)
CREATE INDEX idx_rules_asof     ON rules(rule_key, effective_from, effective_to);

-- What a litigant must actually DO. The highest-value table, and the one whose
-- errors are most costly, so nothing lands here as 'verified' without review.
CREATE TABLE requirements (
  id                 TEXT PRIMARY KEY,
  rule_id            TEXT NOT NULL REFERENCES rules(id),
  county_id          TEXT REFERENCES counties(id),
  topic              TEXT NOT NULL,
  requirement_type   TEXT NOT NULL,
  actor              TEXT,                       -- 'party' | 'petitioner' | 'attorney' | ...
  action             TEXT,
  description        TEXT NOT NULL,
  trigger_event      TEXT,                       -- 'request_for_order_hearing'
  deadline_amount    INTEGER,
  deadline_unit      TEXT CHECK (deadline_unit IN ('calendar_days','court_days','hours','months') OR deadline_unit IS NULL),
  deadline_direction TEXT CHECK (deadline_direction IN ('before','after') OR deadline_direction IS NULL),
  exceptions_json    TEXT NOT NULL DEFAULT '[]',
  applies_to_json    TEXT NOT NULL DEFAULT '[]',
  confidence         REAL,
  review_status      TEXT NOT NULL
    CHECK (review_status IN ('verified','needs_review','ambiguous','superseded'))
);
CREATE INDEX idx_requirements_county ON requirements(county_id, topic, requirement_type);

CREATE TABLE forms (
  id             TEXT PRIMARY KEY,
  county_id      TEXT REFERENCES counties(id),   -- NULL = Judicial Council statewide
  form_number    TEXT NOT NULL,                  -- 'ALA FL-002' | 'FL-300'
  title          TEXT NOT NULL,
  mandatory      INTEGER NOT NULL DEFAULT 0,
  effective_from TEXT,
  effective_to   TEXT,
  url            TEXT,
  source_id      TEXT REFERENCES sources(id),
  topics_json    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_forms_county ON forms(county_id);

CREATE TABLE citations (
  from_rule_id TEXT NOT NULL REFERENCES rules(id),
  relation     TEXT NOT NULL
    CHECK (relation IN ('implements','subject_to','related_to','uses_form','superseded_by')),
  to_kind      TEXT NOT NULL CHECK (to_kind IN ('rule','statute','crc','form')),
  to_ref       TEXT NOT NULL,
  PRIMARY KEY (from_rule_id, relation, to_kind, to_ref)
);

CREATE TABLE changes (
  id                    TEXT PRIMARY KEY,
  detected_at           TEXT NOT NULL,
  county_id             TEXT,
  source_id             TEXT,
  old_sha256            TEXT,
  new_sha256            TEXT,
  changed_rule_ids_json TEXT NOT NULL DEFAULT '[]',
  pr_url                TEXT
);
CREATE INDEX idx_changes_detected ON changes(detected_at);

CREATE TABLE corpus_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE VIRTUAL TABLE rules_fts USING fts5(
  title, text, content='rules', content_rowid='rowid', tokenize='porter'
);
