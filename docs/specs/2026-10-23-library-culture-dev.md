# library.culture.dev

> library.culture.dev is Culture's one library: pipelines publish recordings, EPUB books, PDFs, newspapers and Markdown into it; it also hosts every repo's devague record — specs, plans and .devague content; Gwen (embodiment) reads, cites and remembers from it and keeps her diary there; it replaces ebooks.culture.dev, and xteink becomes a device-side client of it

## Audience

- The operator (owner, browsing and reading at library.culture.dev behind Cloudflare SSO), Culture pipelines run by culture-rules (push and pull through the API), mesh agents via MCP — first Gwen (embodiment), who reads, cites and writes her diary — and xteink, which pulls items for its e-readers
  - instruction: check the README and explain catalog name these four readers and which surface (site, API, MCP) each uses

## Before → After

- Before: Today the content is scattered: 17 EPUBs (newspapers, 3 documents, 1 book) sit in xteink's local-only library behind ebooks.culture.dev with kinds book/article only and no PDF; 39 Pocket recordings live only in pocketsync; ~2,000 devague records are spread over ~75 repos with no single place to read them; Gwen can read none of it and has no diary
  - instruction: re-derive from s1, s7, s13 and embodiment spec l.12 before export
- After: Opening library.culture.dev shows one catalog — books, newspapers, documents, PDFs, transcripts, Gwen's diary and every repo's specs, plans and devague records — grouped by day with kind filters, full-text and semantic search, and a reader for each kind; culture-rules pushes the daily newspaper and transcripts into it, xteink pulls from it, Gwen asks it questions over MCP and writes her diary, and ebooks.culture.dev is gone
  - instruction: demo walk: open site, search one phrase that appears only in a newspaper body, open a spec page, open a transcript, see a private diary entry only when signed in

## Why it matters

- The operator wants one place to read, search and talk with everything they have read, recorded and planned; Gwen can only remember and cite what she can reach, and the mesh's specs and plans are its institutional memory but are unreadable unless you open each repo
  - instruction: keep this aligned with embodiment spec 'Why it matters' l.17

## Requirements

- library.culture.dev keeps xteink's /api/library contract as a compatible subset (multipart POST file/title/author/kind with sha256 dedup 201/200; GET list ?q&kind&limit&offset, GET {id}, GET {id}/file; DELETE; {detail,code} errors) so culture-rules library.push and Gwen's planned client seam re-point by base URL, not rewrite — xteink docs/api.md + api/openapi.json (feat/issue-1), culture-rules node/actions/library.py, embodiment spec c12/c13
  - honesty: culture-rules' unchanged library.push (`base_url` re-pointed) and xteink's client succeed against culture-library: a contract test replays the xteink feat/issue-1 API calls (POST dedup 201/200, list, get, file, delete, {detail,code} errors) and passes
- The library widens xteink's item model: kinds beyond book/article (newspaper, document, transcript, diary, spec, plan, devague-record) and formats beyond epub/bmp/txt (PDF stored natively instead of 415 `pdf_not_supported`; Markdown and JSON stored as-is). No audio — recordings arrive as transcript text — xteink core/models.py kind enum + ingest 415
  - honesty: Uploading a PDF, a .md and a .json returns 201 and GET .../file returns the identical bytes; an audio upload is refused with a {detail,code} error naming the supported formats
