# Backups and restore drill

The library is a Postgres database plus a content-addressed files tree
(`files/<aa>/<sha256>`) under `CULTURE_LIBRARY_DATA_DIR`. `culture-library backup`
copies both to a second location and can restore them onto an empty database and
an empty directory.

## What a backup is

```text
<backup>/files/<aa>/<sha256>                   shared blobs, copied only when missing
<backup>/snapshots/<UTC stamp>/library.dump    pg_dump custom-format dump of the database
<backup>/snapshots/<UTC stamp>/manifest.json   ids, sha256, visibility, per-table row counts,
                                               blob list, dump sha256, created_at
```

- The dump and the manifest come from one exported transaction snapshot
  (`pg_export_snapshot` and `pg_dump --snapshot`), so they agree while the server
  keeps writing. No need to stop the service.
- Blobs are immutable and named by their hash, so each run copies only new ones and
  re-hashes every copy.
- "Every blob" means every sha256 named by a public table with a `sha256` column:
  catalog items, repo-document versions and drafts, the mirror outbox and any later
  table. The `feed` log is skipped, since it also names blobs of purged items.
- `--keep N` (default 7) prunes old snapshots. Blobs are never pruned, so a backup
  location only grows; clear it by hand if items were deleted and space matters.
- The backup location must be outside the data directory, ideally another disk or host.
- The database comes from the `CULTURE_LIBRARY_PG_*` settings the server uses
  (`--dsn` overrides them).

## Client tools

`backup` needs `pg_dump` and `pg_restore` of PostgreSQL 17. The library image installs
them (PGDG `postgresql-client-17`), so on the live stack run the backup inside the
library container, against the `postgres` service. Elsewhere, put them on `PATH` or
set `CULTURE_LIBRARY_PG_BIN_DIR` to the directory that holds both. A missing tool is an
error (exit 2) that names this setting; nothing is skipped.

## Commands

```bash
export CULTURE_LIBRARY_DATA_DIR=/srv/culture-library
export CULTURE_LIBRARY_BACKUP_DIR=/mnt/second-disk/culture-library-backup

culture-library backup run                      # or: --to DIR --keep 14
culture-library backup verify <snapshot-dir>
culture-library backup restore --from <snapshot-dir> --to <empty-dir> --dsn <empty-db-dsn>
culture-library backup overview                 # verbs, plus the last successful backup
```

`verify` needs no database. It checks the dump file against the manifest's sha256,
runs `pg_restore --list` to confirm every manifest table is in the dump, and re-hashes
every blob the manifest names.

`restore` refuses a non-empty files target and a database that already has tables. It
verifies the snapshot first, loads the dump with `pg_restore --single-transaction`,
copies the blobs back, then checks the restored database: every table's row count, and
every item's and repo-document version's id, sha256 and visibility, against the
manifest, and re-hashes every blob. The target database must exist, be empty, use the C
collation (the same rule the server enforces), and already have the `vector` extension.
Only a superuser may create the extension, so make the target the way the init script makes
the live database; the restore itself then runs as the application role. It skips the
dump's comments: the only one is the extension's, which only its owner may set. Exit code 1
means a check failed or a target was not empty.

On the live stack (the superuser password is the grant secret
`CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD`; inside the `postgres` container the socket needs
none):

```bash
docker compose -p culture-library -f deploy/compose.yaml exec -T postgres psql -U postgres \
  -c "CREATE DATABASE culture_library_restore OWNER culture_library TEMPLATE template0
      ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
docker compose -p culture-library -f deploy/compose.yaml exec -T postgres psql -U postgres \
  -d culture_library_restore -c "CREATE EXTENSION vector"
R=/mnt/second-disk/culture-library-restore; mkdir -p "$R"; chmod o+w "$R"
grant run --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
  docker compose -p culture-library -f deploy/compose.yaml \
  --env-file "$HOME/.config/culture-library/live.env" run --rm --no-deps \
  -v "$B":/backup:ro -v "$R":/restore library culture-library backup restore \
  --from /backup/snapshots/<UTC stamp> --to /restore \
  --dsn "host=postgres dbname=culture_library_restore user=culture_library"
```

The t18 drill (2026-11-01) ran this shape on a scratch server built from the same image and
init script; see "Restore drill" below.

## Schedule

`backup schedule` writes a daily systemd user timer and service and enables nothing.
`--command` is required to write units: the database password is a grant secret that is
never in `live.env` and Postgres publishes no port, so a bare `culture-library backup run`
cannot reach the database from a timer (see "On the live stack" below for the command):

