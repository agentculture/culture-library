# Cutover from SQLite to Postgres

This runbook moves the live library from the 0.19.x SQLite release to the Postgres release.
It is run by the operator, present, in one sitting. The aim is a freeze of under 30 minutes.
Spec: `docs/specs/2026-10-31-library-on-postgres-and-pgvector.md`. Plan:
`docs/plans/2026-10-31-library-on-postgres-and-pgvector.md`.

The operator decided on 2026-10-31 to migrate to Postgres before any further SQLite
optimisation (issue #47).

The old SQLite volume is never written by this runbook after the final backup. That is
what makes rollback possible until step 6.

## Prerequisites

Do these before the freeze. None of them touches the live stack.

1. Create the two new grant secrets (`grant show NAME` confirms one exists without printing
   it):

   ```bash
   grant set CULTURE_LIBRARY_PG_PASSWORD - --hidden             # the application role
   grant set CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD - --hidden   # becomes POSTGRES_PASSWORD
   ```

   Neither goes in `live.env`; `deploy restart` refuses them there.
2. Build the image from the merged release, and keep the running image under its own tag
   for the final SQLite backup and for rollback:

   ```bash
   docker tag culture-library:local culture-library:<old-version>
   git checkout <release tag>
   docker compose -p culture-library -f deploy/compose.yaml build library
   ```

   Do not recreate any container yet. Building replaces the `culture-library:local` tag,
   not the running containers.
3. A restore drill has passed on the Postgres stack (plan task t18, see
   [backup.md](backup.md)). Do not cut over without it.
4. Check the `culture_library` database does not exist yet (the volume
   `culture-library-pgdata` is new and empty). The copy refuses a non-empty target.

## Freeze

1. Pause the culture-rules rules that write to the library. At minimum `repodocs-sync`.
   List the others with the culture-rules CLI at that time and pause each one that pushes
   to the library (todos, newspapers, Discord messages). The operator accepted handling any
   other writer retroactively: a write that lands on SQLite after the final backup is not
   carried over, so note what ran during the window. Note the time: this is the freeze
   start.
2. Stop `library` and `mcp` (cloudflared can stay; it will serve errors):

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml stop library mcp
   ```

3. Take the final SQLite backup with the OLD image, then verify it. This is the existing
   pre-Postgres procedure (the image runs as uid 10001, so the empty host directory must be
   writable for it during the run):

   ```bash
   B=~/backups/culture-library-pre-postgres; mkdir -p "$B"; chmod o+w "$B"
   docker run --rm -v culture-library_culture-library-data:/data -v "$B":/backup \
     --entrypoint culture-library culture-library:<old-version> \
     backup run --data-dir /data --to /backup
   chmod o-w "$B"
   docker run --rm -v "$B":/backup:ro --entrypoint culture-library \
     culture-library:<old-version> backup verify /backup/snapshots/<UTC stamp>
   ```

   `verify` must print OK. The snapshot holds `library.db`; this is the copy's source and
   the rollback copy.

## Copy

1. Start Postgres. The init script creates the database, the role and the `vector`
   extension on the empty volume:

   ```bash
   uv run culture-library deploy restart --dry-run   # read the steps first
   grant run --inject POSTGRES_PASSWORD=CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD \
     --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
     docker compose -p culture-library -f deploy/compose.yaml \
     --env-file "$HOME/.config/culture-library/live.env" up -d --no-deps --wait postgres
   ```

   (This is the first step `deploy restart` prints. Starting only that step leaves
   `library` and `mcp` stopped.)
2. Run the copy from the snapshot's `library.db` into the empty database. It runs in a
   one-off container from the new image, on the compose network, with the password from
   grant; the gateway is only asked for `/capabilities` (no embedding calls):

   ```bash
   grant run --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
     docker compose -p culture-library -f deploy/compose.yaml \
     --env-file "$HOME/.config/culture-library/live.env" run --rm --no-deps \
     -v "$B/snapshots/<UTC stamp>":/src:ro library \
     culture-library migrate sqlite-to-postgres --from /src/library.db --json \
     > ~/backups/culture-library-copy-report.json
   ```

   It runs in one transaction and writes nothing if it finds a problem: NaN or Infinity in
   a vector, a NUL byte, an orphan row or an unknown column. It fills `model_id` and
   `strategy` from the gateway, drops and rebuilds the HNSW indexes, and prints a per-table
   report (row counts, max ids, digests). Exit 0 means every table is equal. Exit 1 means
   problems or a mismatch, exit 2 a gateway or database error. About 72 s for 70,000 vectors
   on the test database. Vectors made under another model label stop it unless you pass
   `--drop-stale-vectors` (the indexer re-embeds those afterwards).

   If it does not exit 0, stop. Nothing was committed. Go to Rollback.
3. Compare again from outside the copy (optional, cheap): the same command with
   `--verify-only`.

## Start on the new image, and check

1. `uv run culture-library deploy restart` starts `postgres` (already healthy), then
   `library` and `mcp` on the new image with every grant secret injected. It waits for
   `/api/health` and lists the send targets.
2. Go/no-go checks. Every one must pass:
   - `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8796/api/health` prints
     `200`.
   - `GET /api/status` (read key): the `database` block has `reachable: true`,
     `schema_current: true` (`schema_version` equals `expected_schema_version`), and
     `vector_indexes` present.
   - Item, repo-document and embedding counts in `/api/status` equal the copy report.
   - A search answers: `GET /api/search?q=<a word you know>` returns hits with `mode`
     `hybrid`.
   - A RAG retrieve answers: `POST /api/rag/retrieve` with `{"query": "...", "k": 5}`
     returns passages and `reranked` true.
   - The mirror is whole: `python3 scripts/verify-mirror.py --all` reports 0 missing and
     0 mismatched (it needs `CULTURE_LIBRARY_API_KEY`; add `--no-s3` only if S3 is down,
     and then say so in the record).
   - A Postgres backup succeeds: run the live backup command from
     [backup.md](backup.md) once and `backup verify` the new snapshot.

   Any failure here is still before the point of no return: Rollback applies.

## Point of no return

Resume the culture-rules rules you paused (at minimum `repodocs-sync`). From this moment
writes go to Postgres, and nothing copies them back to SQLite.

- Before this step, rollback is: stop `library`, `mcp` and `postgres`, point
  `culture-library:local` back at the old image (`docker tag culture-library:<old-version>
  culture-library:local`), and start it on the untouched SQLite volume with the
  `deploy restart` of that release. No data is lost
  except what was written between the freeze and the rollback, which is nothing: writers
  were paused.
- After this step, problems are fixed forward: patch the release, restart on the new
  image. Do not run the old image against the Postgres writes, and do not try to restore
  the pre-cutover SQLite snapshot over them. A restore of a Postgres snapshot
  ([backup.md](backup.md)) is still available for data damage.

## Rollback

Only before the point of no return. Stop `library` and `mcp`, start the old image on the
SQLite volume (`culture-library_culture-library-data`) the way the previous release was
started (retag the old image as `culture-library:local` first), check `/api/health`, then resume the paused rules. Leave the Postgres volume in
place for diagnosis. Record why it failed.

## Record

For the delivery summary, write down:

- freeze start (the first pause) and freeze end (the point of no return); the target is
  under 30 minutes;
- the copy report (`~/backups/culture-library-copy-report.json`): per-table counts, max
  ids and digests, and the elapsed time;
- the results of each go/no-go check;
- which rules were paused, and any writer that ran during the window.
