"""Markdown catalog for ``culture-library explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("culture-library",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# culture-library

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `culture-library whoami` — identity probe from `culture.yaml`.
- `culture-library learn` — structured self-teaching prompt.
- `culture-library explain <path>` — markdown docs for any noun/verb.
- `culture-library overview` — descriptive snapshot of the agent.
- `culture-library doctor` — check the agent-identity invariants.
- `culture-library cli overview` — describe the CLI surface.
- `culture-library server serve` — run the HTTP server (`[server]` extra).

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `culture-library explain whoami`
- `culture-library explain doctor`
"""

_WHOAMI = """\
# culture-library whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    culture-library whoami
    culture-library whoami --json
"""

_LEARN = """\
# culture-library learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    culture-library learn
    culture-library learn --json
"""

_EXPLAIN = """\
# culture-library explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    culture-library explain culture-library
    culture-library explain whoami
    culture-library explain --json <path>
"""

_OVERVIEW = """\
# culture-library overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    culture-library overview
    culture-library overview --json
"""

_DOCTOR = """\
# culture-library doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

## Usage

    culture-library doctor
    culture-library doctor --json
"""

_SERVER = """\
# culture-library server

Runs the library HTTP server. Requires the optional `[server]` extra
(`pip install 'culture-library[server]'`); the base CLI has zero runtime
dependencies and works without it.

## Verbs

- `culture-library server serve [--host H] [--port N]` — start the server
  (default 127.0.0.1:8780). `GET /api/health` reports status.
- `culture-library server overview` — describe this noun.

If the extra is missing, `serve` exits 2 with a hint to install
`culture-library[server]`.
"""

_CLI = """\
# culture-library cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    culture-library cli overview
    culture-library cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("culture-library",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("server",): _SERVER,
    ("server", "serve"): _SERVER,
    ("server", "overview"): _SERVER,
}