- The library-generic half of xteink feat/issue-1 is cited (cite-don't-import) into culture-library: core/{library,store,models,ingest,keys,errors}.py, server/`routes_library.py`, server/{access,auth}.py, client.py, MCP `push_file`/`list_library`, /api/keys and the web UI; storage stays SQLite + content-addressed `files/<aa>/<sha256>` — xteink#3's extraction list
  - honesty: Cited code keeps a provenance header naming xteink feat/issue-1 commit + path, and the cited modules' tests are carried over and pass
- API keys gain scopes (at least read vs write, ideally per kind) because xteink's `xtk_` keys are unscoped full-operator keys and Gwen's diary needs a write key that cannot delete books or mint keys — xteink core/keys.py (hash-only, no scopes), embodiment spec c15
  - honesty: A read-scoped key gets 403 on POST/DELETE and on /api/keys; a write key scoped to kind=diary gets 403 when uploading or deleting any other kind; tests pin both
- Items carry visibility (private vs shared) so Gwen's diary and sensitive recordings are not browsable by every reader; embodiment CLAUDE.md:244-246 requires daemon writes default private, and pocketsync README routes recordings to shared destinations only when enrichment.filters.sensitive is present and low
  - honesty: A private item is absent from list, search results, get and file for any caller without private scope (404, not 403, so its existence does not leak), on the API, MCP and the site
- library.culture.dev is served by a docker compose stack on spark behind cultureflare 'remote-login setup --hostname library.culture.dev --service `http://<svc>:<port>` --allow `<owner>'` (Access SSO; dry-run then --apply; tunnel token sealed via --shushu), its own tunnel since cultureflare replaces a tunnel's whole ingress (xteink deviation d1); the server verifies Cf-Access-Jwt-Assertion itself — cultureflare `_remote_login`, xteink compose.yaml + docs/remote-access.md, pocketsync web/access.py
  - honesty: The stack restarts unattended after a host reboot within 120 s; anonymous browser → Access wall; /api without key → 401; the server rejects a request whose Cf-Access-Jwt-Assertion fails RS256/aud verification even when it reaches the origin directly
- The 17 live items in xteink's data volume (`xteink_xteink`-data, /data 36 MB: 16 article EPUBs, 1 book) migrate into the library with sha256 preserved, so Gwen's planned reconcile by (item id, sha256) does not purge and re-index everything — xteink core/store.py, embodiment spec c33
  - honesty: A migration report lists 17/17 xteink items with matching sha256 and kind remapped (Daily newspaper → newspaper), and is idempotent on re-run
- A server runtime needs third-party dependencies (xteink's server stack, pandoc for MD/HTML→EPUB), which breaks culture-library CLAUDE.md's 'runtime package has zero third-party dependencies' — the dependency set is a deliberate, recorded decision (likely an optional \[server\] extra) — culture-library pyproject dependencies=\[\], CLAUDE.md CLI architecture
  - honesty: The runtime package's CLI (culture-library whoami/doctor/explain...) still installs and runs with zero third-party deps; server deps live only in an optional extra, recorded in CLAUDE.md
- The library hosts each repo's devague record: docs/specs/\*.md, docs/plans/\*.md, docs/deliveries/\*.md and .devague/{frames,plans,deliveries}/\*.json + {reviews,questions}/\*.md — across ~75 repos in ~/git (≈2,000 .devague files: 651 frames, 578 plans, 363 reviews, 267 questions, 253 deliveries; largest are colleague 292, lobes-cli 216, culture-nodes 143), ~80 MB with docs
  - honesty: For a sample of ≥ 5 repos, every committed docs/{specs,plans,deliveries}/\*.md and .devague/{frames,plans,deliveries,reviews,questions}/\* pushed by the rule appears in the library with matching content
- Repo documents are mutable and versioned, unlike sha256-immutable books: identity is (repo, path), each version is pinned to a git commit sha, and the library shows the current version with history — a spec amended or a frame re-exported replaces, not duplicates
  - honesty: Pushing the same (remote, path) at a new commit creates a new version, not a new item; pushing the same commit twice is a no-op; history lists both commits
- Repo documents inherit their repo's visibility: 17 of 129 agentculture GitHub repos are PRIVATE (incl. culture-core, steward, devex, guildmaster, grant, cultureagent, agent-lifecycle, katvan) — their specs/plans are never shown to readers who could not read the repo
  - honesty: A document pushed from any of the 17 PRIVATE repos is private by default; a test pushes one from a repo marked private and asserts it is invisible to a shared-scope key
- Frame and plan JSON (`schema_version` 5 per devague docs/spec-contract.md) is rendered as readable pages — claims with status, honesty conditions, scope entries, open questions, plan waves — rather than served as raw JSON only; the raw file stays downloadable
  - honesty: Every frame/plan JSON with `schema_version` ≤ 5 in ~/git renders without error (a corpus test over the real files); unknown future schema renders raw with a notice instead of failing
- The library dedups repo documents by (remote URL, path, commit) so a culture-rules rule that reads ~/git — which holds 106 extra .devague dirs under .worktrees.\* and \*/.worktrees — cannot multiply frames
  - honesty: Pushing the same frame from `~/git/<repo>` and `~/git/.worktrees.<repo>/<branch>` at the same commit yields one version

## Honesty conditions

- Every content kind named in the announcement (recording transcript, EPUB, PDF, newspaper, Markdown, diary, spec, plan, devague record) has at least one real item in the library at ship time — not just a schema slot
- culture-library has no device keys, device routes, queue or firmware tables — a grep/test over `culture_library`/ finds none — and xteink's queue works by pulling from the library API
- culture-library contains no git write, no repo checkout mutation and no devague invocation — a grep/AST test pins it
- Each named reader has a working path on ship day: operator via site, culture-rules via API, Gwen via MCP, xteink via API pull
- The numbers (17 items, 39 recordings, ~2,000 records, ~75 repos) are re-measured on the day of export, not copied from this frame
- Each after-state sentence maps to a test or evidence item in the plan; none ships unverified
- The operator confirms this states their reason (user-owned)
- Each number is measured on spark, recorded in docs/evidence/, and the 1 s full-text bound holds at the full corpus size
- Measured end-to-end through embodiment once its seam points at library.culture.dev; until then recorded as pending, not passed

## Success signals

- After migration all 17 xteink items are in the library with identical sha256 (17/17), the next 05:00 newspaper lands via culture-rules with 0 manual steps, ≥ 70 repos' devague records are listed, a full-text query returns hits from a newspaper body in < 1 s, and an anonymous request to library.culture.dev gets the Access wall while an API call without a key gets 401
  - instruction: measure each number in a delivery evidence file; the 1 s bound is measured on spark with the full corpus indexed
- Gwen answers a typed question with ≥ 1 \[n\] citation that opens the library reader at the cited passage, and writes ≥ 1 diary entry per day that is visible only to private-scoped readers
  - instruction: verify against embodiment's planned seam (c12/c13 there) once it points at library.culture.dev

## Scope / boundaries

- The e-reader side stays in xteink and is not built here: device keys (`xtd_`), device protocol v1, per-device queue, mirror, firmware tables, MCP `send_to_device`, USB CLI, avahi, and the xteink.culture.dev tunnel; xteink becomes an HTTP client of library.culture.dev — xteink#3 'xteink keeps the device side', xteink `routes_device.py`/core/devices.py
- The library never writes back to repos and does not change devague: devague stays deterministic, offline and local-only (devague issue #20, spec-contract 'All operations run fully offline against local .devague/ state'); the library is a reader/mirror of committed state

## Non-goals

- No retirement of pocketchat.culture.dev and no change to pocketsync's sync/enrichment/vector search this cycle: pocketsync stays the landing zone; the library consumes from it — pocketsync README non-goals ('landing zone', consumers pull) and embodiment spec non-goal on pocketsync

## Assumptions

- Changes to sibling repos (culture-rules library.push generalization, a pocket→library rule, xteink as client, embodiment's c26 write boundary, ebooks.culture.dev teardown) are filed as issues via the communicate skill, not edited from this repo — CLAUDE.md siblings + embodiment c26 'asks are drafted for the operator's go-ahead'

## Scope exploration

- `s1` — `../xteink feat/issue-1 (docs/api.md, api/openapi.json, core/store.py)`: Live library behind ebooks.culture.dev: /api/library CRUD with sha256 dedup, kinds book|article only, formats epub/bmp/txt (MD/HTML→EPUB via pandoc, PDF 415), 50 MiB cap + zip-bomb checks, SQLite + `files/<aa>/<sha256>`; 17 items / 36 MB; GET /api/library is 401 without an `xtk_` key
  - seeds: `c2`, `c3`, `c9`
- `s2` — `../xteink#3 + ../ebooks-cli`: xteink#3 (open) splits library→ebooks-cli, device side stays; ebooks-cli is a bare template scaffold with no library code — the destination is unclaimed and culture-library can take it
  - seeds: `c4`, `c5`, `q1` (question, resolved)
- `s3` — `../xteink compose.yaml + docs/remote-access.md`: Two tunnels: ebooks.culture.dev→api:8780 (Access SSO allow-list) and xteink.culture.dev→api:8781 (--no-access, device keys); server verifies Cf-Access-Jwt-Assertion itself; MCP :8782 LAN-only
  - seeds: `c5`, `c8`
- `s4` — `../xteink core/keys.py + server/auth.py`: `xtk_` API keys are hash-only bearer tokens with no scopes — any key can delete items and mint keys
  - seeds: `c6`
- `s5` — `../culture-rules node/actions/library.py + docs/rules/newspaper`: library.push is the only publisher today: daily 05:00 Asia/Jerusalem newspaper EPUB, kind=article, POST /api/library then POST `/api/devices/<id>/queue`, Bearer grant:`XTEINK_PUSH_API_KEY`; `base_url` is actor data (config change) but paths, kind and the queue step are code
  - seeds: `c2`, `q4` (question, resolved), `c10`
- `s6` — `../culture-rules docs/rules/pocket-transcribe (audio.transcribe)`: Transcription writes only local `transcript.<model>.json` under `output_dir/<audio_sha256>/`; nothing pushes transcripts to any library — a new step/action is needed
  - seeds: `q3` (question, resolved), `c10`
- `s7` — `../pocketsync (sinks/local.py, events.py, web/access.py, README data contract)`: Landing zone by design: Mongo docs + files tree `<YYYY>/<MM>/<id>/{meta.json`,transcript.\*,summary.md,audio.m4a} (39 recordings, ~103 MB audio); outbound only events.jsonl / MQTT; sensitive filter gates shared destinations; no library/ebooks references; access.py is a reusable stdlib Access verifier
  - seeds: `c11`, `c7`, `q3` (question, resolved)
