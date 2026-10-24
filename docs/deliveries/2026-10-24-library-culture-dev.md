# Delivery Summary — library.culture.dev

plan: `library-culture-dev` · run: `complete` · date: `2026-10-24`
baseline: `devague summary skeleton`

## Intent

The run had one aim: build the library behind library.culture.dev so it can
replace xteink's library at ebooks.culture.dev. The library holds:

- EPUB books, PDFs, newspapers and Markdown;
- recordings, as transcripts;
- Gwen's diary;
- every repo's specs, plans and devague records.

Culture-rules moves data in and out of it. xteink reads from it. Gwen reads,
cites and writes through MCP. People use an approved, pixel-tested site
behind Cloudflare Access.

The run executed the converged 21-task plan
`docs/plans/2026-10-24-library-culture-dev.md` in 7 waves. The split was
approved at gate 2 (`docs/plans/2026-10-24-library-culture-dev-split.md`).

"Complete" means every plan task merged. It does not mean every success
signal is met:

- **Waiting on other repos:**
  - the next newspaper arriving with no manual steps needs culture-rules#86;
  - Gwen's citations and diary need embodiment#97.
- **Waiting on the operator's provisioning:** the Cloudflare Access wall.
- **A validation finding:** search does not yet cover repo documents (#4).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Server extra and app skeleton
- `t2` — Store: SQLite WAL schema and content-addressed files
- `t3` — Frame and plan renderer for devague schema 1-6
- `t4` — Ingest: formats, caps and untrusted archives
- `t5` — Keys with scopes and Cloudflare Access verification
- `t6` — Change feed
- `t7` — Search: full-text and semantic with a background indexer
- `t8` — Repo documents: versioned identity, visibility and dedup
- `t9` — Migration from xteink
- `t10` — Library HTTP API, compatible with xteink
- `t11` — Send to device: publish the event and serve the reference
- `t12` — Status and search endpoints
- `t13` — MCP server for agents
- `t14` — OPDS and WebDAV for reading apps
- `t15` — Site: port the approved prototype
- `t16` — Visual parity tests against the reference screenshots
- `t17` — Backups and a restore drill
- `t18` — Deploy on spark
- `t19` — File the sibling-repo changes as issues
- `t20` — Teardown gate for ebooks.culture.dev
- `t21` — Delivery evidence and success signals

## Actual Delivery

All 21 tasks are accounted for. Each merged into `spec/library-culture-dev`
with a test run before and after the merge. One exception: t15 was merged on
top of a red suite left by t14's merge (lapse `l9`); it was fixed in `ac56b99`.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | Optional `[server]` extra and `create_app` with auto-discovered `routes_*.py`. `culture-library server serve`, default port 8796. Merge `72da375`; follow-up `d7eb6f5` added a bare-venv zero-deps test and moved the port off xteink's 8780. |
| `t2` | delivered | SQLite WAL store, `items` with visibility defaulting to private, content-addressed files, `register_schema` hook, explicit-id inserts. Merge `119a6ff`. |
| `t3` | delivered | `culture_library/render/devague.py` renders all 1,231 frame and plan files in `~/git` (schema 1–6 and unversioned). Merge `e3432a8`. |
| `t4` | delivered | Ingest of EPUB, PDF, Markdown, text and JSON. Audio is refused. Size cap and zip-bomb caps; refusals are recorded. Merge `3052e1d`. |
| `t5` | delivered | `clk_` hash-only keys with read, write and admin scopes plus kind limits. Access JWT verification with a CSRF header. Merge `ca3ca15`; follow-up `4147a1c` refuses Access service tokens. |
| `t6` | delivered | Change feed `GET /api/feed?since=` with a monotonic cursor and no catalog scan. Merge `d140481`. |
| `t7` | delivered | FTS5 and semantic search plus a background indexer with retries. Merge `dd3eab2`; completed by d4 (indexes item bodies without the gateway). |
| `t8` | delivered | Repo documents keyed by normalized remote and path, versioned by commit; private repos stay private; no git writes. Merge `034270f`. |
| `t9` | delivered | `culture-library migrate from-xteink`, a dry run unless `--apply`. Ids and sha256 preserved, idempotent. Merge `b03259f`. |
| `t10` | delivered | xteink-compatible `/api/library`. The culture-rules wire contract passes. Private items return 404; uploads without visibility are private; delete archives for 30 days, then the item is purged. Opaque 409 for private duplicates, except to the original uploader. Merge `37c82f7`. |
| `t11` | delivered | `POST /api/library/{id}/send` publishes `library.item.send_requested` through events-cli and serves the content by reference. Merge `b20a7e6`. |
| `t12` | delivered | `GET /api/status` and `GET /api/search`. Merge `cb3300a`; indexer lifespan wiring in `5894700`. |
| `t13` | delivered | `python -m culture_library.mcp` (stdio and LAN streamable-http): push, list, search, read, send and diary tools, each using the caller's own key. Merge `9b19239`. |
| `t14` | delivered | OPDS 1.2 at `/opds` and read-only WebDAV at `/dav/`, accepting HTTP Basic with the key as the password. Merge `cf2d97d`. |
| `t15` | delivered | Site at `/` ported from the approved prototype, with a strict CSP, text-only content, `dir=auto` and logical CSS. New read endpoints. Merge `90f366f`; parity fixes landed with t16. |
| `t16` | delivered | Opt-in visual suite (`visual` group, CI job): 30 views within 1% of `docs/design/reference/`, plus token, typography, layout and persistence checks. Lato is vendored. 18 references were re-approved (d3, d5). Merge `d821d76`; references in `eef8a86`. |
| `t17` | delivered | `culture-library backup run/verify/restore/schedule`. The restore drill on spark matched 17/17 (`docs/operations/backup.md`). Merge `f0de27d`. |
| `t18` | delivered | `deploy/` (Dockerfile, compose, `.env.example`) and `docs/operations/deploy.md`. Merge `e957756`. The image was built and booted on spare ports (`docs/evidence/t18-deploy-verification.md`): both services healthy in 11.4 s (6.1 s after a restart), health 200, `/api/library` without a key 401, site served with its CSP, full-text search and the working flow pass. events-cli is now in the image. Provisioning the hostname remains an operator step. |
| `t19` | delivered | culture-rules#86, embodiment#97 and ebooks-cli#2 opened, plus a comment on xteink#3, all after operator review. |
| `t20` | delivered | `docs/operations/teardown-ebooks.md`. The migration condition was reworded to count xteink's live total (`7048fe5`). |
| `t21` | partial | `docs/evidence/t21-delivery-evidence.md`, measured on spark. Passed: every kind, reader paths, 51/51 migration, 74 repos, full-text 1.7 ms median, 401. Pending: next newspaper, Access wall, c31. Merge `71f656a`. |

## Mid-work Decisions

- `d1` — The lobes gateway embedder client lives in
  `culture_library/gateway.py` (outside core/); core/search.py and
  core/indexer.py take the embedder as an injected callable — the
  network-free core guard test keeps `culture_library/core` network-free.
  The acceptance criteria are met with the client injected.
- `d2` — Add a 'keys' CLI noun (mint, list, revoke, overview) outside any
  plan task — t5 could not add a CLI verb, but the first admin key must come
  from the host.
- `d3` — Drop the prototype's 'arriving' spines and 'N arriving' count from
  the site design and re-approve the affected references — the library has no
  source for not-yet-ingested content. The operator chose this on
  2026-10-24; the decider leaned the same way (0.57).
- `d4` — Integration fixes outside the planned tasks, all from wave-4 reports
  (full-text could not reach EPUB/PDF bodies without the gateway; diary keys
  could not see their own entries; targets had no type):
  - the indexer fills full-text without the gateway;
  - an uploader key can read its own private items (decider 0.74);
  - send targets have a type label (decider 0.83);
  - the feed accepts `?item_id=`;
  - mcp is in the dev group.
- `d5` — 18 reference views re-approved instead of d3's six, and the phone
  send menu fixed — t16's pixel comparison found prototype-only content, and a
  reference that scrolled sideways. The operator approved all 18 on
  2026-10-24.
- `d6` — t21's remaining success signals are deferred outside this repo:
  `o45` (the next newspaper with no manual steps) to culture-rules#86 and
  `o47` (Gwen's citations and diary) to embodiment#97, both in progress
  elsewhere. The third, `o46` (the Access wall), has since passed (`e47`).
  The operator approved d6 on 2026-10-24.

Other decisions were taken with the decider and recorded in issue #3:

- Cloudflare Access service tokens are refused (0.80).
- `gemma2-embed` stays the default until quality is compared (0.78; follow-up
  #7).
- Uploads use FastAPI multipart parsing, not a hand-written parser (0.965).
- Duplicate bytes of a private item get an opaque 409 (0.749), with an
  exception for the original uploader (0.943).
- Visual tests are opt-in with a dedicated CI job (0.948), and Lato is served
  by the site itself (0.965).

The operator decided:

- programs reach the library on spark's local address (c44);
- diary deletion archives for 30 days, and this applies to every kind (c45);
- the library serves OPDS and WebDAV (c46);
- send to device ends at the event, and delivery belongs to culture-rules
  (c47, c52);
- the four sibling issues were posted after review.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t7` (`d1`) | The core network-free guard test keeps `culture_library/core` network-free; the acceptance criteria are met with the client injected. | acceptable |
| `t5` (`d2`) | t5 could not add a CLI verb (`cli/__init__.py` was owned by t9 that wave), but the first admin key must come from the host for `/api/keys` and deploy (t18) to work. | acceptable |
| `t15` (`d3`) | The library has no source for not-yet-ingested content (other systems push when they choose). The operator chose "drop and re-approve" on 2026-10-24 after the decider leaned the same way (0.57). | acceptable |
| `t7` (`d4`) | Wave-4 reports showed full-text search could not reach EPUB/PDF bodies without the gateway (blocking c30's full-text signal), diary keys could not see their own private entries, and the site could not show target types. The decider settled the two design questions. | acceptable |
| `t16` (`d5`) | t16's pixel comparison showed these views carry prototype-only content the real site cannot reproduce, and the reference send menu broke the no-sideways-scroll contract. | needs-follow-up |
| `t9` | The plan and c9/c30 said 17 items. xteink held 51 on 2026-10-24, because 33 recording EPUBs and more newspapers had arrived. All 51 migrated, but the recordings land as `document`, not `transcript` (#5). | needs-follow-up |
| `t18` | Built and booted on spare ports and verified (`docs/evidence/t18-deploy-verification.md`); events-cli added to the image. Two things wait outside the repo: the operator provisioning library.culture.dev, and a broker address the container can reach for send to device (#6). | acceptable |
| `t21` (`d6`) | Two success signals cannot be measured from this repo: the next newspaper (`o45`, culture-rules#86) and c31 (`o47`, embodiment#97), both in progress elsewhere. The Access wall (`o46`) passed once provisioned (`e47`). Validation also found that search does not cover repo documents (#4). | needs-follow-up |

## Evidence

- tests: `uv run pytest -n auto` at `7048fe5` — 656 passed, 3 skipped (the
  skips are the cross-repo note and the opt-in visual group).
- tests: `uv run pytest -m visual` at `eef8a86` — 52 passed (30 views within
  1%, plus token, typography, layout and persistence checks).
- per-claim test runs at `d821d76`: obligations `o8`–`o37` with evidence
  `e1`–`e30`, all pass.
- design obligations: `o1`–`o7` with evidence `e31`–`e37`, all pass.
- delivery evidence measured on spark: `e38`–`e44`, all pass (the t21
  measurements).
- failing evidence: `e45`, repo-document search (#4). Unchecked:
  - `o45`, the next newspaper with no manual steps;
  - `o46`, the Access wall;
  - `o47`, c31 (Gwen's citations and diary).
- behavioral deltas: `b1`–`b12`.
- lint at `7048fe5`: black, isort and flake8 clean; bandit reports no issues;
  `teken cli doctor . --strict` passes; `scripts/scan-secrets.py` is clean;
  markdownlint is clean on the new docs.
- commits: `3f94227..7048fe5` on `spec/library-culture-dev`, including 20
  task merges and the integration-fix merge.
- issues: decisions in #3; remaining work in #4, #5, #6 and #7; sibling repos
  culture-rules#86, embodiment#97, ebooks-cli#2 and the xteink#3 comment.

All evidence, obligation and delta records above were filed with
`--origin llm` and are still proposed for the operator to adjudicate.

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| xteink's `/api/library` contract is kept; culture-rules' real push works unchanged (c2) | high | test `tests/contract/test_culture_rules_push.py` · `e1` |
| Kinds and formats widened; audio refused; untrusted archives capped (c3, c38) | high | tests `tests/core/test_ingest.py`, `tests/web/test_read.py` · `e2`, `e20` |
| Scoped keys enforced; Access service tokens refused (c6) | high | tests `tests/server/test_auth.py`, `tests/core/test_keys.py` · `e5` · commit `4147a1c` |
| Private items hidden (404); missing visibility fails closed; uploader reads own (c7, c43) | high | tests `tests/server/test_library_api.py`, `tests/server/test_uploader_visibility.py` · `e6`, `e25` |
| No device code in the library (c5) | high | test `tests/contract/test_no_device_code.py` · `e4` |
| All xteink items migrate with ids and sha256 preserved (c9, c33) | high | test `tests/migrate/test_xteink.py` · `e8`, `e9` · measured 51/51 in `docs/evidence/t21-delivery-evidence.md` (`e41`) |
| Repo documents versioned, deduped, private-by-repo, never written back (c13–c15, c17, c18, c37) | high | test `tests/core/test_repodocs.py` · `e11`–`e17` · 74 repos measured (`e42`) |
| Frames and plans of schema 1–6 render (c16) | high | test `tests/render/test_corpus.py` (1,231 files) · `e14` |
| Concurrent writers without lock errors (c35) | high | test `tests/core/test_store.py::test_concurrent_writes_and_reads_no_lock_errors` · `e19` |
| Change feed without catalog scans (c34) | high | test `tests/core/test_feed.py` · `e18` |
| Full-text over item bodies in under 1 s without the gateway (c41, part of c30) | high | tests `tests/core/test_search.py` · `e23` · measured median 1.7 ms (`e43`) |
| Search covers the whole catalog, including repo documents (c28) | low | failing evidence `e45` — items only (#4) |
| Hebrew renders right to left; content is text-only under a strict CSP (c39, c38) | high | tests `tests/web/test_read.py`, `tests/web/test_static_rules.py` · `e21` |
| Backups restore ids, sha256 and visibility (c40) | high | test `tests/core/test_backup.py` · drill in `docs/operations/backup.md` · `e22` |
| Send to device ends at an event; rules fetch by reference; no device calls (c48–c52) | medium | tests `tests/core/test_send.py`, `tests/server/test_send_route.py` · `e26`–`e30` — no real broker publish yet (lapse `l8`, pending) |
| The site matches the approved design in 30 views and both themes (c32) | high | `tests/visual/test_parity.py` · `e35`–`e37`, `e31`–`e34` · references `eef8a86` |
| Served by compose behind cultureflare Access (c8) | medium | test `tests/test_deploy_files.py` · `e7` · built, booted and probed: healthy in 11.4 s, 401 without a key (`docs/evidence/t18-deploy-verification.md`). The Access wall is unverified until the hostname is provisioned. |
| ebooks.culture.dev teardown is gated (c42) | high | file `docs/operations/teardown-ebooks.md` · test `tests/test_teardown_gate.py` · `e24` |
| Every content kind exists in a running library (c1) | medium | `docs/evidence/t21-delivery-evidence.md` · `e38` (PDF, transcript and diary items synthetic) |
| The next newspaper lands with no manual step (c30) | unverified | needs culture-rules#86 — not claimed |
| An anonymous request to library.culture.dev gets the Access wall (c30) | high | measured live: 302 to the Access login (`docs/evidence/t21-delivery-evidence.md`, update) · `e47` |
| Gwen cites the library and keeps a daily diary (c31) | unverified | needs embodiment#97 — not claimed |

Lapse ledger evidence: approved lapses `l1`–`l3` bear on c13/c27 (repo
counts, re-measured by remote in t21) and c16 (schema range, corrected in c16
and h43). The confidences above already reflect them. Lapses `l4`–`l10` are
pending approval and are not yet evidence. They concern:

- tests not run red first (`l4`, `l5`);
- mocked timeouts and pandoc (`l6`);
- the embedding sample being a proxy, n=1 (`l7`);
- no real broker publish (`l8`);
- the t15 merge on a red suite (`l9`);
- the deploy never being built or started (`l10`).

## Remaining Work / Follow-up

- **`t21` / c28:** index repo documents for full-text and semantic search
  (#4).
- **`t9`:** map xteink's recording EPUBs to kind `transcript` when migrating
  (#5).
- **`t18`:** library.culture.dev is provisioned and live behind Access
  (`o46`, `e47`). Still open: give the container a reachable broker address
  for send to device (#6).
- **c30 next newspaper:** culture-rules points `library.push` at the local
  address and drops the queue step (culture-rules#86). Then watch 7
  consecutive issues (`o45`, teardown condition 2).
- **c31:** embodiment lifts c26 for diary writes and points Gwen at the local
  address (embodiment#97). Then measure the citations and the daily diary
  entry (`o47`).
- **xteink:** push or merge `feat/issue-1` and become a library client
  (xteink#3). This is teardown condition 1.
- **Embeddings:** compare `gemma2-embed` and Qwen3 on real queries before
  changing the default (#7).
- **ebooks.culture.dev teardown:** after all three gate conditions hold
  (`docs/operations/teardown-ebooks.md`).
- **Adjudication:** the operator reviews the proposed records:
  - evidence `e1`–`e45`;
  - deltas `b1`–`b12`;
  - obligations `o8`–`o47`;
  - lapses `l4`–`l10`.
