# Teardown gate for ebooks.culture.dev

This page is the gate for retiring `ebooks.culture.dev`, the xteink library's
public hostname. Decision c25 says the hostname is torn down after
library.culture.dev serves its content. Boundary c42 says the teardown runs
only after all three conditions below are recorded with evidence.

This page writes the gate; it does not run the teardown. The teardown is an
operator action.

`xteink.culture.dev` is the hostname the e-reader devices use. It stays: xteink
keeps the device side (c5).

## Conditions

Fill in each evidence slot with a date, the command you ran and its output, or
a link. All three must hold at the same time, before you run the dry run below.

### 1. Devices sync from the library

The xteink reader `x3-ori` receives items whose bytes come from
culture-library, not from xteink's own library. culture-rules' send-to-device
rule delivers them (culture-rules#86), and xteink fetches the content by
reference (xteink#3).

- [ ] **Evidence:** _date_, the send request (`POST /api/library/{id}/send`,
  202), the culture-rules run id, and xteink's queue entry for that item
  marked delivered after the device's next sync.

### 2. Seven consecutive newspapers on library.culture.dev

culture-rules' daily newspaper lands in culture-library seven days in a row
with no manual steps. That means `library.push` is pointed at the local
address and no longer sends the device-queue step (culture-rules#86).

- [ ] **Evidence:** the seven dates, the matching `created` events from
  `GET /api/feed`, and `GET /api/library?kind=newspaper` showing them.

### 3. Migration 17/17

All 17 xteink items are in culture-library with the same id and sha256.

- [ ] **Evidence:** the output of
  `culture-library migrate from-xteink --data-dir <copy of xteink /data> --apply`,
  ending in `17/17 items with matching sha256`. Then a re-run reporting every
  item `unchanged`.

## Before the teardown

- [ ] **A backup taken and verified in the last 24 hours.** Run
  `culture-library backup run` and `culture-library backup verify <snapshot>`.
  See `backup.md`.
- [ ] **No machine client still calls ebooks.culture.dev or xteink's library
  API.** Check:
  - culture-rules' `library.push` actor `base_url`;
  - embodiment's library base URL (embodiment#97);
  - the xteink MCP clients.

## Dry run, then review

The command runs as a dry run unless `--apply` is given. It lists every
resource it would delete: the tunnel, the DNS record and the Access
application. Pass the names that were used at setup. xteink set the hostname
up with the tunnel `ebooks-culture-dev`; see xteink `docs/remote-access.md` on
feat/issue-1.

```bash
cultureflare remote-login teardown --hostname ebooks.culture.dev \
  --tunnel-name ebooks-culture-dev
```

Paste the dry-run output here and review it before applying.

```text
(dry-run output, date)
```

Check the output for three things:

- only `ebooks.culture.dev` resources are listed;
- the `xteink-culture-dev` tunnel and its DNS record for `xteink.culture.dev`
  are not touched;
- no other hostname in the `culture.dev` zone appears.

## Apply

Only when every box above is ticked:

```bash
cultureflare remote-login teardown --hostname ebooks.culture.dev \
  --tunnel-name ebooks-culture-dev --apply
```

After applying, stop the `cloudflared-ui` container in xteink's stack. The
device tunnel keeps running.

There is no long-lived redirect (decision c25). A browser that still opens
ebooks.culture.dev gets a DNS failure; use library.culture.dev instead.

## Rollback

xteink's stack and data volume are not deleted by this teardown. To restore
the hostname, rerun the setup from xteink `docs/remote-access.md`:

```bash
cultureflare remote-login setup --hostname ebooks.culture.dev \
  --service http://api:8780 --allow <owner> --tunnel-name ebooks-culture-dev
```

Run it as a dry run first, then add `--apply`.