- `s8` — `../embodiment docs/specs/2026-10-23-gwen-is-pocketchat-transcripts-as-knowledge.md`: Exported+challenged spec (no code, plan has 0 tasks) already names culture-library at library.culture.dev as the knowledge home (c30), binds via a GET-only seam on xteink's contract (c12/c13/c15), reconciles by (id, sha256) (c33), and confirms boundary c26 'Gwen never writes to the library' — a diary in the library reverses c26; v4 parks culture-library's contract/auth as unknown
  - seeds: `c2`, `c9`, `q2` (question, resolved), `c10`
- `s9` — `../embodiment embodiment/memory.py + CLAUDE.md:244-246`: Gwen's memory is eidetic (files backend, gwen-ingest private scope); no diary concept exists; daemon writes must default private
  - seeds: `q2` (question, resolved), `c7`
- `s10` — `../cultureflare _remote_login + cultureflare-write skill scripts`: remote-login setup provisions tunnel+DNS+Access (dry-run default, --shushu seals tokens); teardown by hostname; redirects only via cf-redirect-create.sh (zone Single Redirect, non-additive, needs a proxied DNS record)
  - seeds: `c8`, `q6` (question, resolved)
- `s11` — `../eidetic-cli (memory/record.py, embed.py, README)`: Records are short text+metadata, no blobs or chunking, embeds all candidates per recall — fits a diary, not book-body search
  - seeds: `q2` (question, resolved), `q5` (question, resolved)
