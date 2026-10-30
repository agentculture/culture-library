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

## Restart the live stack

On spark the live stack has no `deploy/.env`. Its settings live in a host env file
outside git, `$HOME/.config/culture-library/live.env`, and the tunnel token comes from
`grant`. Keep every value the stack needs in that file, never typed on the command
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

Recreate `library` and `mcp` with the current image (cloudflared keeps its tunnel). The
CLI does it in one step: it refuses an env file with a missing, empty or placeholder
setting, or send targets that do not parse, before anything runs. Then it recreates the
containers, waits for `/api/health` and lists the send targets the new container loads:

```bash
uv run culture-library deploy restart            # --build rebuilds the image first
uv run culture-library deploy restart --dry-run  # print the commands, run nothing
```

It reads `$CULTURE_LIBRARY_LIVE_ENV`, else `$HOME/.config/culture-library/live.env`, and
first checks that each grant secret exists (`grant show`, which never prints a value),
then runs exactly this, which also works by hand:

```bash
grant run --inject TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN \
  --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY -- \
  docker compose -f deploy/compose.yaml --env-file "$HOME/.config/culture-library/live.env" \
  --profile remote up -d --no-deps library mcp
```

With an S3 bucket in the env file, the command also injects the AWS credentials
(see "S3 mirror").

Then check it came back with its settings:

```bash
curl -s http://127.0.0.1:8796/api/health
docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
  "from culture_library.core import send; print(send.load_targets('/data'))"
```

The second command must list the send targets. A `SendConfigError` means the env file
is wrong: fix it and recreate again. A restart never touches the data volume. Drop
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
   around `docker compose up` (`--gateway-secret NAME` picks another grant secret). The
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
   docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
     "from culture_library.core import Store
   with Store().reader() as c: print(c.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0])"
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
uv run culture-library mirror status      # outbox backlog, oldest pending age, newest error
uv run culture-library mirror reconcile   # queue repo-doc keys missing or stale in the bucket
```

`GET /api/status` carries the same `mirror` block.

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

The release that adds repo-document mirroring widens the items kind CHECK again (the new
'design' kind), so the first open rebuilds the `items` table. Take a backup first (step 1
below) and keep the old image.

Some releases change the database schema. Release 0.12.0 widens the `items.kind`
CHECK constraint so the `todo` kind is accepted. On first open after the upgrade the
store rebuilds the `items` table once, preserving ids and the id sequence. It verifies
the row counts and refuses to open (rolling back) if they differ. Follow these steps
for any such release.

1. Keep the running image for rollback, then take a backup with it and verify it.
   The rebuild in step 3 overwrites the `culture-library:local` tag, and the container
   mounts only the data volume, so back up to a host directory using the *old* image
   (no migration can run during the backup). The image runs as uid 10001, so the
   empty host directory must be writable for it during the run:

   ```bash
   docker tag culture-library:local culture-library:<old-version>
   B=~/backups/culture-library-pre-<new-version>; mkdir -p "$B"; chmod o+w "$B"
   docker run --rm -v culture-library_culture-library-data:/data -v "$B":/backup \
     --entrypoint culture-library culture-library:<old-version> \
     backup run --data-dir /data --to /backup
   chmod o-w "$B"
   docker run --rm -v "$B":/backup:ro --entrypoint culture-library \
     culture-library:<old-version> backup verify /backup/snapshots/<UTC stamp>
   ```

2. Record the row count, highest id and id sequence before the upgrade:

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
     "import sqlite3; c = sqlite3.connect('file:/data/library.db?mode=ro', uri=True); \
   print(c.execute('SELECT count(*), max(id) FROM items').fetchone(), \
   c.execute(\"SELECT seq FROM sqlite_sequence WHERE name = 'items'\").fetchone())"
   ```

3. Build the new image and recreate only `library` and `mcp` (cloudflared keeps its
   tunnel). Use the env file from [Restart the live stack](#restart-the-live-stack), so
   every setting carries over; the tunnel token comes from `grant` and is never
   printed:

   ```bash
   git checkout <new-tag-or-commit>
   docker compose -p culture-library -f deploy/compose.yaml build library
   grant run --inject TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN \
     --inject GATEWAY_API_KEY=LOBES_GATEWAY_API_KEY -- \
     docker compose -f deploy/compose.yaml --env-file "$HOME/.config/culture-library/live.env" \
     --profile remote up -d --no-deps library mcp
   ```

   Then run the send-targets check from that section.

4. Check the container logs for the one-line migration diagnostic, which the store
   writes to stderr once, on success:

   ```bash
   docker compose -p culture-library -f deploy/compose.yaml logs library | grep -i migrat
   ```

   If the container fails to start with a row-count mismatch, the store rolled back
   and left the database as it was. Do not retry; restore the snapshot (below) and
   report the failure.

5. Run the count query from step 2 again. The count and `max(id)` must equal the
   before values (plus any items uploaded since).

**Rollback is restoring the pre-upgrade snapshot**, not redeploying an older image
over the migrated database: stop the stack, restore the snapshot from step 1 with the
restore steps in [backup.md](backup.md), then start the image tagged in step 1.

The release that adds the `discord-message` kind (#19) does the same: the same one-time
rebuild and the same steps. Counts are checked for `items` and for every child table
(`embeddings`, `index_queue`, `item_sources`, `eval_sets`).

Running an older image on a database that holds `todo` or `discord-message` items is
unsupported. The old `feed.append` rejects unknown kinds, so events for those items fail.
A snapshot restore discards every item created since the upgrade, including `todo` and
`discord-message` items culture-rules pushed in the meantime; export what you need first.

### Eval columns (additive, no rebuild)

The eval-suites release adds nullable columns in place. On first open the store runs
`ALTER TABLE ADD COLUMN` for each one that is missing; it does not rebuild a table and
the steps above still apply (take the pre-deploy snapshot in step 1 first, because a
restore is the only rollback). After the upgrade, check that the columns exist:

- `eval_runs`: `partial_aggregates_json`, `error_rate`, `suites_json`, and `running`
  (`INTEGER NOT NULL DEFAULT 0`).
- `eval_run_samples`: `question`, `reference_chunk_shas`, `set_version`,
  `retrieved_item_ids`, `latency_ms`, `freshness`.

```bash
docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 -c \
  "import sqlite3; c = sqlite3.connect('file:/data/library.db?mode=ro', uri=True); \
print([r[1] for r in c.execute('PRAGMA table_info(eval_runs)')], \
[r[1] for r in c.execute('PRAGMA table_info(eval_run_samples)')])"
```

**Snapshot backfill.** Runs stored before this release have no question or reference
shas in their results. `culture_library.core.eval_store.backfill_snapshots(conn)` copies
them from `eval_samples` into those results. It is idempotent, and a result whose sample
was deleted by `replace_samples` (a regenerated set) cannot be filled. No CLI verb runs it,
so run it once after the upgrade:

```bash
docker compose -p culture-library -f deploy/compose.yaml exec -T library python3 - <<'PY'
from culture_library.core import Store, eval_store

with Store("/data").connect() as conn:
    print(eval_store.backfill_snapshots(conn))
PY
```

The command prints how many results it filled; running it again prints `0`.

Upgrading to 0.18.2 or later, run it once more. It also repairs results that the 0.18.0
and 0.18.1 backfill filled without a version check: references copied from a newer set
version are withdrawn (the radar then keeps the run's stored scores), and the rest get their
`set_version`. Repairs are not counted, so this run can print `0`.

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
