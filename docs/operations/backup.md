# Backups and restore drill

The library is a SQLite database (`library.db`, WAL) plus a content-addressed files
tree (`files/<aa>/<sha256>`) under `CULTURE_LIBRARY_DATA_DIR`. `culture-library backup`
copies both to a second location and can restore them onto a clean directory.

## What a backup is

```text
<backup>/files/<aa>/<sha256>                  shared blobs, copied only when missing
<backup>/snapshots/<UTC stamp>/library.db     consistent copy (SQLite online backup API)
<backup>/snapshots/<UTC stamp>/manifest.json  ids, sha256, visibility, counts, created_at
```

- The database is copied with the SQLite online backup API, so it is consistent while
  the server is writing. No need to stop the service.
- Blobs are immutable and named by their hash, so each run copies only new ones and
  re-hashes every copy.
- "Every blob" means every sha256 named by a table with a `sha256` column: catalog
  items and repo-document versions (`repodoc_versions`). The `feed` log is skipped,
  since it also names blobs of purged items. The manifest lists repo-document versions
  and counts them (`repodoc_count`, `repodoc_version_count`).
- `--keep N` (default 7) prunes old snapshots. Blobs are never pruned, so a backup
  location only grows; clear it by hand if items were deleted and space matters.
- The backup location must be outside the data directory, ideally another disk or host.

## Commands

```bash
export CULTURE_LIBRARY_DATA_DIR=/srv/culture-library
export CULTURE_LIBRARY_BACKUP_DIR=/mnt/second-disk/culture-library-backup

culture-library backup run                      # or: --to DIR --keep 14
culture-library backup verify <snapshot-dir>
culture-library backup restore --from <snapshot-dir> --to <empty-dir>
```

`restore` refuses a non-empty target, checks the snapshot first, restores the database
and blobs, then re-verifies every item's and repo-document version's id, sha256 and
visibility against the manifest, and re-hashes every referenced blob. A snapshot taken
before repo documents were covered fails verify for the repo-document blobs it lacks.
Exit code 1 means a check failed or the target was not empty.

## Schedule

`backup schedule --print` prints a daily schedule and installs nothing:

```bash
culture-library backup schedule --print --kind systemd --data-dir /srv/culture-library --to /mnt/second-disk/culture-library-backup
culture-library backup schedule --print --kind cron --data-dir /srv/culture-library --to /mnt/second-disk/culture-library-backup
```

For systemd, save the two units from the output as
`culture-library-backup.service` and `.timer` in your systemd user unit
directory (see `man systemd.unit`), then
`systemctl --user enable --now culture-library-backup.timer`. Check it with
`systemctl --user list-timers` and by confirming a new snapshot each day.

## Restore drill (spark, 2026-10-24)

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
the HTTP server is under write load (the unit test holds an open write transaction, but
that is not a load test). Repeat the drill after the first real deployment.
