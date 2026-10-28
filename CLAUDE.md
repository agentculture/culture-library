# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`culture-library` is the AgentCulture mesh agent for **Culture's library of
your own content and media** — recordings, books, newspapers, articles, EPUBs,
PDFs, docs and more. The goal is one catalog to ingest, index, search and serve
it all, published at **library.culture.dev**.

**Status: built and live.** library.culture.dev has served the library
behind Cloudflare Access since 2026-10-24, from the compose stack on spark
(`deploy/compose.yaml`). Spark runs 0.17.0 as of 2026-10-25, with
embeddings and the reranker on. Machine clients reach it on spark's loopback
(`127.0.0.1:8796` for the API and site, `:8797` for MCP).

- **Spec, plan and evidence.** The library was specified with devague:
  - spec: `docs/specs/2026-10-23-library-culture-dev.md`;
  - plan: `docs/plans/2026-10-24-library-culture-dev.md`;
  - the run's record: `docs/deliveries/2026-10-24-library-culture-dev.md`.

  It replaces xteink's ebooks.culture.dev library and ebooks-cli's intended
  purpose.
- **Code layout.** All library code lives under `culture_library/`:
  - `core/`: stdlib-only and network-free. It holds the SQLite WAL store,
    ingest, keys, feed, search and indexer, repo documents, library rules,
    send events, backup, and the uploaders table.
    Kinds are `book`, `article`, `newspaper`, `document`, `transcript`,
    `diary`, `spec`, `plan`, `devague-record`, `todo` (Markdown
    checklists, one per recording) and `discord-message` (one saved Discord
    message as Markdown; a `discord-react` sibling is not reserved yet). Adding a kind widens the `items` kind
    CHECK; the store rebuilds the table once on first open (see
    `docs/operations/deploy.md`, "Upgrading across a schema change").
  - `server/`: the FastAPI app and its `routes_*.py` modules. These serve
    `/api/library`, `/api/repodocs`, `/api/search`, `/api/status`,
    `/api/feed`, `/api/keys` and send, plus `/opds`, `/dav/` and the site
    at `/`. `PUT /api/library/{id}/file` replaces an item's bytes in place
    (same id, title, author, kind and visibility; new sha256, size and
    format) and appends an `updated` feed event.
  - `server/` also serves the RAG and eval surface (spec
    `docs/specs/2026-10-25-rag-endpoints-ragas-evals.md`):
    - `POST /api/rag/retrieve` (`routes_rag.py`) takes `{query, k, mode:
      semantic|hybrid, kind?, item_ids?, rerank?, candidates?}` and returns
      ranked passages `{item_id, title, kind, chunk_no, chunk_sha, text,
      score, rerank_score, source}` plus a `reranked` flag. It needs `read`
      scope, applies the same private/owner visibility as `/api/search`, and
      returns 503 `rag_unavailable` without an embedder. An unreachable
      reranker degrades to the unreranked order, never a 5xx.
      `GET /api/rag/items/{id}/chunks` returns an item's chunks.
    - `PUT`/`GET /api/library/{id}/sources` (`routes_sources.py`, `write` /
      `read` scope) store and return the newspaper sources sidecar: a JSON
      manifest `[{article_title, source_urls[], excerpt}]` that culture-rules
      pushes with each issue (in the `item_sources` table; cascades on item
      delete).
    - `POST`/`GET /api/eval/sets` and `/api/eval/runs`, `GET .../{id}`
      (`routes_eval.py`; writes need `write` scope, reads follow item
      visibility) store generated sets and scored runs. Sets pin the item
      sha256 and anchor references by `chunk_sha`, not `(item_id, chunk_no)`,
      because re-indexing renumbers chunks. A run records the server-reported
      retrieval config, and is stored `complete`, `partial` or `failed`;
      aggregates exist only on `complete`.
    - `/api/status` carries a `retrieval` block (embed model and prompts,
      chunker `max_chars`, reranker model, stored chunk count).
  - `mcp/`: the agents' MCP server; its `rag_retrieve` tool mirrors
    `POST /api/rag/retrieve`.
  - `eval/`: the RAG evaluation package, outside `core/` so `core/` stays
    network-free. It holds a framework-neutral adapter interface and models,
    a `LibraryClient` over the HTTP API, the OpenAI-compatible judge config
    (`config.py`; defaults to the lobes gateway, and refuses to send text from
    a non-public item to a non-local judge), the set generator, the runner,
    and the one shipped adapter, RAGAS (`ragas_adapter.py`).
  - `cli/_commands/eval.py`: `culture-library eval {overview,generate,run,
    show}`. `generate <item_id>` builds a set from a book's own text or a
    newspaper's sources sidecar; `run <set_id>` scores retrieval (context
    precision/recall) under a chosen `--mode`, `--k`, rerank setting and
    `--no-llm` (ID metrics only), so two runs differ only in retriever
    config. `LOBES_EMBED_PROMPTS` and `LOBES_RERANK_MODEL` configure the
    gateway prompts and reranker (see `docs/operations/deploy.md`, "Enable
    embeddings"). The judge defaults to the gateway's `cortex` role;
    `--judge-url`, `--judge-model` and `--judge-max-tokens` (or the
    `CULTURE_LIBRARY_EVAL_LLM_*` env vars) point it elsewhere. If LLM scoring
    fails, the run is still stored `complete` with its ID aggregates, and
    `config.llm_metrics_failed` records the failure. Book sets never sample
    the Project Gutenberg header or licence, which the generator finds by
    their `*** START`/`*** END` markers, not the phrase. The licence is still
    indexed and retrievable (planned: #27, strip it at index time). A generated
    question that points at its source ("the passage", "the author") is
    retried once, then skipped, because a search engine can't answer it.
  - `web/static/`: the site. A `todo` item opens as a read-only checklist
    (`checklist.js`).
  - `render/`: the devague renderer.
  - `migrate/`: the xteink migration.
  - `cli/_commands/deploy.py`: `culture-library deploy restart` recreates the live
    `library` and `mcp` containers from the host env file
    (`$HOME/.config/culture-library/live.env`), after refusing a missing or placeholder
    setting, then checks health and the send targets.
- **Site design.** `docs/design/library-site.md` is the contract. 30
  operator-approved screenshots in `docs/design/reference/` back it, and the
  opt-in visual tests check against them.
- **Deploy.** The files are in `deploy/`, with runbooks in `docs/operations/`.
- **Embeddings and the eval baseline (live).** Embeddings were enabled on
  2026-10-25 with the "Enable embeddings" runbook step in
  `docs/operations/deploy.md`: `gemma2-embed` with the `Retrieval-document`
  and `Retrieval-query` prompts, plus the gateway's `reranker` role.
  `deploy restart` injects `GATEWAY_API_KEY` from grant, so `live.env` stays
  secret-free (deviation d2). The baseline is stored on live (numbers on #7):
  - eval sets 1–5 for books 68, 66, 63, 71 and 70 (20 samples each), and
    sets 6–8 for newspapers 81, 98 and 73 (4 samples each, from the
    culture-rules sources sidecar);
  - every set has a complete run with rerank on and off at k=5, and an
    LLM-scored run (cortex judge);
  - ID recall@5 averages 0.89 with rerank and 0.78 without on books, and
    1.00 with rerank on newspapers.

  The run's delivery summary is
  `docs/deliveries/2026-10-25-rag-endpoints-ragas-evals.md`.
- **Not done yet:**
  - search does not cover repo documents (#4);
  - retrieval is too slow for Gwen's turn path, at about 0.7 s without rerank
    (#25);
  - in-progress items (#22);
  - the Project Gutenberg header and licence are still indexed and
    retrievable (#27);
  - the sibling repos must adopt the library (culture-rules#86,
    embodiment#97, embodiment#107, xteink#3);
  - #6 is still open for what is left of the deploy check.

**Before-state evidence for RAG and evals.** Before this work,
`core/search.py`'s `Hit` was item-level with a 200-char snippet, so it could
not hand an agent a citable passage. `docs/evidence/t7-embedding-cost.md`
measured embedding cost and throughput only; it did not evaluate retrieval
quality. Issue #7 (`gemma2-embed` versus Qwen3) therefore had no way to be
decided. `/api/search` keeps its shape (`Hit` is unchanged); RAG is additive.

Keep this file honest as features land: describe what is on disk, and mark
anything ahead of reality as planned.

Because the scaffold was produced by an identifier rename of the template, some
prose still describes `culture-library` as "a clonable template" — the CLI
parser description, the `explain`/`learn`/`overview` text, comments such as
`whoami.py`'s "When you clone this template", and the `docs/harness-*.md` /
`docs/automation-contract.md` history pages. That is inherited wording, not a
statement of intent; fix it opportunistically (tests assert on headings like
`# culture-library`, not on that wording).

Siblings in the Organic Development framework:
[`guildmaster`](https://github.com/agentculture/guildmaster) (skills supplier),
[`steward`](https://github.com/agentculture/steward) (alignment — `steward
doctor`), and [`teken`](https://github.com/agentculture/teken) (the `afi-cli`
"Agent First Interface" scaffolder this CLI is cited from).

## Commands

```bash
uv sync                                         # set up (Python >=3.12, dev group included)
uv run pytest -n auto                           # full suite (what culture.yaml's gate runs)
uv run pytest tests/test_cli.py::test_whoami_json -v   # a single test
uv run pytest -n auto --cov=culture_library --cov-report=term   # coverage (fail_under = 60)

# Lint — the same checks the CI `lint` job runs:
uv run black --check culture_library tests
uv run isort --check-only culture_library tests
uv run flake8 culture_library tests
uv run bandit -c pyproject.toml -r culture_library
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken" "#.venv"
python3 scripts/scan-secrets.py                 # committed secrets / non-localhost endpoints
uv run teken cli doctor . --strict              # agent-first rubric gate

# Harness + identity checks:
uv run python scripts/harness-smoke.py --stage config   # offline per-harness config check
uv run python scripts/harness-smoke.py --stage all --require config   # what CI runs
uv run culture-library doctor                   # prompt-file-present / backend-consistency
```

Formatting is black + isort (profile black), line length 100 everywhere;
flake8 ignores E203/W503 to agree with black.

## CLI architecture

The package is `culture_library/` (no `src/` wrapper); the console script is
`culture-library = culture_library.cli:main`. The CLI is cited
(cite-don't-import) from teken's `python-cli` reference, so the **runtime
package has zero third-party dependencies** (`culture.yaml` is parsed by hand
in `whoami.py`; PyYAML is dev-only, used by tests and `scripts/`). Adding a
runtime dependency for library features is a deliberate decision, not a
default.

The pieces that span files:

- **Registration** — `cli/__init__.py:_build_parser()` imports each module in
  `cli/_commands/` and calls its `register(sub)`. A new noun group is a new
  module with a `register()` function added there; the subparsers use
  `parser_class=_CliArgumentParser`, and nested noun subparsers must propagate
  it (`parser_class=type(p)`, as `_commands/cli.py` does) so parse errors stay
  structured.
- **Error contract** — handlers return `None`/an int, and raise
  `cli._errors.CliError(code, message, remediation)` on failure. `_dispatch()`
  wraps any other exception into a `CliError` so no traceback ever reaches
  stderr. Text errors render as `error: …` + `hint: …` (the `hint:` line is
  required by the rubric). Exit codes: `0` success, `1` user error, `2`
  environment error, `3+` reserved.
- **Output contract** — results go to stdout and diagnostics/errors to stderr,
  never mixed (`cli/_output.py`: `emit_result`, `emit_error`,
  `emit_diagnostic`). Every command accepts `--json`; `main()` pre-scans raw
  argv for `--json` so even argparse-level errors honour it.
- **`explain` catalog** — `explain/catalog.py:ENTRIES` maps command-path tuples
  to markdown. Every registered noun/verb should get an entry, and `learn`
  output should mention it.
- **Rubric shapes** — `overview`/`cli overview` emit `{subject, sections}`;
  `doctor` emits `{healthy, checks: [{id, passed, severity, message,
  remediation}]}`; descriptive verbs never hard-fail on a bad target. Any noun
  with action verbs must also expose an `overview` verb
  (`teken cli doctor --strict` checks this).

**The `[server]` extra (decision).** The HTTP server lives in
`culture_library/server/` and its dependencies (fastapi, uvicorn, `mcp>=1.2,<2`,
python-multipart) are an optional `[server]` extra in `pyproject.toml`; base
`dependencies` stays empty and the CLI must import and run without the extra
(`tests/test_zero_deps.py` enforces it). Import fastapi/uvicorn only inside
`culture_library/server/*` and lazily in `cli/_commands/server.py`'s `serve`
handler, which raises a `CliError` with an "install culture-library[server]"
hint when they are missing. `create_app` (`server/app.py`, cited from xteink
`ea2f643`) auto-includes the `router` of every `server/routes_*.py`, so a new
route module needs no edit to `app.py`. Run it: `culture-library server serve
--host 127.0.0.1 --port N`.

**The `[eval]` extra (decision).** RAG evaluation is a second optional extra:
`eval = ["ragas==0.4.3", "openai>=1", "langchain-community<0.4.2"]` (the
`langchain-community` cap is deviation d1: 0.4.2 removed a module ragas 0.4.3
imports). Base `dependencies` stays empty, the production image still installs
only `.[server]`, and no CI job installs `[eval]` by default. ragas and openai
are imported lazily inside the `eval` CLI handler, which raises a `CliError`
with an "install culture-library[eval]" hint when they are missing;
`tests/test_zero_deps.py` blocks the eval modules from the zero-dependency
import. Eval tests run on fakes (`tests/eval/conftest.py`); real ragas runs are
opt-in. Install: `uv sync --extra eval`.

`doctor` keeps two backend→prompt tables on purpose: `_PROMPT_FILE` (every file
*recognized* under a backend name) and `_RESIDENT_PROMPT` (the one file the
Culture daemon reads). Only the resident prompt satisfies
`prompt_file_present`. `_PROMPT_FILE` mirrors
`.claude/skills/agent-config/data/backend-fingerprints.yaml`, and
`tests/test_harness_registries.py` fails if they drift apart.

## Identity and harnesses

`culture.yaml` declares the mesh identity (`suffix: culture-library`,
`backend: claude`), so **this file is the mesh resident's prompt**. It also
carries a `gate` section (`setup`/`test` argv lists) that external tooling
runs. Don't rename the suffix casually: `whoami`, `doctor`, the `cicd`/
`communicate` signatures (`- culture-library (Claude)`), and the eidetic
memory scope all resolve from it.

Four harnesses work over this one clone, each reading exactly one root file.
There is deliberately **no `AGENTS.md`**, and the config smoke check fails if
one appears, because it would shadow the Pi/colleague cascades:

| Harness | Reads |
|---------|-------|
| Claude Code | `CLAUDE.md` (this file) |
| Pi / `associate` | `AGENTS.override.md` (context) + `.pi/SYSTEM.md` (system prompt; read/find/summarize only, no repo writes) |
| colleague | `AGENTS.colleague.md` |
| Qwen Code | `QWEN.md` |

Keep the other three files in step with this one when the project's purpose
or conventions change. They are short on purpose; don't paste this file into
them. Two kinds of content in them are load-bearing:

- `.pi/SYSTEM.md`, `.qwen/skills`, `.pi/skills` and `.colleague/skills` (the
  last three are relative symlinks onto `.claude/skills`) are checked by
  `scripts/harness-smoke.py`.
- The live probes in `docs/harness-invocations.yaml` (edit only there) ask
  whether `CLAUDE.md`/`QWEN.md` "describe this project as culture-library",
  and whether `.pi/SYSTEM.md` says its role mirrors lobes-cli's `associate`.
  Keep those facts true.

"Which harness am I running" (whichever binary you invoke) and "which backend
is the mesh resident" (`culture.yaml`, changed only by `guild harness use`) are
separate selections. Never force a harness by editing tracked files. See
`docs/harness-selection.md` and `docs/automation-contract.md`.

## Skills

`.claude/skills/` holds the guildmaster skill kit, **vendored verbatim**
(cite-don't-import). Don't reformat or edit those files. A fix goes upstream
and is re-synced (`docs/skill-sources.md` has provenance and the procedure;
`markdownlint` and Sonar exclude the tree). The ones you'll use most:

- `version-bump`, `cicd` (PRs via `devex pr` + SonarCloud gating),
  `run-tests`, `sonarclaude`.
- `communicate` (sibling-repo issues via `agtag`, mesh messages).
- `recall` / `remember` (eidetic memory).
- `ask-colleague` (a second, independent model).
- The devague chain: `scope` → `think` → `challenge` → `spec-to-plan` →
  `assign-to-workforce` → `deviate` / `validate-delivery` →
  `summarize-delivery`.

Prerequisites on PATH: `devex` (>=0.21) for `cicd`, `agtag` (>=0.1) for
`communicate`, and optionally `colleague` for `ask-colleague`.

The `recall`/`remember` SKILL.md descriptions say records default to a private
home-dir scope. **The wrapper code is what actually runs**: when the suffix
resolves from `culture.yaml`, `remember.sh` defaults to `--visibility public`
and writes to the committed `<repo>/.eidetic/memory` (needs eidetic >=0.10.0).
Pass `--visibility private` to keep a record in `$HOME/.eidetic/memory`.

## Conventions

- **Every PR bumps the version**, even for docs, config or CI. Use the
  `version-bump` skill, which updates `pyproject.toml` and prepends a
  Keep-a-Changelog entry to `CHANGELOG.md`. CI's `version-check` job fails
  the PR otherwise. `__version__` comes from package metadata, so there is no
  second version string to edit.
- **PRs** go through the `cicd` skill; its scripts append the
  `- culture-library (Claude)` signature, so don't sign bodies by hand.
- **Deploy**: a push to `main` that touches `pyproject.toml` or
  `culture_library/**` publishes to PyPI via Trusted Publishing
  (`.github/workflows/publish.yml`). PRs publish a `.devN` build to TestPyPI.
  The `pypi`/`testpypi` GitHub environments and the PyPI Trusted Publisher
  must be configured before those jobs can succeed. SonarCloud
  (`agentculture_culture-library`) runs only when `SONAR_TOKEN` is set, and
  then blocks on a red quality gate or on any OPEN/CONFIRMED issue in the PR
  (`scripts/sonar-pr-issues.py`, cited from culture-rules; accepted issues pass).
- **Worktrees** you create by hand go in
  `../.worktrees.culture-library/<name>/`, never a shared `../worktrees/`.
  Use work-scoped branch prefixes (e.g. `ingest/t2`). The vendored
  `assign-to-workforce` skill already uses this root but still names branches
  `agent/<task-id>`, which collide with leftovers from earlier fan-outs, so
  override the branch prefix when following it. `ask-colleague`'s
  self-deleting temp worktrees under `$TMPDIR` are exempt. Clean up with
  `git worktree remove`, not `rm -rf`.
- **Memory**: `/recall` before non-trivial work and `/remember` non-obvious
  decisions, constraints and gotchas as they come up. Don't store what the
  repo or `CHANGELOG.md` already records.
- **Never run `colleague learn-from claude` here**: `.colleague/skills` is a
  symlink, so it would write generated files into `.claude/skills/`.

## Roadmap

Built in the `library-culture-dev` run (see the delivery summary). Still
open:

- search over repo documents (#4);
- map recording EPUBs to `transcript` on migration (#5);
- an embedding model comparison (#7). The eval harness can run it from config
  alone: rerun eval sets 1–5 under each embed config;
- faster passage retrieval, a multi-kind filter, and transcript timestamps in
  chunks (#25);
- in-progress items that culture-rules grows (#22);
- the ebooks.culture.dev teardown, gated by
  `docs/operations/teardown-ebooks.md`.

Machine clients (culture-rules, Gwen, xteink) call the library on spark's
local address with scoped API keys. People use library.culture.dev behind
Cloudflare Access.
