-- SQLite schema of live culture-library 0.19.8 (spark), from the t18 drill snapshot of 2026-11-01:
-- a library.db that 0.19.8 had opened. (An earlier copy, taken from the pre-0.19.8 backup, lacked
-- vector_log and its triggers; lapse l5.)
-- Reference for the Postgres port (plan tasks t4 and t14). Generated; do not edit.
-- table api_keys
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    kinds TEXT,
    private INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used TEXT
, replaces INTEGER REFERENCES api_keys(id));

-- table embeddings
CREATE TABLE embeddings (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, chunk_no)
);

-- table eval_run_samples
CREATE TABLE eval_run_samples (
    run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    retrieved_chunk_shas TEXT NOT NULL DEFAULT '[]',
    scores_json TEXT NOT NULL DEFAULT '{}', question TEXT, reference_chunk_shas TEXT, set_version INTEGER, retrieved_item_ids TEXT, latency_ms REAL, freshness TEXT,
    PRIMARY KEY (run_id, sample_id)
);

-- table eval_runs
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    set_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    config_json TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    aggregates_json TEXT,
    created_at TEXT NOT NULL, partial_aggregates_json TEXT, error_rate REAL, suites_json TEXT, running INTEGER NOT NULL DEFAULT 0,
    CHECK (aggregates_json IS NULL OR status = 'complete')
);

-- table eval_samples
CREATE TABLE eval_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    reference_answer TEXT NOT NULL,
    reference_chunk_shas TEXT NOT NULL DEFAULT '[]',
    reference_contexts TEXT NOT NULL DEFAULT '[]',
    source_urls TEXT NOT NULL DEFAULT '[]'
);

-- table eval_sets
CREATE TABLE eval_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_sha256 TEXT NOT NULL,
    chunker_json TEXT NOT NULL,
    adapter TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    generator_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- table feed
CREATE TABLE feed (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    at TEXT NOT NULL,
    data TEXT
, subject TEXT NOT NULL DEFAULT 'item');

-- table index_meta
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- table index_queue
CREATE TABLE index_queue (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    body_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT
, priority INTEGER NOT NULL DEFAULT 0);

-- table item_sources
CREATE TABLE item_sources (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    manifest_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- table item_uploaders
CREATE TABLE item_uploaders (
    item_id INTEGER PRIMARY KEY,
    key_id TEXT NOT NULL
);

-- table items
CREATE TABLE "items" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'article', 'newspaper', 'document', 'transcript', 'diary', 'spec', 'plan', 'devague-record', 'todo', 'discord-message', 'design')),
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);

-- table mirror_outbox
CREATE TABLE mirror_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    enqueued_at TEXT NOT NULL,
    done_at TEXT
);

-- table projects
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL UNIQUE,
    domains TEXT NOT NULL DEFAULT '[]',
    connected TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

-- table repodoc_dirty
CREATE TABLE repodoc_dirty (doc_id INTEGER PRIMARY KEY);

-- table repodoc_drafts
CREATE TABLE repodoc_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL UNIQUE REFERENCES repodocs(id),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    pushed_at TEXT NOT NULL
);

-- table repodoc_embeddings
CREATE TABLE repodoc_embeddings (
    doc_id INTEGER NOT NULL REFERENCES repodocs(id) ON DELETE CASCADE,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (doc_id, chunk_no)
);

-- table repodoc_fts
CREATE VIRTUAL TABLE repodoc_fts USING fts5(
    title, body, label UNINDEXED, visibility UNINDEXED, tokenize = 'porter unicode61'
);

-- table repodoc_fts_config
CREATE TABLE 'repodoc_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table repodoc_fts_content
CREATE TABLE 'repodoc_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3);

-- table repodoc_fts_data
CREATE TABLE 'repodoc_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table repodoc_fts_docsize
CREATE TABLE 'repodoc_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table repodoc_fts_idx
CREATE TABLE 'repodoc_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table repodoc_index_queue
CREATE TABLE repodoc_index_queue (
    doc_id INTEGER PRIMARY KEY REFERENCES repodocs(id) ON DELETE CASCADE,
    shown_sha256 TEXT NOT NULL,
    shown_visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT
);

-- table repodoc_versions
CREATE TABLE repodoc_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES repodocs(id),
    commit_sha TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    on_default_branch INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    pushed_at TEXT NOT NULL,
    UNIQUE (doc_id, commit_sha)
);

-- table repodocs
CREATE TABLE repodocs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, removed_at TEXT, removed_commit TEXT,
    UNIQUE (repo, path)
);

-- table search_fts
CREATE VIRTUAL TABLE search_fts USING fts5(
    title, body, tokenize = 'porter unicode61'
);

-- table search_fts_config
CREATE TABLE 'search_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table search_fts_content
CREATE TABLE 'search_fts_content'(id INTEGER PRIMARY KEY, c0, c1);

