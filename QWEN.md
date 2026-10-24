# QWEN.md

This file provides guidance to Qwen Code when working with code in this
repository. Qwen Code's context loader reads exactly `QWEN.md` and `AGENTS.md`
in a directory; this repo deliberately ships only `QWEN.md` — there is no
`AGENTS.md` here (each harness gets its own file; see "Prompt files by
harness" below), so this file is the sole source of project guidance for a
Qwen Code session.

## What this project is

`culture-library` is the AgentCulture mesh agent for **Culture's library of
your own content and media**: recordings, books, newspapers, articles, EPUBs,
PDFs, docs and more. The goal is one catalog to ingest, index, search and
serve it all, published at library.culture.dev.

**Status: built, not yet deployed.** The library lives in `culture_library/`:

- `core/`: storage, ingest, keys, feed, search, repo documents, send, backup;
- `server/`: the HTTP API, OPDS, WebDAV and the site;
- `mcp/`: the agents' MCP server;
- `web/static/`: the site's files.

`CLAUDE.md` describes the layout. Deploying and provisioning
library.culture.dev are still open (see [Roadmap](#roadmap)). Some inherited prose (CLI help/`explain` text, a few
comments, the `docs/harness-*.md` pages) still calls the repo "a clonable
template". That wording came from the scaffold and does not describe the
repo's purpose.

It is a sibling to [`guildmaster`](https://github.com/agentculture/guildmaster)
(the **skills supplier**), [`steward`](https://github.com/agentculture/steward)
(**alignment** — `steward doctor`, the sibling-pattern baseline), and
[`teken`](https://github.com/agentculture/teken) (the **afi-cli** "Agent First
Interface" scaffolder this CLI is cited from) within the Organic Development
framework.

## Prompt files by harness

This repo's root carries one prompt file per agent harness, each read by
exactly one of them — there is no shared base file for them to inherit from:

- **Claude Code** → [`CLAUDE.md`](CLAUDE.md) (the fullest write-up; read it
  first if you are new to the repo).
- **Pi / associate** → [`AGENTS.override.md`](AGENTS.override.md) for context,
  plus [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** → [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** → this file.

## Identity

Declared in `culture.yaml`:

```yaml
agents:
- suffix: culture-library
  backend: claude
```

`backend: claude` fixes the *mesh resident* prompt file to `CLAUDE.md` — the
mesh runtime reads that file, not this one. A Qwen Code session working in a
clone of this repo is a separate, local tool session; it reads `QWEN.md`
regardless of what `culture.yaml` declares, and running Qwen Code here neither
requires nor changes that declaration. The declaration and the resident prompt
together satisfy the two invariants `steward doctor` verifies:
**prompt-file-present** and **backend-consistency** (`claude` ↔ `CLAUDE.md`).

## The CLI

The CLI is cited (cite-don't-import) from teken's `python-cli` reference
(`teken cli cite`), so the runtime package has **no third-party dependencies**;
`teken` (a.k.a. `afi-cli`) is a dev dependency only. Agent-first verbs:

- `culture-library whoami` — identity from `culture.yaml`.
- `culture-library learn` — structured self-teaching prompt.
- `culture-library explain <path>` — markdown docs for any noun/verb.
- `culture-library overview` — descriptive snapshot of the agent.
- `culture-library doctor` — check the agent-identity invariants.
- `culture-library cli overview` — describe the CLI surface itself.

Conventions: every command supports `--json`; results go to stdout, errors and
diagnostics to stderr (never mixed); exit codes are `0` success, `1` user
error, `2` environment error, `3+` reserved. The agent-first rubric is
enforced in CI by `teken cli doctor . --strict`.

New verbs register via a `register(sub)` function in
`culture_library/cli/_commands/`, wired up in `cli/__init__.py`. They raise
`CliError` on failure and never print tracebacks, use the `cli/_output.py`
helpers, and get an entry in `explain/catalog.py`. `CLAUDE.md`'s "CLI
architecture" section has the details.

## Skills

`.claude/skills/` vendors the **canonical guildmaster skill kit**
(cite-don't-import). Provenance and the re-sync procedure live in
`docs/skill-sources.md`. Do not reformat or edit vendored scripts — re-sync
from guildmaster instead.

## Conventions

- **Every PR bumps the version** — even docs/config/CI. Use the
  `version-bump` skill; the `version-check` CI job blocks merge otherwise.
- **Tests**: `uv run pytest -n auto` (single test:
  `uv run pytest tests/test_cli.py::test_whoami_json -v`). **Lint**: black,
  isort, flake8 (line length 100), bandit, markdownlint.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing
  (`.github/workflows/publish.yml`); PRs do a TestPyPI dry-run.

## Layout

```text
culture_library/   agent-first CLI (cited from teken's python-cli reference)
  cli/                    parser, error/output contract, _commands/ (verbs)
  explain/                markdown catalog for `explain`
tests/                    pytest smoke + introspection tests
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/skill-sources.md     skill provenance ledger
culture.yaml              mesh identity (suffix + backend)
.github/workflows/        tests + deploy (PyPI Trusted Publishing)
```

## Roadmap

Planned, not built: ingest your own media into one catalog, index and search
it, and serve it at library.culture.dev. No design is settled yet.

This file describes the repository **as it exists on disk today**. When you
edit, keep claims grounded in checked-in reality; if a section drifts ahead of
reality, mark it `(planned)` or move it under a `## Roadmap` heading. For the
full set of workflow conventions (worktree layout, memory discipline,
`ask-colleague` usage), see [`CLAUDE.md`](CLAUDE.md) — those conventions apply
to work in this repo regardless of which harness is doing it.
