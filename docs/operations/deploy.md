# Deploying on spark

The stack in `deploy/` runs PostgreSQL 17 with pgvector, the library server and the MCP
server (the last two from one image), and optionally a Cloudflare Tunnel connector that fronts `library.culture.dev`. It
mirrors the xteink stack (`compose.yaml` and `docs/remote-access.md` at xteink commit
`ea2f643`). Provisioning on Cloudflare and on spark is an operator step; nothing in
this repo calls Cloudflare.

## What runs

| Service | Port | Reachable from | Purpose |
|---------|------|----------------|---------|
| `postgres` | none (5432 inside the compose network) | `library` only; no published port | the database: `pgvector/pgvector:pg17`, volume `culture-library-pgdata` |
| `library` | 8796 | spark loopback (default) or LAN | site and API; machine clients use it with API keys |
| `mcp` | 8797 | spark loopback (default) or LAN, never the tunnel | MCP streamable-http for agents; calls `http://library:8796` and holds no database credentials |
| `cloudflared` | none | outbound only, `remote` profile | serves `library.culture.dev` to humans, behind Cloudflare Access |

Machine clients (culture-rules, Gwen, xteink) use the local address and an API key.
The public hostname is for humans, through Cloudflare Access only. Port 8780 is taken
on spark by xteink, which is why the library uses 8796/8797.

Both ports are published on `${LIBRARY_PUBLISH_BIND:-127.0.0.1}`. To let LAN clients
in, set `LIBRARY_PUBLISH_BIND` to spark's LAN address in `deploy/.env`. Do not use
`0.0.0.0` without a firewall, and keep the MCP port off the internet.

