# culture-library

Culture's library of your own content and media: recordings, books, newspapers, articles, EPUBs, PDFs, docs and more. One catalog to ingest, index, search and serve it all, published at library.culture.dev.

`culture-library` is an [AgentCulture](https://github.com/agentculture) mesh
agent: a Python package with an agent-first CLI, a mesh identity, and the
canonical skill kit.

## Status

**Built and live** at library.culture.dev, behind Cloudflare Access. The
library is in `culture_library/`. It has:

- a storage and ingest core;
- an HTTP API compatible with xteink's;
- search;
- repo documents;
- send to device;
- OPDS and WebDAV;
- an MCP server for agents;
- RAG passage retrieval and retrieval evals;
- the site.

It was specified and built with devague: spec in `docs/specs/`, plan in
`docs/plans/`, the run's record in `docs/deliveries/`. Deploy files are in
`deploy/`, and runbooks in `docs/operations/`. What is still open is in the
[Roadmap](#roadmap).

## What's here today

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`). The runtime package has no third-party dependencies.
- **A mesh identity**: `culture.yaml` (`suffix: culture-library`,
  `backend: claude`) plus the matching resident prompt file, `CLAUDE.md`.
- **Four harness prompt files**, one per agent harness (see
  [Prompt files by harness](#prompt-files-by-harness)).
- **The guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline**: pytest, lint, the agent-first rubric gate, a
  per-harness smoke check, and PyPI Trusted Publishing in GitHub Actions.

## RAG and evals

Agents can fetch citable passages, not just titles. `POST /api/rag/retrieve`
(also the MCP `rag_retrieve` tool) takes a query and returns ranked passages
with `item_id`, `chunk_no`, `chunk_sha`, full text and scores, optionally
reranked. Before this, search (`core/search.py` `Hit`) was item-level with a
200-char snippet, and `docs/evidence/t7-embedding-cost.md` measured embedding
cost but not retrieval quality (issue #7 compares embedders).

The `eval` verbs measure retrieval with RAGAS context precision and recall,
stored in the library so runs compare across modes and embedders:

```bash
uv sync --extra eval                          # ragas lives in the [eval] extra only
uv run culture-library eval generate ITEM_ID  # a book's own text, or a newspaper's sources sidecar
uv run culture-library eval run SET_ID --mode hybrid --k 5
uv run culture-library eval show --set SET_ID
```

Newspaper sets use the sources sidecar at `PUT /api/library/{id}/sources`.
Eval sets and runs live behind `/api/eval/sets` and `/api/eval/runs`. The judge
is an OpenAI-compatible endpoint (default: the local lobes gateway); text from
non-public items is never sent to a non-local judge. Install
`culture-library[eval]` to use it; the base package and the server image do
not include it.

### Eval suites and shape

A run is scored by suites, each a file under `culture_library/eval/suites/`:
`id_retrieval` (found_at_1/3/5/10, precision_at_k, mrr, ndcg), `judged` (RAGAS
context precision and recall, needs a judge), `result_quality` (duplicate rate,
distinct sources), `speed` (p50/p95 latency, share within a 1.0 s budget,
timeout and error rates) and `freshness` (fresh, stale or unknown per
question). The shape radar folds a run, or a group of runs, onto 0-1 axes where
higher is better, so two configurations compare at a glance.

```bash
uv run culture-library eval suites
uv run culture-library eval run SET_ID --suites id_retrieval,speed --warmup
uv run culture-library eval radar --help
uv run culture-library eval compare --help
uv run culture-library eval reanchor --help
```

The HTTP side is `GET /api/eval/suites`, `POST /api/eval/runs/start`,
`GET /api/eval/radar`, `/compare` and `/weakest`, and
`POST /api/eval/sets/{id}/reanchor`; agents use the twelve `eval_*` MCP tools.
The contract is `docs/api/eval.md`. The site has an Evals tab (`#evals`); its
radar and compare pages are planned. The question review queue (#30) is not
built yet.

Embeddings are on in the live library (see "Enable embeddings" in
`docs/operations/deploy.md`). The eval baseline is recorded there: five books
(20 questions each) and three newspaper issues (4 questions each, built from
the sources culture-rules pushes with each issue). ID recall@5 is 0.89 with
rerank and 0.78 without on books, and 1.00 with rerank on newspapers.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run culture-library whoami         # identity from culture.yaml
uv run culture-library learn          # self-teaching prompt (add --json)
uv run culture-library doctor         # agent-identity invariants
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved. Library verbs (ingest, search, serve, …) will be added as
new noun groups on the same contract.

## Roadmap

The library ingests, indexes, searches and serves the catalog at
library.culture.dev today. These are still open:

- remove specs, plans and `.devague` from GitHub once the full-corpus mirror
  check passes (#35); the devague work itself is live since 2026-10-31;
- faster passage retrieval, so Gwen can recall from the library on a live
  turn (#25);
- in-progress items that grow as culture-rules writes them (#22);
- an embedding model comparison (#7), which reruns the stored eval sets;
- stripping Project Gutenberg boilerplate from the search index (#27).

## Prompt files by harness

Four harnesses, four root files, no shared base — each file is read by
exactly one harness:

| Harness | File(s) |
|---------|---------|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) |
| Pi / associate | [`AGENTS.override.md`](AGENTS.override.md) + [`.pi/SYSTEM.md`](.pi/SYSTEM.md) |
| colleague | [`AGENTS.colleague.md`](AGENTS.colleague.md) |
| Qwen Code | [`QWEN.md`](QWEN.md) |

`CLAUDE.md` is the fullest write-up of the repo's conventions; read it first.
`AGENTS.override.md` stops Pi inheriting `CLAUDE.md`, and `.pi/SYSTEM.md`
gives Pi the non-coding `associate` identity (read/find/summarize only).
There is intentionally **no `AGENTS.md`** at the root: it would shadow the Pi
and colleague prompt cascades, and the CI smoke check fails if one appears.

### Two selections, not one

1. **The interactive harness** is whichever binary you run (`claude`, `pi`,
   `colleague`, `qwen`). All four are live over the same clone at once, and
   choosing one never requires editing a file. See
   [`docs/automation-contract.md`](docs/automation-contract.md).
2. **The mesh resident** is the single `backend` in `culture.yaml`. It is what
   the Culture daemon starts and what `steward doctor` checks, and only
   `guild harness use <name>` changes it.

See [`docs/harness-selection.md`](docs/harness-selection.md) for the full
writeup.

## Development

- **Every PR bumps the version** (the `version-bump` skill updates
  `pyproject.toml` and `CHANGELOG.md`). The `version-check` CI job fails the
  PR otherwise.
- **Lint**: black, isort, flake8 (line length 100), bandit, markdownlint, and
  `scripts/scan-secrets.py`. See [`CLAUDE.md`](CLAUDE.md) for the exact
  commands.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing, and
  PRs publish a `.devN` build to TestPyPI
  (`.github/workflows/publish.yml`).

## The `gate` section

`culture.yaml` carries a top-level `gate` section that declares how to set up
and test this repo, so tooling (for example a PR-fixer that re-runs the
checks before pushing) needs no per-repo knowledge:

```yaml
gate:
  setup:
  - [uv, sync]
  test:
  - [uv, run, pytest, -n, auto]
```

`setup` and `test` are each a list of commands, and each command is an argv
list (never a shell string), run in order from the repo root. The values are
configuration only; no tool supplies a default. Culture's loader ignores
unknown top-level keys, so the section is safe alongside `agents`.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
