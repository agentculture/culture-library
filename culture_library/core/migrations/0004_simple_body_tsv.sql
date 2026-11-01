-- The body as a 'simple' tsvector (no stemming, no stopwords dropped), indexed, beside the
-- weighted english tsv. A query's last word matches as a prefix, as in FTS5; when that word is
-- a stopword the english tsv never stores it, so the prefix is checked here: '... ship with?'
-- then finds a body that says "with" (core/search.py, _FtsQuery; deviations d20, d21, d22).
-- About 49 MB and 3 s to build over live's text on 2026-11-01.
ALTER TABLE item_text
  ADD COLUMN simple_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, body)) STORED;
ALTER TABLE repodoc_text
  ADD COLUMN simple_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, body)) STORED;
CREATE INDEX item_text_simple_tsv ON item_text USING gin (simple_tsv);
CREATE INDEX repodoc_text_simple_tsv ON repodoc_text USING gin (simple_tsv);
