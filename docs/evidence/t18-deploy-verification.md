# t18 deploy verification

Measured on spark on 2026-10-24 at branch `feat/library-culture-dev`. The
stack ran under a throwaway compose project, `culture-library-verify`, on
spare loopback ports 18896 (library) and 18897 (MCP). Nothing in xteink's or
anyone else's stack was touched. The project and its volume were removed
afterwards with `docker compose -p culture-library-verify -f
deploy/compose.yaml down -v`.

This closes the build-and-boot part of lapse `l10` and issue #6. What is
still open is listed at the end.

## Build

```bash
docker compose -p culture-library-verify -f deploy/compose.yaml build
```

The image `culture-library:local` built in 96 s. It includes:

- the `[server]` extra;
- `events-cli==0.10.0` (the `events` command is at `/usr/local/bin/events`);
- `poppler-utils`.

The container runs as uid 10001.

## Boot and health (claim c8, honesty condition h8)

```bash
LIBRARY_PUBLISH_PORT=18896 LIBRARY_PUBLISH_MCP_PORT=18897 \
CULTURE_LIBRARY_PURGE_INTERVAL_S=0 \
  docker compose -p culture-library-verify -f deploy/compose.yaml up -d
```

| Check | Result |
|-------|--------|
| `library` and `mcp` healthy after `up -d` | 11.4 s (target: under 120 s) |
| both healthy again after `docker restart` | 6.1 s |
| restart policy | `unless-stopped` |
| published ports | `127.0.0.1:18896->8796`, `127.0.0.1:18897->8797` (loopback only) |
| `GET /api/health` | 200 |
| `GET /api/library` without a key | 401 |
| `GET /` (site) | 200, with `content-security-policy: default-src 'self'; script-src 'self'; style-src 'self' …` |
| `POST /mcp/` without a key | 401 |

## Working flow

These were run inside the stack:

1. `docker compose … exec library culture-library keys mint --name verify --scope admin --json`
   minted an admin key. The key is not recorded here.
2. Uploading a small Markdown document (`POST /api/library`, visibility
   private) returned 201, item 1.
3. `GET /api/search?q=zanzibar&mode=fulltext` returned item 1. The body
   indexing works without the embedding gateway.
4. Without send targets, `POST /api/library/1/send {"target":"x3-ori"}`
   returned 422 `unknown_target`.
5. With `CULTURE_LIBRARY_SEND_TARGETS` set to x3-ori, the same send returned
   503 `publish_unavailable`, which is retryable. That is the designed
   behaviour when the broker is unreachable: the host's mosquitto listens on
   127.0.0.1:1883, and that address is not reachable from inside a container.
6. After the container was recreated, item 1 was still served (200) from the
   named volume.

## Still open

- **Provisioning library.culture.dev** with cultureflare is an operator step
  (`docs/operations/deploy.md`). Checking the Access wall depends on it
  (obligation `o46`).
- **Send to device from the container** needs a broker address the container
  can reach. Set `EVENTS_BROKER_HOST` and `EVENTS_BROKER_PORT`, documented in
  `deploy/.env.example`, for example by binding mosquitto to an address the
  container can reach. Until then send returns 503, which can be retried.
- **A full host reboot** was not performed. Container restart recovery was
  measured instead (6.1 s), together with the `unless-stopped` policy.
