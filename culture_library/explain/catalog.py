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
- `culture-library migrate from-xteink` — copy an xteink library in.

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
  (default 127.0.0.1:8796). `GET /api/health` reports status.
- `culture-library server overview` — describe this noun.

If the extra is missing, `serve` exits 2 with a hint to install
`culture-library[server]`.
"""

_EVAL = """\
# culture-library eval

Generate, run and inspect RAG retrieval evals against a running library. Generation and
scoring need the optional `[eval]` extra (`pip install 'culture-library[eval]'`, ragas 0.4.3);
without it `generate` and `run` exit 2 with a hint to install `culture-library[eval]`.

## Configuration

- `CULTURE_LIBRARY_URL` — library base URL (default `http://127.0.0.1:8796`).
- `CULTURE_LIBRARY_API_KEY` — API key (required; mint one with `culture-library keys mint`).
- The judge: `--judge-url`, `--judge-model` and `--judge-max-tokens` on `generate` and `run`
  override `CULTURE_LIBRARY_EVAL_LLM_URL` / `_MODEL` / `_MAX_TOKENS`, which override the
  defaults (the lobes gateway, model `cortex`, 8192 tokens). `eval overview` shows the
  effective judge and where each value came from. Non-public text is never sent to a
  non-local judge.
- `GATEWAY_API_KEY` is sent only to the lobes gateway or a local judge; any other judge uses
  `CULTURE_LIBRARY_EVAL_LLM_KEY`. Judge calls never follow redirects.

## Verbs

- `culture-library eval generate ITEM_ID [--samples N] [--seed S] [--kind book|newspaper]` —
  build a set from the item's chunks (newspapers: from the sources sidecar) and store it.
- `culture-library eval run SET_ID [--mode semantic|hybrid] [--k N] [--rerank|--no-rerank]
  [--candidates N] [--no-llm] [--suites a,b] [--warmup]` — retrieve every question, score it,
  store the run. Run the same set twice with different flags to compare two configurations.
- `culture-library eval show [--set ID | --run ID | --item ID]` — list or show.
- `culture-library eval suites` — list the metric suites (name, version, whether they need the
  judge) and each metric's definition, range and direction. `run --suites a,b` scores with only
  those suites; an unknown name exits 1 before any retrieval. `--warmup` does one untimed
  retrieval first.
- `culture-library eval radar (--run ID | --runs IDS) [--vs IDS] [--budget-s N]` — the ten-axis
  shape (found at 1/3/5, MRR, judge precision/recall, no errors, latency in budget, no
  duplicates, fresh) of a run or a group of runs; `--vs` pairs two.
- `culture-library eval compare --before IDS --after IDS` — before/after axes, deltas and the
  questions that changed. The comparability checks print first; when the groups are not
  comparable only the checks and what was left out are shown.
- `culture-library eval reanchor SET_ID` — re-anchor a set's samples after the item's text
  changed; reports which were re-anchored and which could not be, and bumps the set version.
- `culture-library eval overview` — describe this noun.

Every verb accepts `--json`. Library/API failures exit 2; bad input, stale sets and policy
refusals exit 1.
"""

_KEYS = """\
# culture-library keys

Mint, list and revoke API keys on this host. The first admin key comes from here,
because the `/api/keys` routes need an admin key.

- `keys mint --name N --scope read|write|admin [--kind K] [--private]` prints the key once;
  only a hash is stored. In text mode stdout carries only the key, so it pipes straight into
  a secret store (`| grant set NAME - --hidden`); the note goes to stderr.
- `keys list` shows scopes, kinds, status and `replaces` (which key a key replaced), never
  the secret.
- `keys revoke --id N` revokes a key.
- `keys rotate ID [--kind K] [--drop-kind K] [--scope S] [--private | --no-private]` mints a
  replacement with key ID's name, scopes, kinds and private flag, plus the changes given
  (`--kind` adds to the kind list; a key for any kind stays any kind). It prints the new key
  once, like `mint`, and records `replaces`. Key ID stays valid. A missing or revoked ID is
  refused before anything is minted.
- `keys rotate ID --confirm NEW_ID` revokes ID once the consumer uses the new key. It refuses
  and changes nothing unless NEW_ID exists, is live and was minted as ID's replacement
  (codes `key_revoked`, `not_a_replacement`).
- `--json` on rotate reports `{old_id, new_id, name, scope, kinds, private}`; the first step
  adds `key`, the only place the secret appears.

Scopes: read; write implies read; admin implies both. `--kind` limits writes to those
kinds; `--private` lets the key see private items.
"""

_MIGRATE = """\
# culture-library migrate

One-off migrations into the catalog.

## Verbs

- `culture-library migrate from-xteink --data-dir <xteink-dir> [--dest D] [--apply]
  [--visibility private|public]` — read the xteink `library.db` (opened read-only,
  never written) and copy items with the same id, sha256 and created_at. Titles
  starting `Daily newspaper` become `newspaper`, kind `book` stays `book`, the rest
  become `document`. Dry run unless `--apply`; items are `private` unless
  `--visibility public`. Re-runs report existing items as unchanged; a clashing id
  or sha256 is an error (exit 1), never an overwrite. A `--data-dir` without
  `library.db` is refused with a hint. The destination is `--dest`, else
  `CULTURE_LIBRARY_DATA_DIR`.
- `culture-library migrate overview` — describe this noun.
"""

_BACKUP = """\
# culture-library backup

Back up the library to a second location and prove it restores.

