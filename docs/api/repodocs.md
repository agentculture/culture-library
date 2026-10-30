# Repo documents API: the push contract

This is the contract culture-rules (the pusher, decision c26) follows to put every repo's devague
record into culture-library. Spec: `docs/specs/2026-10-30-devague-work-in-the-library.md`.
Code: `culture_library/server/routes_repodocs.py`.

The library only receives pushed content: it never runs git or devague (boundary c16). The
pusher reads the repos, computes commits, domains and spec links, and sends them.

## What gets pushed

From every **top-level** repo under `~/git`, all files under:

- `docs/specs/`, `docs/plans/`, `docs/deliveries/`, `docs/design/`;
- `.devague/` — every file, not only JSON and Markdown (decision c52), including the
  gitignored `reviews/` and `questions/`.

**Skip worktree checkouts**: any path under `.worktrees.*/` or `*-worktrees/`. The library also
dedups by `(remote, path, commit)`, so a clone and its worktree push one doc (boundary c18), but
the pusher should not send the duplicates at all.

The kind is inferred from the path; the pusher does not send it:

| Path | Kind |
|------|------|
| `docs/design/**` | `design` |
| a binary asset (`.png .jpg .jpeg .gif .webp .svg .html .htm .pdf`) under any prefix | `design` (deviation d1) |
| `docs/specs/**` | `spec` |
| `docs/plans/**` | `plan` |
| `docs/deliveries/**`, `.devague/**` | `devague-record` |

## Authentication

`Authorization: Bearer clk_…` with a **write** key whose kinds allow-list is exactly
`spec, plan, devague-record, design` (claim c44). Store it with `grant` and inject it at run
time; never put it in a file.

## `POST /api/repodocs`

One file per request, JSON body.

| Field | Type | Notes |
|-------|------|-------|
| `remote` | string, required | The repo's git remote URL (any of https, ssh, scp-style); normalized to `host/org/repo`. A repo without a remote sends `host:/abs/path`. |
| `path` | string, required | Repo-relative path, e.g. `.devague/frames/my-feature.json`. |
| `commit` | string, required | A full 40/64-hex commit sha, or `"working"` for the working-tree draft. |
| `committed_at` | ISO-8601 string | Required for a commit push; ignored for `working` and tombstones. |
| `default_branch_head` | sha | Required for a commit push: the default branch's head when pushing. `commit == default_branch_head` makes the version eligible to be current. |
| `content` | string | UTF-8 text. Send exactly one of `content` / `content_b64` (unless `tombstone`). |
| `content_b64` | string | Base64 bytes; use for binary files and anything that is not valid UTF-8. |
| `tombstone` | bool, default `false` | Mark the doc removed at `commit` (a sha or `"working"`). Content is ignored. |
| `project` | object | `{"domains": [string], "connected": [remote]}` from the repo's `culture.yaml`; the latest push wins. |
| `spec_links` | object | Specs only (`docs/specs/**`); see below. |
| `visibility` | string | Accepted for compatibility and **ignored**: every document is stored private (decision c31). |

### Working drafts

`commit: "working"` keeps **one** mutable draft per `(repo, path)`, overwritten on each push
(decision c25). Push it for uncommitted edits and for gitignored files. A draft with the same
bytes as the stored draft is a no-op. Readers see the draft by default when it is ahead of the
committed version (decision c51).

### Tombstones

When a file disappears from a repo (git rm, a deleted frame, a discarded draft), push
`{"remote", "path", "commit", "tombstone": true}`. The doc leaves the default listing, keeps
its version history, and loses its draft. Nothing is deleted from S3. A tombstone for a doc
the library never received is `404 not_found`; replaying the same tombstone is a no-op.

### Project scope from `culture.yaml`

Each repo declares its domains and connected projects in its `culture.yaml` (decision c24,
assumption c32), for example:

```yaml
domains: [library, media]
connected: [github.com/agentculture/culture-rules]
```

The pusher sends them as `project` with every push (or at least once per cycle per repo).
A project is a repo; `connected` lists other repos (decision c27).

### Spec links

For a spec, the pusher computes which code realizes it (decision c53) and sends:

| Field | Meaning |
|-------|---------|
| `pr` | The delivering PR: a URL, a number, or `{"url", "number"}`. |
| `merge_commit` | The sha of the merge that delivered the spec. |
| `code_paths` | The files that currently represent the spec. |
| `at_commit` | The commit `code_paths` was computed at. |
| `at_committed_at` | ISO-8601 time of `at_commit`; it decides which push is newer (deviation d2). Required with `code_paths`. |
| `trailer_commits` | Commits that carry a `Spec: <slug>` trailer for this spec. |

How to compute `code_paths`: if any commit carries a `Spec: <slug>` trailer, use the files those
commits touched; otherwise use the files changed by the delivering merge. Then follow those
files forward through renames to the current default-branch head. A push whose
`at_committed_at` is older than the stored one never replaces the stored paths.

### Size limit

The decoded content is capped at `CULTURE_LIBRARY_REPODOC_MAX_BYTES` (default 16 MiB; the
largest file in the corpus today is 1.3 MB). Over the limit is `413 too_large` and nothing is
stored.

### Responses

- `201` when something was created (a new version, or a new or changed draft); `200` otherwise,
  including every tombstone.
- Body: `{doc, version, draft, created, current_changed, tombstoned, project, spec_links}`.
  `version` is `null` for a working push; `draft` is `null` for a commit push.
- Errors are flat `{"detail": "...", "code": "..."}` (deviation d11): `400 invalid`,
  `403 forbidden_kind`, `404 not_found`, `413 too_large`, `422` for a malformed body.

Every new version or changed draft queues an upload to the private S3 mirror (key
`<repo>/<path>@<commit>` or `<repo>/<path>@working`) and appends one feed event, in the same
transaction. A tombstone appends one `archived` event (deviation d10).

## `GET /api/repodocs`

Lists docs: `{"docs": [...]}`. Filters intersect:

| Param | Meaning |
|-------|---------|
| `remote`, `path` | Exact repo and path. |
| `domain` | Docs of repos declaring that domain; an unknown domain gives `[]`. |
| `project` | Docs of one repo (`host/org/repo` or any remote form). |
| `connected=1` | With `project`: also the docs of its connected projects. |
| `include_removed=1` | Include tombstoned docs. |

Every document is private, so a read key without private access lists nothing.

## Example

```bash
curl -sS -X POST http://127.0.0.1:8796/api/repodocs \
  -H "Authorization: Bearer $LIBRARY_PUSH_KEY" -H 'Content-Type: application/json' \
  -d '{"remote": "https://github.com/agentculture/culture-library.git",
       "path": ".devague/frames/devague-work-in-the-library.json",
       "commit": "working",
       "content": "{\"slug\": \"devague-work-in-the-library\"}",
       "project": {"domains": ["library"],
                   "connected": ["github.com/agentculture/culture-rules"]}}'
```
