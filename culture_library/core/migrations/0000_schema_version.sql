-- The migration ledger: one row per core/migrations file applied to this database.
-- The runner (culture_library.core.migrations.migrate) inserts the rows; nothing else writes.
CREATE TABLE schema_version (
    version integer PRIMARY KEY,
    name text NOT NULL,
    applied_at text NOT NULL
);