- `s12` — `culture-library (pyproject.toml, culture_library/, CLAUDE.md)`: Scaffold only: CLI introspection verbs, dependencies=\[\], no server/compose/deploy; CLAUDE.md mandates zero runtime deps and 'spec it before building'
  - seeds: `c12`, `c4`
- `s13` — `~/git/*/.devague + docs/{specs,plans,deliveries} (inventory)`: ~75 repos carry devague records: ~2,000 .devague files (651 frames, 578 plans, 363 reviews, 267 questions, 253 deliveries) plus exported md; mostly tracked, but reachy-mini-cli/culture-guide/notion-agent/agentculture-cli/reachy-lobes gitignore .devague and many repos have untracked in-progress frames (e.g. embodiment's drafting plan)
  - seeds: `c13`, `q7` (question, resolved)
- `s14` — `~/git/.worktrees.* and */.worktrees`: 106 extra .devague directories live in git worktrees — a naive crawl duplicates them
  - seeds: `c18`
- `s15` — `gh repo list agentculture (visibility)`: 129 repos: 112 PUBLIC, 17 PRIVATE (guildmaster, mjolnir-agent, cultureagent, jetson-containers-agent, katvan, rollout-cli, steward, culture-agent-template, reterminal-cli, grant, devex, operator-cli, agtag, culture-guide, culture-core, agent-lifecycle, identity-cli)
  - seeds: `c15`, `q9` (question)
- `s16` — `../devague docs/spec-contract.md`: Frames/plans/deliveries are versioned JSON (`schema_version` 5) on a deterministic, offline-only CLI; export renders spec-md with a Scope exploration section — the library reads these files, it cannot ask devague to do anything networked
  - seeds: `c16`, `c17`, `c14`
- `s17` — `~/git/agentculture-cli remote`: origin points at github.com/agentculture/culture-guide.git — a local clone's directory name is not a reliable repo identity; key repo docs by remote URL
  - seeds: `c14`

## Decisions

- culture-library replaces ebooks-cli's intended purpose: it is the destination of xteink#3's library split (storage, ingest, library API, OPDS/WebDAV, MCP `push_file`/`list_library`); ebooks-cli is superseded (operator, 2026-10-24)
- Gwen's diary lives in the library as private diary items written by Gwen with a scoped write key (operator, 2026-10-24)
- Recordings are text only in the library (transcripts); audio stays local, moving to S3 in future — the library stores no audio (operator, 2026-10-24)
- culture-rules does all data movement into and out of the library; the library is agnostic and exposes push and pull through its HTTP API, an MCP server for agents, and a website for humans (operator, 2026-10-24)
- xteink reads from the library (pull); the device queue stays in xteink (operator, 2026-10-24)
- The library provides both full-text and semantic search (operator, 2026-10-24)
- ebooks.culture.dev is torn down eventually, after library.culture.dev serves its content (operator, 2026-10-24)
- The site UI follows the approved prototype (<https://claude.ai/artifact/2z94SGy76vPeWT1jUGQafz>, v3) as documented in docs/design/library-site.md: cream light / dark-blue dark themes with a toggle, Lato, forest-green accent, full-width layout, card home with continue-reading, reader with a collapsible filterable list pane (drawer on phones), upload button, and a site-state page with validated charts (operator, 2026-10-24)

## Hard questions

- Does culture-library supersede ebooks-cli as xteink#3's split destination (xteink#3 names ebooks-cli, a bare scaffold; README 'private self-hosted ebook library (API, OPDS, MCP)'), and is ebooks-cli then archived? (resolved: Yes — culture-library takes over ebooks-cli's intended purpose (private self-hosted library: API, OPDS, MCP) and is xteink#3's split destination (operator, 2026-10-24))
- Gwen's diary: stored in the library as private kind=diary items (needs scoped write key c6 + visibility c7, and reverses embodiment's confirmed boundary c26 'Gwen never writes to the library' + h14/AST test), or kept in eidetic (short scoped records with supersede/links fit a diary per eidetic-cli Record) with the library only serving a read view? (resolved: Diary items: Gwen stores her diary in the library as diary items (operator, 2026-10-24) — needs c6 scoped write key + c7 private visibility, and an embodiment ask to lift c26 'never writes')
- Search scope: metadata-only like xteink (q matches title+author) with consumers (Gwen, pocketsync) building their own vector indexes, or full-text/semantic search inside the library? eidetic does not scale to book bodies (no embedding cache, embeds all candidates per search) (resolved: Full-text and semantic search, both inside the library (operator, 2026-10-24))
- Retire ebooks.culture.dev by 'cultureflare remote-login teardown' or keep a 301 Single Redirect to library.culture.dev (needs a proxied DNS record kept after teardown; cf-redirect-create.sh refuses if the culture.dev zone already has a redirect ruleset)? (resolved: ebooks.culture.dev is torn down eventually (no long-lived redirect) (operator, 2026-10-24))
- culture-rules library.push hard-codes the xteink device-queue second step (/api/devices/{id}/queue, `newspaper_device` var) and kind=article: does the library accept-and-forward queueing to xteink, or does library.push become push-only with xteink pulling (needs a culture-rules change)? (resolved: xteink reads from the library (pulls); the library has no device queue and does not forward; culture-rules does the data movement (operator, 2026-10-24))
- Recordings: does the library store the Pocket audio (.m4a, ~103 MB / 39 recordings today) and transcripts itself, pulled from pocketsync's events.jsonl / MQTT events/pocketsync/recording/enriched + files tree, or only a transcript document (EPUB/MD) pushed by a new culture-rules step after audio.transcribe (which today writes only local `transcript.<model>.json`)? (resolved: Text only: recordings enter the library as transcript text; audio stays local (S3 in future), not in the library (operator, 2026-10-24))
- Source of truth for repo docs: committed state on each repo's default branch (GitHub, includes repos not cloned here, excludes in-progress work) or the local ~/git working trees (includes untracked frames like embodiment's drafting plan, and gitignored .devague in reachy-mini-cli 125 files, culture-guide, notion-agent, agentculture-cli, reachy-lobes)? (resolved: Deferred to culture-rules: which tree a rule reads (GitHub default branch vs ~/git) is the rule's choice; the library accepts either, keyed by remote URL + path + commit (follows from the q8 decision, operator 2026-10-24))
- Push or pull: does each repo publish on merge (a CI step / guildmaster-broadcast skill, i.e. a change in ~75 repos), does a culture-rules rule fire on push events, or does the library crawl GitHub/~/git on a schedule (no repo changes)? (resolved: culture-rules moves the data; the library is source-agnostic and accepts push (API/MCP) and serves pull (API/MCP/site) — no crawler or repo hook inside the library (operator, 2026-10-24))
- Visibility model: map GitHub repo visibility to library visibility automatically (private repo → private item), and who may read private ones — operator only via Access, or mesh agents with scoped keys?

## Open parks

- [unknown_nonblocking] xteink feat/issue-1 is local-only (unpushed; xteink#3 asks for it to be pushed/merged) — the code culture-library would cite exists only on this host and may move under it
- [unknown_nonblocking] Transcript EPUB format is unseen (embodiment v3; no transcript in the 2026-10-24 catalog) — whether segment start/end/speaker survive decides if the library reader can jump to a moment like pocketchat does
