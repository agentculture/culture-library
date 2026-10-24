# t21 delivery evidence

Measured on spark on 2026-10-24 against a scratch library built from this branch
(`library/t21`, package version 0.11.0). Nothing here touched production data, the xteink
container's volume (read only copy), or any repo under `~/git` (read only `git` calls). Numbers
were measured, not copied from the frame (lapse l1), and one of them differs from the frame.

The scratch server ran on `127.0.0.1:18796` with `CULTURE_LIBRARY_DATA_DIR=/tmp/claude-1000/t21-lib`,
Access off. Four keys were minted for the run (a private write key, a public read key, a private
read key and a diary-only write key); none appear here and the scratch data was deleted after.

## Summary

| Claim | Status | Measured value | Date |
|-------|--------|----------------|------|
| c1 every kind | passed | book, newspaper, document (EPUB, PDF, Markdown), transcript, diary, spec, plan, devague-record all present | 2026-10-24 |
| c26 reader paths | passed (xteink client pending) | site 200 + CSP; API list/file/search; MCP list_library/search/read_item; file bytes match sha256 | 2026-10-24 |
| c27 before state | passed (re-measured) | xteink 51 items, Pocket 39 live recordings, 1545 tracked `.devague` files in 68 repos, hostname unresolved | 2026-10-24 |
| c29 operator reason | confirmed by the operator in the frame (h20) | not measured | n/a |
| c30 migration | passed | 51/51 sha256 (the frame's 17 is stale); re-run: 51 unchanged | 2026-10-24 |
| c30 next newspaper, 0 manual steps | pending | needs culture-rules#86 | n/a |
| c30 at least 70 repos | passed | 74 repos, 72 with devague records, 2269 documents | 2026-10-24 |
| c30 full text under 1 s | passed | median 1.7 ms, max 40.4 ms over 20 runs | 2026-10-24 |
| c30 Access wall | pending | `library.culture.dev` not provisioned (operator step, `docs/operations/deploy.md`) | 2026-10-24 |
| c30 401 without a key | passed | 401 without key, 200 with a read key | 2026-10-24 |
| c31 Gwen cites and writes diary entries | pending | until embodiment points at the library (embodiment#97); not passed | n/a |

## c1: an item of every kind

Added through the HTTP API (`POST /api/library`), the MCP `diary_write` tool, the migration and
`POST /api/repodocs`. Transcript, PDF, Markdown and diary content is synthetic; no Pocket
transcript was copied.

| Kind | Format | Id | Source |
|------|--------|----|--------|
| book | epub | 11 | migrated from xteink |
| newspaper | epub | 20 (Daily newspaper 2026-10-23) | migrated; 14 newspapers in all (ids 5, 8-10, 12-20, 54) |
| document | pdf | 55 | synthetic one-line PDF, text found by `pdftotext` |
| document | md | 56 | synthetic Markdown |
| transcript | txt | 57 | synthetic text |
| diary | md | 58 | MCP `diary_write` with a diary-scoped key |
| spec | md | repodoc 603 | `docs/specs/2026-10-23-library-culture-dev.md` of culture-library |
| plan | md | repodoc 601 | `docs/plans/2026-10-24-library-culture-dev-split.md` |
| devague-record | json | repodoc 599 | `.devague/frames/library-culture-dev.json` |

Item counts by kind afterwards (`GET /api/status`): book 1, diary 1, document 38, newspaper 14,
transcript 1 (55 items); repodocs: devague-record 1204, plan 585, spec 480 (2269 documents).

Finding: the migration maps 33 of xteink's Pocket derived EPUBs to kind `document`, not
`transcript` (the mapping in `migrate/xteink.py` turns the old `article` into `newspaper` only
for "Daily newspaper" titles). So "recordings as transcripts" is shown by the synthetic
transcript (id 57) and by the migrated recording EPUBs as documents, not as kind `transcript`.

## c30: migration 17/17

```text
culture-library migrate from-xteink --data-dir /tmp/claude-1000/t21-xteink/data \
    --dest /tmp/claude-1000/t21-lib --apply
APPLIED (visibility=private)
...                       (51 rows, all "copied")
51/51 items with matching sha256
(same command again)
...                       (51 rows, all "unchanged")
51/51 items with matching sha256
```

The source is a `docker cp xteink-api-1:/data` copy, opened read only. The xteink library now
holds 51 items, not 17: 33 recording EPUBs were pushed on 2026-10-24 and a 24 October newspaper
arrived. All 51 carry identical sha256, id and `created_at`, so the claim holds in substance,
but the literal "17/17" is stale. `docs/operations/teardown-ebooks.md` still says 17/17 and
the gate should read "all items" (report, not edited here).

Why `/data/files` holds more blobs than items: 56 blobs against 51 items. The 5 extra blobs
are the 5 xteink firmware images (table `firmware`, ids 1-5, about 5.8 MB each, no file
extension), which are named by sha256 like items. Every blob's name equals its content hash.
Firmware is not library content and the migration correctly ignores it.

## c30: repos and records

Pushed read only from the clones: committed `docs/specs/`, `docs/plans/`, `docs/deliveries/`
and `.devague/{frames,plans,deliveries,reviews,questions}/` at `HEAD` (`git ls-tree`,
`git show HEAD:<path>`, `git log`), with each clone's origin URL, `HEAD` and the
`origin/HEAD` (fallback `origin/main`) head. The `jetson-containers` symlink and `.worktrees*`
directories were skipped. Visibility came from
`gh repo list agentculture --limit 300 --json name,visibility` (129 repos, 17 PRIVATE): a
PRIVATE repo, a repo outside agentculture and a repo with no remote were pushed `private`.
The 3 remoteless repos use a `spark:/home/spark/git/<name>` identity.

```text
pushed 3093 files, 3093 created, 0 errors, 2 skipped as non text
GET /api/repodocs (private key):  2269 documents, 74 distinct repos
GET /api/repodocs (public key):   2035 documents, 57 distinct repos
repos with a devague-record:      72 (1204 records)
repos with a .devague/ path:      68 (997 documents)
re-push of culture-library and devague: 80 files, 0 created, 80 unchanged
```

Result: 74 repos are listed, above the 70 bar (72 with devague records). 3093 pushed against
2269 stored because several clones share one remote (for example `culture` and `culture-tools`
both point at agentculture/culture) and are one repo to the library; the re-push shows dedup.
Only 57 repos are visible without a private key. Search does not index repodocs (a full text
query for "devague" returns no item hits); search covers items only.

## c30: full text under 1 s

After the indexer finished (`GET /api/status`: `index.pending 0, done 55, failed 0`):

```text
GET /api/search?mode=fulltext&q=Embedl    x20, private read key
hits: 3, item 20 (Daily newspaper 2026-10-23) among them, snippet from the body
median 1.7 ms, max 40.4 ms, min 1.5 ms
corpus: 55 items (7.7 MB of xteink files plus 4 synthetic) and 2269 repodocs, 62 MB data dir
```

The synthetic words also hit their items: `quartzmarmot` gives id 55 (the PDF), `zebrafinch`
gives 56, `lanternfish` gives 57. The searched corpus is the 55 items; repodocs are stored but
not searched.

## c30: 401 and the Access wall

```text
curl -s http://127.0.0.1:18796/api/health                  -> 200 {"status":"ok","version":"0.11.0","api_version":"1"}
curl -s -o /dev/null -w '%{http_code}' .../api/library      -> 401
curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer <read key>' .../api/library -> 200
dig +short +tries=1 library.culture.dev                     -> resolver timed out (spark's local resolver is flaky)
dig library.culture.dev @1.1.1.1                            -> status: NXDOMAIN
getent hosts library.culture.dev                            -> exit 2, no entry
curl -sI https://ebooks.culture.dev/                        -> HTTP/2 302, location: https://agentculture.cloudflareaccess.com/...
```

Access wall: pending. Hostname not provisioned (operator step, `docs/operations/deploy.md`).
The reference behaviour is `ebooks.culture.dev`, which answers an anonymous request with a 302
to the Access login. A private item requested with a public read key answers 404, not 401.

## c26: each reader's working path

| Reader | Result |
|--------|--------|
| Site | `GET /` gives 200, `text/html`, title "Culture Library", with `content-security-policy: default-src 'self'; script-src 'self'; ...; frame-ancestors 'none'`. `GET /api/repodocs/1/rendered` gives 200. |
| API | `GET /api/library?limit=3` (list), `GET /api/library/{id}/file` (file) and `GET /api/search?mode=fulltext&q=...` (search) all work with a key. `/api/feed` lists the `created` events. |
| MCP | `python -m culture_library.mcp --transport stdio` over a real `mcp` stdio session lists 8 tools; `list_library` (kind newspaper), `search` ("Embedl", 3 hits) and `read_item` (id 58, diary text) returned results. |
| xteink pull | `GET /api/library/{id}/file` with a private read key returned bytes whose sha256 equals the item's for ids 20 (newspaper), 11 (book) and 55 (PDF). This is the path xteink will use. xteink itself is not yet a client of the library (xteink#3, pending). |

## c27: before state, re-measured

| Measure | Value | Command |
|---------|-------|---------|
| xteink items | 51 (not 17); 51 queue rows, 3 devices, 5 firmware images | read only sqlite on the container copy: `select count(*) from items` |
| Pocket recordings | 39 `meta.json`, 0 deleted, 0 merged, so 39 live | `find ~/.local/share/pocketsync/files -name meta.json`, then check `deleted_at`, `deleted_in_pocket`, `merged_at`, `merged_into` |
| `.devague` files | 1545 tracked files in 68 repos by distinct remote URL (77 clones have a `.devague` directory on disk) | `git ls-files .devague` per clone, remotes normalised (no `.git`, lowercase, http to https) |
| Clones under `~/git` | 172 top-level repos with a `.git`, 3 with no remote; 129 agentculture repos on GitHub, 17 private | `gh repo list agentculture --limit 300` |
| `library.culture.dev` | does not resolve (NXDOMAIN) | see above |

## c29

Confirmed by the operator in the frame (h20). Not measured.

## c31: pending

Gwen answers with at least one [n] citation and writes at least one private diary entry a day:
pending, not passed. It needs embodiment to point at the library (embodiment#97). The `diary_write`
path works (item 58 above, a synthetic entry from a diary-scoped key), but no Gwen entry or
citation has been observed, so nothing is claimed.

## Pending, with what unblocks each

| Item | Blocked on |
|------|------------|
| Next 05:00 newspaper lands via culture-rules with 0 manual steps | culture-rules#86 |
| Access wall on library.culture.dev | operator provisioning (`docs/operations/deploy.md`) |
| xteink as a client of the library | xteink#3 |
| Gwen cites and writes diary entries (c31) | embodiment#97 |
