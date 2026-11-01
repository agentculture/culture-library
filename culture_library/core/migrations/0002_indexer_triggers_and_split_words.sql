-- The indexer port (plan task t13): the repodoc_dirty marks as plpgsql triggers, and the
-- full-text vectors also index the words inside punctuated tokens (plan risk r5).

-- 1. repodoc_dirty (#39). A write that changes what a repo doc shows marks the doc in the
-- same transaction, so discovery (core/indexer.py, discover_repodocs) plans only marked docs
-- and an idle tick reads no repodocs row. INSERT and DELETE always mark (a hard-deleted doc is
-- dropped from the index); an UPDATE marks only WHEN a column the discovery query
-- (indexer._SHOWN_SQL) reads changes, so a re-push of unchanged bytes marks nothing. The
-- watched columns are indexer.DIRTY_SOURCES; tests/core/test_repodoc_dirty.py checks that the
-- triggers below watch exactly those, so a new column there needs a new migration here.
--
-- The mark is an upsert that touches an existing mark (DO UPDATE, not DO NOTHING) on purpose:
-- it row-locks the mark until the writing transaction commits. Discovery claims marks with
-- DELETE ... RETURNING and then reads the docs in a later statement; a DELETE that meets a
-- locked mark waits for the writer, so the docs it then reads include that writer's commit.
-- With DO NOTHING a second writer behind an already committed mark would take no lock, and
-- discovery could claim the mark before that writer committed and never plan its change.
CREATE FUNCTION repodoc_mark_dirty() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    doc bigint;
BEGIN
    IF TG_TABLE_NAME = 'repodocs' THEN
        doc := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        doc := CASE WHEN TG_OP = 'DELETE' THEN OLD.doc_id ELSE NEW.doc_id END;
    END IF;
    INSERT INTO repodoc_dirty (doc_id) VALUES (doc)
        ON CONFLICT (doc_id) DO UPDATE SET doc_id = EXCLUDED.doc_id;
    RETURN NULL;
END
$$;

CREATE TRIGGER repodocs_write_dirty AFTER INSERT OR DELETE ON repodocs
    FOR EACH ROW EXECUTE FUNCTION repodoc_mark_dirty();
CREATE TRIGGER repodocs_update_dirty AFTER UPDATE ON repodocs
    FOR EACH ROW WHEN (
        OLD.visibility IS DISTINCT FROM NEW.visibility
        OR OLD.current_version_id IS DISTINCT FROM NEW.current_version_id
        OR OLD.removed_at IS DISTINCT FROM NEW.removed_at
    ) EXECUTE FUNCTION repodoc_mark_dirty();

CREATE TRIGGER repodoc_drafts_write_dirty AFTER INSERT OR DELETE ON repodoc_drafts
    FOR EACH ROW EXECUTE FUNCTION repodoc_mark_dirty();
CREATE TRIGGER repodoc_drafts_update_dirty AFTER UPDATE ON repodoc_drafts
    FOR EACH ROW WHEN (
        OLD.sha256 IS DISTINCT FROM NEW.sha256
        OR OLD.visibility IS DISTINCT FROM NEW.visibility
    ) EXECUTE FUNCTION repodoc_mark_dirty();

CREATE TRIGGER repodoc_versions_write_dirty AFTER INSERT OR DELETE ON repodoc_versions
    FOR EACH ROW EXECUTE FUNCTION repodoc_mark_dirty();
CREATE TRIGGER repodoc_versions_update_dirty AFTER UPDATE ON repodoc_versions
    FOR EACH ROW WHEN (
        OLD.sha256 IS DISTINCT FROM NEW.sha256
        OR OLD.visibility IS DISTINCT FROM NEW.visibility
        OR OLD.committed_at IS DISTINCT FROM NEW.committed_at
    ) EXECUTE FUNCTION repodoc_mark_dirty();

-- A database that held repo docs before the triggers existed (one copied from SQLite) has
-- writes no trigger saw: mark every doc once, so the next discovery re-plans them all.
INSERT INTO repodoc_dirty (doc_id) SELECT id FROM repodocs ON CONFLICT (doc_id) DO NOTHING;

-- (The helper functions call each other schema-qualified: pg_restore runs with an empty
-- search_path.)
-- 2. Punctuated tokens (plan risk r5). Postgres's parser reads core/search.py as one file
-- lexeme where SQLite FTS5's unicode61 tokenizer read core, search and py, so "search" would no
-- longer find a body that names core/search.py. Each text now also indexes the words of its
-- tokens with internal punctuation: the inner regexp_replace deletes every whitespace-separated
-- token that has none (punctuation only at its ends, as in "jumped." or "(see"), the outer one
-- turns the ASCII punctuation left into spaces, and that text's vector is appended after the
-- plain one. So text without such tokens gets exactly the vector it had, and the words of one
-- path stay adjacent: to_tsquery reads culture_library as the phrase cultur <-> librari, which
-- must match inside culture_library/core/indexer.py. [[:punct:]] rather than [^[:alnum:]]: the
-- database's ctype is C, where [:alnum:] is ASCII only and would split café into caf; with
-- [:punct:] only ASCII punctuation becomes a space. Weights stay A for the title, B for body.
-- A generated column cannot be altered in place: drop it (its GIN index goes with it), re-add
-- it and recreate the index; the re-add recomputes every stored row.
ALTER TABLE item_text DROP COLUMN tsv;
ALTER TABLE repodoc_text DROP COLUMN tsv;
CREATE OR REPLACE FUNCTION library_fts_split(t text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT regexp_replace(regexp_replace(' ' || t || ' ',
        '(?<=\s)[[:punct:]]*[^[:punct:][:space:]]*[[:punct:]]*(?=\s)', '', 'g'),
        '[[:punct:]]+', ' ', 'g') $$;
CREATE OR REPLACE FUNCTION library_fts_part(t text, w "char") RETURNS tsvector
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT setweight(to_tsvector('english'::regconfig, t)
        || to_tsvector('english'::regconfig, public.library_fts_split(t)), w) $$;
CREATE OR REPLACE FUNCTION library_fts_doc(title text, body text) RETURNS tsvector
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT public.library_fts_part(title, 'A') || public.library_fts_part(body, 'B') $$;
ALTER TABLE item_text ADD COLUMN tsv tsvector GENERATED ALWAYS AS (library_fts_doc(title, body)) STORED;
CREATE INDEX item_text_tsv ON item_text USING gin (tsv);
ALTER TABLE repodoc_text ADD COLUMN tsv tsvector GENERATED ALWAYS AS (library_fts_doc(title, body)) STORED;
CREATE INDEX repodoc_text_tsv ON repodoc_text USING gin (tsv);