The database lives in the named volume `culture-library-pgdata`, owned by the `postgres`
service. The content-addressed files (`files/<aa>/<sha256>`) live in the named volume
`culture-library-data`, mounted in `library` at `/data` (`CULTURE_LIBRARY_DATA_DIR`). Set
`LIBRARY_DATA` to a host directory (writable by uid 10001) to use a bind mount for the
files instead. `postgres` has `shm_size: 1gb` (a parallel HNSW build needs more than
Docker's 64 MB `/dev/shm`) and a `pg_isready` healthcheck; `library` waits for it to be
healthy. Every service has `restart: unless-stopped`
and a healthcheck whose start period plus retries finish within 60 s, so the stack
is healthy well inside 120 s of a reboot once the Docker daemon is up.

## The database

On the first start with an empty `culture-library-pgdata` volume, the init script
`deploy/postgres-init/01-library.sql` runs once, as the superuser. It creates the database
`culture_library` (UTF8 with the `C` collation, so sorting is byte order) and the role
`culture_library` (login only: not a superuser, no create-database or create-role rights).
It also creates the `vector` extension, which needs the superuser. The init script does not
run again, so changing a password later is an `ALTER ROLE`, not an edit to a secret alone.

`library` reads `CULTURE_LIBRARY_PG_HOST`, `_PORT`, `_DATABASE` and `_USER` (set in
`compose.yaml` to `postgres`, `5432`, `culture_library`, `culture_library`) and
`CULTURE_LIBRARY_PG_PASSWORD`. `mcp` has no database settings.

Two grant secrets hold the passwords. Create them before the first restart that starts
Postgres:

```bash
grant set CULTURE_LIBRARY_PG_PASSWORD - --hidden             # the application role
grant set CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD - --hidden   # injected as POSTGRES_PASSWORD
```

`deploy restart` injects the first into `postgres` (for the init script) and `library`, and
the second into `postgres` only. Neither may appear in `live.env`; `deploy restart` refuses
an env file that sets them.

The store applies versioned migrations (`culture_library/core/migrations/NNNN_*.sql`) when
it opens, once each, under an advisory lock, and records them in `schema_version`. It refuses
to open a database whose collation is not `C`, or whose schema is ahead of the code.

## Build and start

```bash
cd culture-library
cp deploy/.env.example deploy/.env      # edit; it holds no secrets
grant run --inject POSTGRES_PASSWORD=CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD \
  --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
  docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.yaml ps       # postgres, library and mcp: healthy
curl -s http://127.0.0.1:8796/api/health
```

On spark use `culture-library deploy restart` (see "Restart the live stack") instead of
typing the injection by hand.

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

### Rotate a client's key

To change what a client's key may do (for example, add a kind), replace it in two
steps instead of retyping a mint. The new key copies the old one's name, scopes, kinds
and private flag. The old key keeps working until you confirm:

```bash
docker compose -f deploy/compose.yaml exec -T library \
  culture-library keys rotate 3 --kind todo | grant set LIBRARY_PUSH_API_KEY - --hidden
# restart the consumer on the new key, then:
docker compose -f deploy/compose.yaml exec library \
  culture-library keys rotate 3 --confirm <new id>
```

stdout carries only the key; the new id and the confirm command go to stderr. `keys list`
shows which key replaced which.

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
secret (`cultureflare --grant` stores it as `CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN`), then inject it at exec time:

```bash
grant run --inject TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN \
  --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY \
  -- docker compose -f deploy/compose.yaml --env-file deploy/.env --profile remote up -d
```

To rotate the token, fetch a new one from Cloudflare and re-run
`grant set CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN - --hidden` with the value on stdin.

## Verify

```bash
# 1. Anonymous browser: the Access wall (a 302 to the Cloudflare login)
curl -sI https://library.culture.dev/ | head -3

# 2. /api without a key: 401
curl -s -o /dev/null -w '%{http_code}\n' https://library.culture.dev/api/library
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8796/api/library

# 3. Health, on spark (503 when the database is unreachable)
curl -s http://127.0.0.1:8796/api/health
docker compose -f deploy/compose.yaml ps

# 3b. The database block of /api/status (read key): reachable, schema_version,
#     expected_schema_version, schema_current, vector_indexes
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8796/api/status | python3 -m json.tool | grep -A12 '"database"'

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
docker compose -f deploy/compose.yaml ps        # postgres, library, mcp (and cloudflared): healthy
curl -s http://127.0.0.1:8796/api/health
```

Both volumes survive; items and keys are still there. If a container is not
healthy, read `docker compose -f deploy/compose.yaml logs <service>`.

## Backups

Schedule backups before relying on the stack: see
[backup.md](backup.md) for `culture-library backup run --to <dir>` and
`culture-library backup schedule --print`. A backup is a `pg_dump` plus the files tree. On
live, `pg_dump` runs inside the `library` image (the host has no PostgreSQL 17 client
tools and `postgres` publishes no port), writing to a location outside `/data`; backup.md
has the form to use for the timer.

## Restart the live stack

On spark the live stack has no `deploy/.env`. Its settings live in a host env file
outside git, `$HOME/.config/culture-library/live.env`, and the secrets (tunnel token,
gateway key, AWS credentials, the two Postgres passwords) come from `grant`. Keep every value the stack needs in that file, never typed on the command
line: a hand-typed placeholder such as `'[...]'` for the send targets starts the
library with a broken send menu (`SendConfigError: CULTURE_LIBRARY_SEND_TARGETS is not
valid JSON`).

The file holds no secrets. Values are single-quoted so the JSON stays literal:

```bash
COMPOSE_PROJECT_NAME=culture-library
CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CULTURE_LIBRARY_ACCESS_AUD=<application AUD tag>
CULTURE_LIBRARY_SEND_TARGETS='[{"label":"<label>","type":"<description>","enabled":true}]'
```

If the file is missing, rebuild it from the running container before you recreate
anything. The container's environment also holds `GATEWAY_API_KEY`, so keep the grep: it
prints the three settings only, never the key:

```bash
docker inspect culture-library-library-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^CULTURE_LIBRARY_(ACCESS_TEAM_DOMAIN|ACCESS_AUD|SEND_TARGETS)='
```

Recreate `postgres`, `library` and `mcp` with the current image (cloudflared keeps its
tunnel). Starting `postgres` when it already runs is a no-op. The CLI does it in one step: it refuses an env file with a missing, empty or placeholder
setting, or send targets that do not parse, before anything runs. Then it starts
`postgres` and waits for its healthcheck, recreates `library` and `mcp`, waits for
`/api/health` and lists the send targets the new container loads:

```bash
uv run culture-library deploy restart            # --build rebuilds the image first
uv run culture-library deploy restart --dry-run  # print the commands, run nothing
```

It reads `$CULTURE_LIBRARY_LIVE_ENV`, else `$HOME/.config/culture-library/live.env`, and
first checks that each grant secret exists (`grant show`, which never prints a value),
then runs these steps in order (`--build` adds a `docker compose build library` before
them). With an S3 bucket in the env file (live has one since 0.19.0) the second step also
injects the AWS credentials (see "S3 mirror"):

```bash
# 1. the database first; --wait blocks until its healthcheck passes
grant run --inject POSTGRES_PASSWORD=CULTURE_LIBRARY_PG_SUPERUSER_PASSWORD \
  --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD -- \
  docker compose -f deploy/compose.yaml --env-file "$HOME/.config/culture-library/live.env" \
  up -d --no-deps --wait postgres

# 2. then library and mcp
grant run --inject TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN \
  --inject CULTURE_LIBRARY_PG_PASSWORD=CULTURE_LIBRARY_PG_PASSWORD \
  --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY \
  --inject AWS_ACCESS_KEY_ID=CULTURE_LIBRARY_AWS_ACCESS_KEY_ID \
  --inject AWS_SECRET_ACCESS_KEY=CULTURE_LIBRARY_AWS_SECRET_ACCESS_KEY -- \
  docker compose -f deploy/compose.yaml --env-file "$HOME/.config/culture-library/live.env" \
  --profile remote up -d --no-deps library mcp
```

Use `deploy restart` rather than typing this. A hand-typed command that leaves out the
two AWS lines still starts a healthy container, and `/api/status` still shows the mirror
`enabled`, but no upload can succeed. `deploy restart --dry-run` prints the exact command
for the current env file.

Then check it came back with its settings:

```bash
curl -s http://127.0.0.1:8796/api/health
docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
  "from culture_library.core import send; print(send.load_targets('/data'))"
```

The second command must list the send targets. A `SendConfigError` means the env file
is wrong: fix it and recreate again. A restart never touches either volume. The secret
names are `--pg-password-secret` and `--pg-superuser-secret` if yours differ. Drop
`--profile remote` only to stop managing cloudflared from this command.

## Enable embeddings

Without a gateway key the library is full-text only. To turn on semantic search, point it
at the lobes gateway, which serves `gemma2-embed` on `/v1/embeddings` and `reranker` on
`/v1/rerank`. The gateway listens on the host, not in the container network: inside the
`library` container `127.0.0.1:8001` is refused. `deploy/compose.yaml` therefore maps
`host.docker.internal` to the host gateway address for `library` and `mcp`, and the
default `LOBES_GATEWAY_URL` is `http://host.docker.internal:8001`.

1. Add the settings to `$HOME/.config/culture-library/live.env`:

   ```bash
   LOBES_GATEWAY_URL=http://host.docker.internal:8001
   LOBES_EMBED_MODEL=gemma2-embed
   LOBES_RERANK_MODEL=reranker
   ```

   The gateway key is **not** written here: the env file stays secret-free, and
   `deploy restart` refuses one that sets `GATEWAY_API_KEY`. The key lives in grant as
   `LOBES_GATEWAY_API_KEY` (check with `grant show LOBES_GATEWAY_API_KEY`, which never
   prints the value). `GATEWAY_API_KEY` is what switches embeddings on: without it the
   embedder is not built. `LOBES_EMBED_PROMPTS=off` disables the Retrieval-document and
   Retrieval-query prompt names (on by default).
2. Recreate the containers: `uv run culture-library deploy restart`. It runs
   `grant run --inject TUNNEL_TOKEN=... --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY`
   (plus the AWS credentials when an S3 bucket is set) around `docker compose up` (`--gateway-secret NAME` picks another grant secret). The
   new environment and `extra_hosts` entry only apply on recreate.
3. Check the container reaches the gateway. `/capabilities` needs no key and lists the
   `gemma2-embed` and `reranker` roles:

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
     "import urllib.request as u; b = u.urlopen('http://host.docker.internal:8001/capabilities', timeout=5).read().decode(); print('ok' if 'gemma2-embed' in b else 'missing gemma2-embed')"
   ```

   Expect `ok`. A refusal or name error means the `host.docker.internal` mapping is
   missing, so the container was not recreated from the current compose file.
4. Index. On startup the library re-opens every finished job that has no embeddings, so
   the restart in step 2 already queues existing items. To recompute everything (for
   example after changing `LOBES_EMBED_MODEL` or `LOBES_EMBED_PROMPTS`) there is no CLI
   verb for `requeue_all`; it is a function in `culture_library/core/indexer.py`, run
   inside the container:

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
     "from culture_library.core import Store; from culture_library.core.indexer import requeue_all; print(requeue_all(Store()))"
   ```

   The running server's indexer picks the jobs up within a few seconds.
5. Wait until the queue drains: `GET /api/status` (with a read key) must show
   `index.pending` 0, `index.failed` 0 and `index.done` equal to the item count.
   `/api/status` does not report embeddings yet (a later release adds an embeddings and
   chunk count to it), so for now count them in the database:

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml exec -T postgres \
     psql -U culture_library -d culture_library -tAc 'SELECT COUNT(*) FROM embeddings'
   ```

   The count must be greater than 0 before you rely on semantic search.
6. Verify: `GET /api/status` is healthy and a `GET /api/search?q=...` answers with
   `mode` `hybrid` (the response says which mode ran).

Rollback: run `deploy restart --no-gateway-key` (and drop the `LOBES_*` lines from
`live.env` if you like). Existing embeddings stay in the database but are unused, and
full-text search keeps working; a gateway outage alone also degrades to full-text.

## S3 mirror

The library can mirror repo documents (kinds `spec`, `plan`, `devague-record`, `design`) to
an S3 bucket in the operator's own AWS account. The mirror is off while
`CULTURE_LIBRARY_S3_BUCKET` is unset. The documents are private, so the bucket must be too.

### Create the bucket

Create it in your private AWS account (not a shared one), with Block Public Access, default
encryption and versioning on:

```bash
aws s3api create-bucket --bucket <bucket> --region <region> \
  --create-bucket-configuration LocationConstraint=<region>
aws s3api put-public-access-block --bucket <bucket> --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket <bucket> --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket <bucket> --versioning-configuration Status=Enabled
```

### The IAM policy: put, get and list only

Attach this to the IAM user (or role) whose access key the library uses. It carries no
delete permission, so a bug or a leaked key cannot remove mirrored objects (versioning keeps
overwritten ones):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<bucket>"
    }
  ]
}
```

### Settings and grant names

Non-secret settings go in `live.env` (single-quoted like the rest):

```bash
CULTURE_LIBRARY_S3_BUCKET=<bucket>
CULTURE_LIBRARY_S3_REGION=<region>      # optional
CULTURE_LIBRARY_S3_PREFIX=<prefix>      # optional key prefix
```

Optional tuning: `CULTURE_LIBRARY_MIRROR_INTERVAL_S` (default 5),
`CULTURE_LIBRARY_MIRROR_BACKOFF_S` (5), `CULTURE_LIBRARY_MIRROR_BACKOFF_MAX_S` (300) and
`CULTURE_LIBRARY_REPODOC_MAX_BYTES` (16 MiB per pushed document). `deploy restart` refuses a
placeholder such as `<bucket>` or `...` in any of the three S3 settings.

The AWS credentials are secrets and live in `grant`, never in `live.env` (an env file that
sets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or `AWS_SESSION_TOKEN` is refused). Store
them under these names:

| grant secret | injected as | flag to rename it |
|--------------|-------------|-------------------|
| `CULTURE_LIBRARY_AWS_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` | `--aws-key-id-secret` |
| `CULTURE_LIBRARY_AWS_SECRET_ACCESS_KEY` | `AWS_SECRET_ACCESS_KEY` | `--aws-secret-key-secret` |
| (none by default) | `AWS_SESSION_TOKEN` | `--aws-session-secret` |

```bash
grant set CULTURE_LIBRARY_AWS_ACCESS_KEY_ID - --hidden
grant set CULTURE_LIBRARY_AWS_SECRET_ACCESS_KEY - --hidden
```

When `CULTURE_LIBRARY_S3_BUCKET` is set, `deploy restart` checks both secrets exist and adds
`--inject AWS_ACCESS_KEY_ID=... --inject AWS_SECRET_ACCESS_KEY=...` to the `grant run`
command. Without a bucket nothing is injected; `--no-aws-credentials` skips them (the mirror
then cannot upload).

### Check the bucket

```bash
uv run culture-library deploy check-bucket            # bucket from $CULTURE_LIBRARY_S3_BUCKET
uv run culture-library deploy check-bucket --bucket <bucket> --json
```

It needs boto3 (`pip install 'culture-library[s3]'`) and AWS credentials in the shell, and
exits 1 unless all four checks pass: `public_access_block` (all four flags on),
`default_encryption` (a rule is present), `versioning` (`Enabled`), and
`delete_object_denied` (IAM policy simulation of `s3:DeleteObject` for the calling
identity must come back `implicitDeny` or `explicitDeny`). If your identity may not call
`iam:SimulatePrincipalPolicy` that last check is reported `unknown`, which counts as
unhealthy: run it with an identity that can, or verify the policy by hand. Simulation does
not read the bucket policy, so review that too.

### Watching the mirror

```bash
uv run culture-library mirror status      # outbox backlog, parked rows, oldest pending age, newest error
uv run culture-library mirror reconcile   # queue repo-doc keys missing or stale in the bucket
```

`GET /api/status` carries the same `mirror` block. A row whose upload fails 8 times
(`core.mirror.MAX_ATTEMPTS`; a missing local blob, say) is **parked**: it leaves the pending
set, shows as `parked` with its `last_error`, and no longer holds the worker in backoff. Fix the
cause, then run `mirror reconcile`: it re-queues every parked key that is still missing or
stale in the bucket, with its attempts reset. Nothing is deleted, here or in the bucket.

## Machine client keys

Every document migrated from xteink and every pushed repo document is `private` (decision
c31), so only a key with private access (`--private`, or an admin key) sees them. Mint one
key per client and list them by id.

| Client | Key | Scope and kinds | Private access |
|--------|-----|-----------------|----------------|
| Gwen | read key | `read` | Yes, to answer from private documents |
| xteink | reader and uploader key | `read`, plus `write` on the kinds its uploads use | Yes |
| culture-rules (todo, newspapers) | push key | `write` on `todo`, `newspaper` | Not needed: it only writes |
| culture-rules (repo documents) | repodoc push key | `write` on spec, plan, devague-record, design | Yes, so the pushed documents are stored private and readable |
| Mesh MCP clients | one `Authorization: Bearer` key each | `read` | Only for agents that should see private documents |

Mint the repodoc key with:

```bash
uv run culture-library keys mint --name culture-rules-repodocs --scope write --private \
  --kind spec --kind plan --kind devague-record --kind design
```

(`--kind` is repeatable and limits writes; see `culture-library keys overview`.) Give no
other key those four kinds. Rotate any of them as described under
"Rotate a client's key".

## Upgrading across a schema change

Schema changes are versioned SQL files in `culture_library/core/migrations/`
(`NNNN_<name>.sql`). The store applies each one once, when it opens, under a Postgres
advisory lock (so `library` and a CLI process starting together do not race), and records it
in `schema_version`. A migration alters tables in place; the store does not rebuild tables,
and there are no `sqlite_sequence` or row-count rebuild checks to run.

The store refuses to open a database whose schema is ahead of its code (an older image on
a migrated database) and one whose collation is not `C`. Both show up as a container that
does not become healthy; read `docker compose -p culture-library -f deploy/compose.yaml logs
library`.

For a release that adds a migration:

1. Keep the running image for rollback, and take a backup with the current stack
   (see [backup.md](backup.md)); verify it:

   ```bash
   docker tag culture-library:local culture-library:<old-version>
   culture-library backup verify <backup>/snapshots/<UTC stamp>
   ```

2. Note the current schema version and an item count. With a read key:

   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8796/api/status
   ```

   `database.schema_version` is the highest applied migration.

3. Build the new image and restart with the env file from
   [Restart the live stack](#restart-the-live-stack). `deploy restart` injects every grant
   secret (including the Postgres passwords and the S3 mirror's AWS credentials), waits for
   health and lists the send targets. Don't recreate by hand.

   ```bash
   git checkout <new-tag-or-commit>
   uv run culture-library deploy restart --build
   ```

4. Check `/api/status`: `database.schema_version` equals `database.expected_schema_version`
   and `database.schema_current` is `true`, and the item count is at least the one from
   step 2.

**Rollback is restoring the pre-upgrade snapshot**, not running an older image over the
migrated database (the store refuses that). Stop `library` and `mcp`, restore the snapshot
into an empty database and an empty files directory as in [backup.md](backup.md), then
start the image tagged in step 1. The restore discards every write made since the
snapshot. Migrations that only add columns or indexes can also be left in place and
fixed forward; decide before the release, not during it.

### Eval snapshot backfill

Eval runs stored before the eval-suites release have no question or reference shas in
their results. `culture_library.core.eval_store.backfill_snapshots(conn)` copies them from
`eval_samples`. It is idempotent, and a result whose sample was deleted by
`replace_samples` (a regenerated set) cannot be filled. It is only needed on a database
that predates 0.18.2; one copied from 0.19.x SQLite by the cutover already had it run.
No CLI verb runs it:

```bash
docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 - <<'PY'
from culture_library.core import Store, eval_store

with Store("/data").connect() as conn:
    print(eval_store.backfill_snapshots(conn))
PY
```

It prints how many results it filled; running it again prints `0`.

## Rollback

```bash
git checkout <previous-tag-or-commit>
uv run culture-library deploy restart --build
```

Both volumes are untouched by a rebuild. This is enough when the previous version runs on
the current schema (the store refuses a database whose schema is ahead of its code). If a
new version migrated the database and must be undone, stop `library` and `mcp`, restore the
last snapshot with the restore steps in [backup.md](backup.md), then start the previous
version. To take the public site down without touching data, stop only the connector:

```bash
docker compose -f deploy/compose.yaml --profile remote stop cloudflared
```

To remove the public hostname entirely, run `cultureflare remote-login teardown
--hostname library.culture.dev` (dry run first, then `--apply`).

The move from SQLite to Postgres (the cutover from the 0.19.x SQLite release) has its own runbook with its
own rollback rule: [cutover-postgres.md](cutover-postgres.md).
