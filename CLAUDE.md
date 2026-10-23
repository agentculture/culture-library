# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`culture-library` is the AgentCulture mesh agent for **Culture's library of
your own content and media** — recordings, books, newspapers, articles, EPUBs,
PDFs, docs and more. The goal is one catalog to ingest, index, search and serve
it all, published at **library.culture.dev**.

**Status: freshly scaffolded.** The repo was created from
[`culture-agent-template`](https://github.com/agentculture/culture-agent-template)
(commit `1a31a8c`) and today contains only the template's sibling-pattern
baseline: an agent-first CLI with introspection verbs, a mesh identity, four
harness prompt files, the vendored skill kit, and the build/deploy pipeline.
**No library functionality exists yet** — no ingest, index, search, storage or
web serving code, and no runtime dependencies. Everything about the catalog is
[Roadmap](#roadmap). Keep this file honest as features land: describe what is
on disk, and mark anything ahead of reality as planned.

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
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
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
  then blocks on a red quality gate.
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

From the project description only. None of this is designed or built yet, so
spec it before building (the `scope`/`think` skills):

- Ingest your own media (recordings, books, newspapers, articles, EPUBs, PDFs,
  docs) into one catalog.
- Index and search across it.
- Serve it, published at library.culture.dev.