```bash
culture-library backup schedule --command "<the live-stack backup command>" \
  --to /mnt/second-disk/culture-library-backup
```

Without `--command` it refuses and exits 1; `backup schedule --print` still prints a
template. It writes `culture-library-backup.service` and `.timer` into
`$HOME/.config/systemd/user/` (`--unit-dir` changes that) and prints the commands to run
yourself:

```bash
systemctl --user daemon-reload
systemctl --user enable --now culture-library-backup.timer
systemctl --user list-timers
```

### On the live stack

`postgres` publishes no port and the host has no PostgreSQL 17 client tools, so the dump
has to run inside a container built from the library image (which has `pg_dump`). The
compose file mounts only the files volume into `library`, so a backup needs a one-off
container that also mounts the backup directory and gets the app role's password from
grant. The backup directory must be writable by uid 10001 (the image's user):

```bash
B=/mnt/second-disk/culture-library-backup; mkdir -p "$B"; chmod o+w "$B"
grant run --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
  docker compose -p culture-library -f deploy/compose.yaml \
  --env-file "$HOME/.config/culture-library/live.env" run --rm --no-deps \
  -v "$B":/backup library culture-library backup run --to /backup
```

For the timer, pass that same command as `--command` (systemd needs absolute paths for the
first word, and no shell is involved unless you write `/bin/sh -c '...'`):

```bash
culture-library backup schedule --data-dir /data --to "$B" \
  --command "/bin/sh -c '<the grant run ... command above>'"
```

This form is written from the code and the compose file; run it once by hand and check
`backup overview` before trusting the timer.

The service reads `$HOME/.config/culture-library/live.env` if it exists. `backup schedule
--print [--kind systemd|cron]` only prints the text. `backup overview` shows the last
successful backup (the newest snapshot with a manifest), so a missed day is visible.

## Restore drill (spark, 2026-10-24; SQLite era)

This is the drill from before the move to Postgres, kept as a record. It exercised the
old SQLite backup and is not how a backup is taken or restored now. The Postgres restore
drill is plan task t18 (restore a snapshot into an empty database and an empty files
directory, then compare against the manifest); its result goes in the delivery summary.

Run on spark before any teardown. Scratch space only; all copies deleted afterwards.
The live xteink service was only read: `docker cp xteink-api-1:/data` copies out of the
container and nothing was written into it. Paths below are shortened (`$S` is a scratch
directory under the session's temp area).

1. Copy the xteink data out and migrate it into a fresh library (17 items, all public):

   ```text
   $ docker cp xteink-api-1:/data $S/xteink
   $ culture-library migrate from-xteink --data-dir $S/xteink --dest $S/live --apply --visibility public
   ...
   20  1f88869e17c4  article -> newspaper  copied  Daily newspaper 2026-10-23
   17/17 items with matching sha256
   ```

2. Back up twice. The second run is incremental (no new blobs):

   ```text
   $ culture-library backup run --data-dir $S/live --to $S/backup
   snapshot $S/backup/snapshots/20261024T003155Z (17 items, 17 new blobs of 17; pruned 0)
   $ culture-library backup run --data-dir $S/live --to $S/backup
   snapshot $S/backup/snapshots/20261024T003155Z-1 (17 items, 0 new blobs of 17; pruned 0)
   $ culture-library backup verify $S/backup/snapshots/20261024T003155Z
   verify: OK (17 items) ...
   ```

3. Restore onto a clean directory, then try again onto the now non-empty one:

   ```text
   $ culture-library backup restore --from $S/backup/snapshots/20261024T003155Z-1 --to $S/restored
   restore: OK (17 items) $S/restored
   $ culture-library backup restore --from ... --to $S/restored
   error: restore target $S/restored is not empty; refusing to overwrite
   exit=1
   ```

4. Independent check, outside the tool. `select id,sha256,visibility from items order by id`
   on the live and restored databases, diffed: identical, 17 rows
   (ids 1 to 20 with gaps, as in the xteink source). Every one of the 17 restored blobs
   re-hashed with `sha256sum` matched its filename.

Result: 17/17 ids, sha256 and visibility reproduced on a clean directory. The same flow
is covered by `tests/core/test_backup.py` and `tests/test_cli_backup.py`.

Not yet drilled: restoring from a second host, and a restore of a snapshot taken while
the HTTP server is under write load. Repeat the drill on the Postgres stack (t18).