-- table search_fts_data
CREATE TABLE 'search_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table search_fts_docsize
CREATE TABLE 'search_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table search_fts_idx
CREATE TABLE 'search_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table spec_links
CREATE TABLE spec_links (
    doc_id INTEGER PRIMARY KEY,
    pr_url TEXT,
    pr_number INTEGER,
    merge_commit TEXT,
    code_paths_json TEXT NOT NULL DEFAULT '[]',
    at_commit TEXT,
    at_committed_at TEXT,
    trailer_commits_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

-- table vector_log
CREATE TABLE vector_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, tbl TEXT NOT NULL, owner INTEGER NOT NULL, at REAL NOT NULL);

-- index eval_runs_set
CREATE INDEX eval_runs_set ON eval_runs(set_id);

-- index eval_samples_set
CREATE INDEX eval_samples_set ON eval_samples(set_id);

-- index eval_sets_item
CREATE INDEX eval_sets_item ON eval_sets(item_id);

-- index feed_item_id
CREATE INDEX feed_item_id ON feed(item_id);

-- index item_uploaders_key
CREATE INDEX item_uploaders_key ON item_uploaders(key_id)
;

-- index mirror_outbox_state
CREATE INDEX mirror_outbox_state ON mirror_outbox (state)
;

-- index repodoc_versions_doc
CREATE INDEX repodoc_versions_doc ON repodoc_versions (doc_id);

-- trigger embeddings_delete_vlog
CREATE TRIGGER embeddings_delete_vlog AFTER DELETE ON embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('embeddings', OLD.item_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger embeddings_insert_vlog
CREATE TRIGGER embeddings_insert_vlog AFTER INSERT ON embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('embeddings', NEW.item_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger embeddings_update_vlog
CREATE TRIGGER embeddings_update_vlog AFTER UPDATE ON embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('embeddings', NEW.item_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger repodoc_drafts_delete_dirty
CREATE TRIGGER repodoc_drafts_delete_dirty AFTER DELETE ON repodoc_drafts BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT OLD.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = OLD.doc_id); END;

-- trigger repodoc_drafts_insert_dirty
CREATE TRIGGER repodoc_drafts_insert_dirty AFTER INSERT ON repodoc_drafts BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.doc_id); END;

-- trigger repodoc_drafts_update_dirty
CREATE TRIGGER repodoc_drafts_update_dirty AFTER UPDATE ON repodoc_drafts WHEN OLD.sha256 IS NOT NEW.sha256 OR OLD.visibility IS NOT NEW.visibility BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.doc_id); END;

-- trigger repodoc_embeddings_delete_vlog
CREATE TRIGGER repodoc_embeddings_delete_vlog AFTER DELETE ON repodoc_embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('repodoc_embeddings', OLD.doc_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger repodoc_embeddings_insert_vlog
CREATE TRIGGER repodoc_embeddings_insert_vlog AFTER INSERT ON repodoc_embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('repodoc_embeddings', NEW.doc_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger repodoc_embeddings_update_vlog
CREATE TRIGGER repodoc_embeddings_update_vlog AFTER UPDATE ON repodoc_embeddings BEGIN INSERT INTO vector_log (tbl, owner, at) VALUES ('repodoc_embeddings', NEW.doc_id, (julianday('now') - 2440587.5) * 86400.0); END;

-- trigger repodoc_versions_delete_dirty
CREATE TRIGGER repodoc_versions_delete_dirty AFTER DELETE ON repodoc_versions BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT OLD.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = OLD.doc_id); END;

-- trigger repodoc_versions_insert_dirty
CREATE TRIGGER repodoc_versions_insert_dirty AFTER INSERT ON repodoc_versions BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.doc_id); END;

-- trigger repodoc_versions_update_dirty
CREATE TRIGGER repodoc_versions_update_dirty AFTER UPDATE ON repodoc_versions WHEN OLD.sha256 IS NOT NEW.sha256 OR OLD.visibility IS NOT NEW.visibility OR OLD.committed_at IS NOT NEW.committed_at BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.doc_id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.doc_id); END;

-- trigger repodocs_delete_dirty
CREATE TRIGGER repodocs_delete_dirty AFTER DELETE ON repodocs BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT OLD.id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = OLD.id); END;

-- trigger repodocs_insert_dirty
CREATE TRIGGER repodocs_insert_dirty AFTER INSERT ON repodocs BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.id); END;

-- trigger repodocs_update_dirty
CREATE TRIGGER repodocs_update_dirty AFTER UPDATE ON repodocs WHEN OLD.visibility IS NOT NEW.visibility OR OLD.current_version_id IS NOT NEW.current_version_id OR OLD.removed_at IS NOT NEW.removed_at BEGIN INSERT INTO repodoc_dirty (doc_id) SELECT NEW.id WHERE NOT EXISTS (SELECT 1 FROM repodoc_dirty WHERE doc_id = NEW.id); END;
