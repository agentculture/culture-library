# Running the tests against Postgres

The suite needs a Postgres 17 with pgvector. Point `CULTURE_LIBRARY_TEST_PG_DSN` at it; the role
must be allowed to `CREATE DATABASE`. Without a reachable server pytest stops before collection
with one error naming this setting. Tests are never skipped for lack of a database.

## Local one-liner

```bash
docker run -d --name culture-library-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_INITDB_ARGS="--locale=C --encoding=UTF8" pgvector/pgvector:pg17 -c fsync=off -c synchronous_commit=off -c full_page_writes=off
export CULTURE_LIBRARY_TEST_PG_DSN=postgresql://postgres@127.0.0.1:55432/postgres
uv run pytest -n auto
```

The port is bound to `127.0.0.1` only, and trust auth is for this throwaway container, never a
shared server. `culture.yaml`'s gate (`uv run pytest -n auto`) needs the same variable.

## How isolation works

- The controller builds one migrated template database per run
  (`culture_library_test_tpl_<run>`). If `culture_library.core.migrations` is not present yet,
  the template is empty.
- Each xdist worker clones the template into its own database
  (`CREATE DATABASE ... TEMPLATE`) and sets `CULTURE_LIBRARY_PG_DSN` to it, which is what the
  store reads. Workers drop their database, and the controller drops the template, at session end.
- Every test starts from empty tables: an autouse fixture runs
  `TRUNCATE ... RESTART IDENTITY CASCADE` on all `public` tables, then restores any rows the
  migrations seeded (such as migration bookkeeping) from a snapshot.
  A clone per test was measured at about 105 ms against about 15 ms for the truncate (40 tables),
  so truncate wins. A test that creates tables keeps them for the worker's lifetime.

A crashed run can leave `culture_library_test_*` databases behind; drop them by hand.

In CI, the `test` jobs of `tests.yml` and `publish.yml` (and the opt-in `visual` and `ragas`
jobs, which also load the root `conftest.py`) run a `pgvector/pgvector:pg17` service.

## Backup tests and pg_dump

`tests/core/test_backup.py` and `tests/test_cli_backup.py` run the real `pg_dump` and
`pg_restore` (17). CI installs `postgresql-client-17` from PGDG in the `test` jobs. A host
without the client tools (spark has none) can use the test container instead: if neither tool is
on `PATH`, the `pg_bin` fixture in `tests/backup_fixtures.py` writes two wrappers that run
`docker exec -i culture-library-pgtest <tool> ...`, rewriting the host port `55432` to the
container's own `5432`, and points `CULTURE_LIBRARY_PG_BIN_DIR` at them for the session. So the
container must be named `culture-library-pgtest`; with neither route the tests fail, they are
never skipped. Outside the tests, set `CULTURE_LIBRARY_PG_BIN_DIR` yourself.
