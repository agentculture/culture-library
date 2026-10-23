# culture-library

Culture's library of your own content and media: recordings, books, newspapers, articles, EPUBs, PDFs, docs and more. One catalog to ingest, index, search and serve it all, published at library.culture.dev.

`culture-library` is an [AgentCulture](https://github.com/agentculture) mesh
agent: a Python package with an agent-first CLI, a mesh identity, and the
canonical skill kit.

## Status

**Early scaffold.** This repo was created from
[`culture-agent-template`](https://github.com/agentculture/culture-agent-template)
and currently ships only the sibling-pattern baseline described below. The
library itself — ingesting media, indexing, search, and serving at
library.culture.dev — is [planned](#roadmap) and not yet implemented.

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

- **Ingest**: bring your own recordings, books, newspapers, articles, EPUBs,
  PDFs and docs into one catalog.
- **Index & search** across everything in the catalog.
- **Serve** the catalog, published at library.culture.dev.

The design is not settled yet. This list restates the project's goal; it does
not describe a committed design.

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
