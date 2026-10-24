# Deploying on spark

The stack in `deploy/` runs the library server and the MCP server from one image,
and optionally a Cloudflare Tunnel connector that fronts `library.culture.dev`. It
mirrors the xteink stack (`compose.yaml` and `docs/remote-access.md` at xteink commit
`ea2f643`). Provisioning on Cloudflare and on spark is an operator step; nothing in
this repo calls Cloudflare.

## What runs

| Service | Port | Reachable from | Purpose |
|---------|------|----------------|---------|
| `library` | 8796 | spark loopback (default) or LAN | site and API; machine clients use it with API keys |
| `mcp` | 8797 | spark loopback (default) or LAN, never the tunnel | MCP streamable-http for agents; calls `http://library:8796` |
| `cloudflared` | none | outbound only, `remote` profile | serves `library.culture.dev` to humans, behind Cloudflare Access |

Machine clients (culture-rules, Gwen, xteink) use the local address and an API key.
The public hostname is for humans, through Cloudflare Access only. Port 8780 is taken
on spark by xteink, which is why the library uses 8796/8797.

Both ports are published on `${LIBRARY_PUBLISH_BIND:-127.0.0.1}`. To let LAN clients
in, set `LIBRARY_PUBLISH_BIND` to spark's LAN address in `deploy/.env`. Do not use
`0.0.0.0` without a firewall, and keep the MCP port off the internet.

Data lives in the named volume `culture-library-data`, mounted at `/data`
(`CULTURE_LIBRARY_DATA_DIR`). Set `LIBRARY_DATA` to a host directory (writable by
uid 10001) to use a bind mount instead. Every service has `restart: unless-stopped`
and a healthcheck whose start period plus retries finish within 60 s, so the stack
is healthy well inside 120 s of a reboot once the Docker daemon is up.

## Build and start

```bash
cd culture-library
cp deploy/.env.example deploy/.env      # edit; it holds no secrets
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.yaml ps       # library and mcp: healthy
curl -s http://127.0.0.1:8796/api/health
```

Without `--profile remote` no cloudflared container runs and nothing is reachable
from outside spark's network. The image installs `.[server]` and `poppler-utils`
(`pdftotext`) and runs as a non-root user.

## First admin key

The database starts with no keys. Mint the first admin key inside the container,
against the same data directory:

```bash
docker compose -f deploy/compose.yaml exec library \
  culture-library keys mint --name ops --scope admin
```

The raw `clk_...` key is shown once. Store it in grant, not in a file or in git.
Mint narrower keys for each machine client afterwards (`culture-library keys
overview` describes the verbs). MCP clients send their own key as
`Authorization: Bearer clk_...`; the MCP service holds none.

## Remote access with cultureflare

Remote access is opt-in. `library.culture.dev` gets its own tunnel, because
cultureflare replaces a tunnel's whole ingress list, so the hostname must not share a
tunnel with another service. The `--service` target is the compose service name
`library`, because cloudflared runs inside the compose network and
`http://127.0.0.1:8796` would point at the cloudflared container itself. The `mcp`
service is never a target.

cultureflare needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in its own
environment. Run the dry run first and read the plan:

```bash
cultureflare remote-login setup --hostname library.culture.dev \
  --service http://library:8796 --allow OWNER_EMAIL \
  --tunnel-name library-culture-dev --shushu
```

Without `--apply` this changes nothing. Once the plan is right, run the same
command with `--apply` added:

```bash
cultureflare remote-login setup --hostname library.culture.dev \
  --service http://library:8796 --allow OWNER_EMAIL \
  --tunnel-name library-culture-dev --shushu --apply
cultureflare remote-login show --hostname library.culture.dev
```

`--shushu` seals the tunnel token into the operator's secret store instead of printing
it (current cultureflare spells it `--grant`; `--shushu` is a deprecated alias). Replace
`OWNER_EMAIL` with the owner's address.

### Access verification

After `--apply`, copy two public identifiers from the Zero Trust dashboard into
`deploy/.env`: the team domain (`CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN`, for example
`your-team.cloudflareaccess.com`) and the application's audience tag
(`CULTURE_LIBRARY_ACCESS_AUD`). Set both or neither; with only one set the server
refuses to start. They are not secrets. Then recreate the service:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d library
```

### Start the connector

The connector token stays out of `.env` and out of git. Store it as a hidden grant
secret (for example `LIBRARY_TUNNEL_TOKEN`), then inject it at exec time:

```bash
grant run --inject TUNNEL_TOKEN=LIBRARY_TUNNEL_TOKEN \
  -- docker compose -f deploy/compose.yaml --env-file deploy/.env --profile remote up -d
```

To rotate the token, fetch a new one from Cloudflare and re-run
`grant set LIBRARY_TUNNEL_TOKEN - --hidden` with the value on stdin.

## Verify

```bash
# 1. Anonymous browser: the Access wall (a 302 to the Cloudflare login)
curl -sI https://library.culture.dev/ | head -3

# 2. /api without a key: 401
curl -s -o /dev/null -w '%{http_code}\n' https://library.culture.dev/api/library
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8796/api/library

# 3. Health, on spark
curl -s http://127.0.0.1:8796/api/health
docker compose -f deploy/compose.yaml ps

# 4. A machine client with a key (local address)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8796/api/library | head -c 200
```

Expect `302` in step 1 and `401` in both calls of step 2. Through the tunnel the
Access wall answers before the server does; a plain `401` from the loopback address
shows the API itself refuses a missing key. A signed-in owner reaches the site in a
browser.

### Reboot test

```bash
sudo reboot
# after login, within 120 s:
docker compose -f deploy/compose.yaml ps        # library, mcp (and cloudflared): healthy
curl -s http://127.0.0.1:8796/api/health
```

The data volume survives; items and keys are still there. If a container is not
healthy, read `docker compose -f deploy/compose.yaml logs <service>`.

## Backups

Schedule backups before relying on the stack: see
[backup.md](backup.md) for `culture-library backup run --to <dir>` and
`culture-library backup schedule --print`. Run them inside the container (or against a
bind-mounted data directory), writing to a location outside `/data`.

## Rollback

```bash
git checkout <previous-tag-or-commit>
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
```

The data volume is untouched by a rebuild. If a new version migrated the database and
must be undone, stop the stack, restore the last snapshot with the restore steps in
[backup.md](backup.md), then start the previous version. To take the public site down
without touching data, stop only the connector:

```bash
docker compose -f deploy/compose.yaml --profile remote stop cloudflared
```

To remove the public hostname entirely, run `cultureflare remote-login teardown
--hostname library.culture.dev` (dry run first, then `--apply`).