- `backup run [--data-dir D] [--to B] [--keep N]` — copy a consistent SQLite snapshot
  (online backup API, safe while the server writes) to `B/snapshots/<UTC stamp>/` with a
  `manifest.json` (item and repo-document ids, sha256, visibility, counts), and copy only
  missing blobs (items' and repo documents') into `B/files/`. Keeps the last N snapshots
  (default 7). `B` defaults to `CULTURE_LIBRARY_BACKUP_DIR`.
- `backup verify <snapshot>` — check the database against the manifest and re-hash every blob.
- `backup restore --from <snapshot> --to <dir>` — restore into an empty directory (a
  non-empty one is refused) and verify ids, sha256 and visibility.
- `backup schedule --print [--kind systemd|cron]` — print a daily schedule; installs nothing.
- `backup overview` — describe this noun.
"""

_MIRROR = """\
# culture-library mirror

Inspect and reconcile the S3 mirror of repo documents. Every pushed version (and changed
working draft) queues a row in the `mirror_outbox` table; the server's mirror worker uploads
it to `<repo>/<path>@<commit>` (`@working` for drafts), retrying with backoff while the
bucket is unreachable. Pushes never fail because the mirror is down.

- `mirror status [--data-dir D]` — the outbox backlog: `pending`, `done`,
  `oldest_pending_age_s`, the newest `last_error` among pending rows, and whether a backend
  is configured (`CULTURE_LIBRARY_S3_BUCKET`). Opens the store directly, like `backup`.
- `mirror reconcile [--data-dir D]` — compare every repo-document version and draft with the
  bucket (`head`) and queue the keys that are missing or hold different bytes; reports
  `enqueued` and the resulting `pending`. Needs `CULTURE_LIBRARY_S3_BUCKET` (plus `_REGION`,
  `_PREFIX` and AWS credentials) and boto3 from the `[s3]` extra; without them it exits 2.
  It only queues rows and never deletes objects; the server's worker uploads them.
- `mirror overview` — describe this noun.

The server: with `CULTURE_LIBRARY_S3_BUCKET` set, the worker drains every
`CULTURE_LIBRARY_MIRROR_INTERVAL_S` seconds (default 5), backing off from
`CULTURE_LIBRARY_MIRROR_BACKOFF_S` (5) up to `CULTURE_LIBRARY_MIRROR_BACKOFF_MAX_S` (300) while
uploads fail. Without a bucket it does nothing. `GET /api/status` reports a `mirror` block
`{enabled, pending, oldest_pending_age_s, last_error}`.
"""

_DEPLOY = """\
# culture-library deploy

Recreate the live stack's `library` and `mcp` containers on spark, the CLI form of the
runbook's "Restart the live stack" (`docs/operations/deploy.md`).

- `deploy restart [--env-file F] [--compose C] [--tunnel-secret S] [--gateway-secret G |
  --no-gateway-key] [--build] [--dry-run] [--timeout N]` — read the host env file `F`
  (default `$CULTURE_LIBRARY_LIVE_ENV`, else `$HOME/.config/culture-library/live.env`) and
  refuse it, before anything runs, when `COMPOSE_PROJECT_NAME`,
  `CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN`, `CULTURE_LIBRARY_ACCESS_AUD` or
  `CULTURE_LIBRARY_SEND_TARGETS` is missing, empty or a placeholder, or the targets do not
  parse (a `${...}` value, which compose would interpolate, is refused too). Settings the file
  sets are dropped from the commands' environment, so a stale shell export cannot override
  the checked file. Then run `grant run --inject TUNNEL_TOKEN=S --inject GATEWAY_API_KEY=G --
  docker compose -f C --env-file F --profile remote up -d --no-deps library mcp` (no shell;
  both secrets stay in grant; `G` defaults to `LOBES_GATEWAY_API_KEY`, and an env file that sets
  `GATEWAY_API_KEY` is refused), wait for
  `/api/health`, and list the send targets the new container loads. `--build` rebuilds the
  image first; `--dry-run` prints the commands and runs nothing. A schema change still needs
  the upgrade runbook (tag, back up, then restart).
- `deploy overview` — describe this noun.
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
    ("backup",): _BACKUP,
    ("backup", "run"): _BACKUP,
    ("backup", "verify"): _BACKUP,
    ("backup", "restore"): _BACKUP,
    ("backup", "schedule"): _BACKUP,
    ("backup", "overview"): _BACKUP,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("mirror",): _MIRROR,
    ("mirror", "status"): _MIRROR,
    ("mirror", "reconcile"): _MIRROR,
    ("mirror", "overview"): _MIRROR,
    ("deploy",): _DEPLOY,
    ("deploy", "restart"): _DEPLOY,
    ("deploy", "overview"): _DEPLOY,
    ("server",): _SERVER,
    ("server", "serve"): _SERVER,
    ("server", "overview"): _SERVER,
    ("migrate",): _MIGRATE,
    ("migrate", "from-xteink"): _MIGRATE,
    ("migrate", "overview"): _MIGRATE,
    ("eval",): _EVAL,
    ("eval", "generate"): _EVAL,
    ("eval", "run"): _EVAL,
    ("eval", "show"): _EVAL,
    ("eval", "suites"): _EVAL,
    ("eval", "radar"): _EVAL,
    ("eval", "compare"): _EVAL,
    ("eval", "reanchor"): _EVAL,
    ("eval", "overview"): _EVAL,
    ("keys",): _KEYS,
    ("keys", "mint"): _KEYS,
    ("keys", "list"): _KEYS,
    ("keys", "revoke"): _KEYS,
    ("keys", "rotate"): _KEYS,
    ("keys", "overview"): _KEYS,
}
