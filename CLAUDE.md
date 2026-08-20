# CLAUDE.md

Guidance for working in this repo: a self-hosted LiteLLM proxy/router running
as a RunPod CPU Pod, with its own Postgres baked into the same container.

## Files

- `Dockerfile.litellm-router` — image: litellm + Postgres + nginx, based on
  `ghcr.io/berriai/litellm-database` pinned to the `v1.96.2` tag + digest
  (this is **Wolfi**, not Debian — use `apk`, not `apt-get`; it also ships no
  `postgres` user, which the Dockerfile creates via `addgroup`/`adduser`).
- `entrypoint.sh` — inits/starts Postgres on **local** disk, restores/syncs
  it to the network volume as a backup (see "Persistence" below), writes
  `/app/config.yaml` from `$LITELLM_CONFIG_YAML` if set, templates and starts
  nginx (see "Accessing the Admin UI" below), starts litellm.
- `nginx/ui-proxy.conf.template` — nginx config templated by `entrypoint.sh`
  via `envsubst` at boot (only `${RUNPOD_POD_ID}`; nginx's own `$` vars are
  passed through). Listens on **4000** (the port RunPod actually exposes),
  proxies to litellm on internal port **4001**, and rewrites litellm's
  broken UI-redirect `Location` headers — see "Accessing the Admin UI".
- `config/litellm_config.yaml` — `router_settings` / `litellm_settings` /
  `general_settings` only, baked into the image as a fallback; `model_list`
  is intentionally empty (`[]`) — models are added at runtime via the API,
  not this file (see "Changing models" below). The **live** config is
  whatever `LITELLM_CONFIG_YAML` is set to on the pod — keep this file in
  sync with that so the repo reflects reality. `litellm_settings.callbacks`
  points at `custom_callbacks.proxy_handler_instance`.
- `callbacks/custom_callbacks.py` — response sanitizer (`CustomLogger`
  subclass): repairs boundary whitespace in non-streamed
  `function.arguments` and logs streamed hits + leaked template markup as a
  hit counter. Baked to `/app/custom_callbacks.py` (importable because the
  proxy sys.path-adds the config dir). `CALLBACK_MUTATE=0` env = audit-only.
- `create_litellm_router_pod.py` — REST-API pod creation script (see
  "Attaching a network volume" below for when this is actually needed instead
  of the RunPod MCP tools).

## Build & push the image

Rebuild only when `Dockerfile.litellm-router`, `entrypoint.sh`, or
`nginx/ui-proxy.conf.template` changes — not for model/config changes (those
go through the API / `LITELLM_CONFIG_YAML`, no rebuild needed).

```bash
docker buildx build --platform linux/amd64 -t velaraptor1/litellm-router:latest -f Dockerfile.litellm-router --push .
```

Build `linux/amd64` explicitly — RunPod pods run x86_64; a local arm64
(Apple Silicon/OrbStack) build will silently produce the wrong arch.

Sanity-check locally before pushing:

```bash
docker run -d --name litellm-router-test \
  -e LITELLM_MASTER_KEY=sk-test -e POSTGRES_PASSWORD=testpass \
  -v /tmp/pgtest:/runpod-volume -p 14000:4000 \
  velaraptor1/litellm-router:latest
curl http://localhost:14000/health/liveliness
curl -i http://localhost:14000/ui/   # trailing slash always works, incl. locally
docker rm -f litellm-router-test
```

## Changing models on the running pod

Models are added/removed at runtime through LiteLLM's model-management API
(`POST {router_url}/model/new`, `POST {router_url}/model/delete`) or the
Admin UI's Models tab, **not** by editing `config/litellm_config.yaml` —
`general_settings.store_model_in_db: true` persists them to Postgres, so no
pod restart or `LITELLM_CONFIG_YAML` update is needed. See README's "Adding
a model backend" for the exact curl calls.

`litellm_params.model` must exactly match the model id the backend actually
serves — it is **not** a free-form label. Check with:
`curl -H "Authorization: Bearer <key>" <api_base>/models`

## Changing router/general settings or secrets on the running pod

1. Edit `config/litellm_config.yaml` (`router_settings` / `litellm_settings`
   / `general_settings` — no `model_list` entries).
2. Push it live with `mcp__runpod__update-pod`, setting **the full YAML** as
   the `LITELLM_CONFIG_YAML` env var, plus every secret env var it or the
   pod otherwise needs (`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`,
   `DATABASE_URL` if overridden, etc).
3. `update-pod`'s `env` is the full replacement env map, not a merge — always
   include every existing var alongside whatever you're adding/changing, or
   you'll drop the rest.
4. No separate restart step needed — `update-pod` restarts the container on
   this v1 CPU pod to apply the new env. (`mcp__runpod__restart-pod` is
   v2-only and 501s here.)

## Attaching a network volume

`mcp__runpod__create-pod` / `update-pod` have **no `networkVolumeId`
param** — they can only provision an ephemeral pod-local disk via
`volumeInGb`, which does not survive pod delete/recreate (only stop/start of
the *same* pod, and in practice this hasn't reliably taken effect either).

The only way found so far to attach a real Network Volume is the raw REST
API (`create_litellm_router_pod.py`, or a direct `curl` to
`POST https://rest.runpod.io/v1/pods` with `networkVolumeId` set) — and that
requires a valid RunPod **account** API key. The `RUNPOD_API_KEY` in a
typical shell env is not necessarily that key (returned 401 in testing) —
if you need to do this, get the account key from the user first.

Stopping/starting a pod (`POST .../stop` then `.../start`) reuses the same
container instance — it does **not** re-pull the image, even after pushing
a new `:latest`. To pick up a new image, `DELETE` the pod and `POST` a fresh
one with the same config (safe here since Postgres data lives on the network
volume backup, not the pod itself — see "Persistence").

## Persistence

RunPod Network Volumes were found to **not reliably honor POSIX directory
permission bits** — neither `chmod` on an existing directory nor `mkdir`'s
own mode argument makes a directory actually report back as `0700`. Postgres
hard-requires exactly `0700`/`0750` on `PGDATA`, so pointing `PGDATA` directly
at the volume makes `initdb` crash-loop forever with
`FATAL: data directory ... has invalid permissions` — confirmed even with a
freshly-created directory, ruling out "existing dir" vs "fresh mkdir" as the
variable.

The fix in `entrypoint.sh`: Postgres's real `PGDATA` lives on the
**container's local disk** (`/var/lib/postgresql/pgdata`, which handles
permissions correctly), and the network volume (`$PGDATA_BACKUP`, default
`/runpod-volume/pgdata_backup`) is used purely as a backup target — restored
into local storage on boot if present, and synced back every 5 minutes plus
on graceful shutdown (`trap ... TERM INT`, with a `CHECKPOINT` first to
reduce inconsistency risk from copying a live data directory).

On top of that hot mirror, the entrypoint writes **dated daily snapshots**:
a `CHECKPOINT`ed tarball of `PGDATA` at
`$PGDATA_BACKUP_DAILY/pgdata-YYYYMMDD.tar.gz` (default
`/runpod-volume/pgdata_backup_daily`, UTC date stamp — same-day reruns
overwrite), taken once at boot and then every 24h, pruned to
`BACKUP_RETENTION_DAYS` (default 14). These are the disaster-recovery /
point-in-time tier: boot restores the **mirror first** (it's the freshest)
and only falls back to the newest daily snapshot when the mirror is missing
entirely. To force a restore from a daily snapshot, empty/rename the mirror
dir (or point `PGDATA_BACKUP` elsewhere) before booting; to pick an *older*
snapshot, remove the newer `pgdata-*.tar.gz` files first. Snapshot write
goes through a `.tmp` file + rename, and boot extracts via a temp dir, so a
truncated/corrupt shard degrades to a fresh `initdb` instead of
crash-looping.

This means a pod restart is only lossless if it's **graceful** (gives the
container time to catch the `TERM` signal and run the final sync) — a hard
kill between periodic syncs loses whatever changed since the last one
(≤5 min of spend logs/keys, not the model config, which is redelivered via
`LITELLM_CONFIG_YAML` regardless).

Verified locally end-to-end: boot → write a row → graceful stop (triggers
final sync) → fresh container against the same volume dir → restore →
row still there.

## Migrating from a volume-less pod (runbook written 2026-08-20)

Volumes attach ONLY at pod creation — a pre-network-volume pod can never be
given one, so migration = dump → new pod with volume at create time →
restore. Two footguns were hit the first time this was done; the steps below
avoid both.

1. **Dump the old pod** (RunPod console → Connect → Web Terminal; the image
   ships no sshd):
   `su postgres -c "pg_dump -Fc -f /tmp/litellm-backup.dump litellm"`, then
   `pg_restore --list` it as a validity check. Move the file off with
   `runpodctl send` / `receive` (or scp). Stop, never delete, the old pod —
   a stopped pod keeps its disk.
2. **Create the new pod with the volume attached.** The v2 MCP/REST create
   path has no `networkVolumeId`; use `create_litellm_router_pod.py` or raw
   REST (needs the account-level API key). Volume and pod must be in the same
   data center.
3. **Kill LiteLLM's connections before dropping the boot-created DB.**
   LiteLLM starts at boot and holds a Prisma pool; a plain `DROP DATABASE`
   is refused ("being accessed by other users") and a `pg_restore` into the
   surviving non-empty DB yields ~300 "already exists" errors plus COPY
   conflicts — a half-merged mess, not a restore. Evict + drop + recreate in
   ONE psql session so nothing reconnects in between:
   `REVOKE CONNECT ON DATABASE litellm FROM public;` (and from litellm),
   `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='litellm' AND pid <> pg_backend_pid();`,
   `DROP DATABASE litellm;`, `CREATE DATABASE litellm OWNER litellm;`.
   LiteLLM errors on 4001 during this and recovers once the DB is back.
4. **Restore into the truly empty DB:**
   `su postgres -c "pg_restore --no-owner --no-privileges -d litellm /tmp/litellm-backup.dump"`
   — expect ~no errors. Verify row counts on `LiteLLM_VerificationToken`,
   `LiteLLM_ProxyModelTable`, `LiteLLM_SpendLogs`.
5. **Sync local → volume mirror BEFORE any restart.** Boot unconditionally
   restores the mirror over local when the mirror has PG_VERSION, and at this
   point the mirror still holds the stale first-boot cluster; restart too
   early and the restore is overwritten. `su postgres -c "psql -c 'CHECKPOINT;'"`
   then `cp -a /var/lib/postgresql/pgdata/. /runpod-volume/pgdata_backup/`
   (or wait >5 min for the sync loop).
6. Verify via API (`/health/liveliness`, `/model/info`, one request on an
   existing virtual key), then do ONE graceful container restart to prove the
   boot→mirror-restore path serves the restored data. Repoint clients at the
   new pod's `-4000.proxy.runpod.net` URL (the pod id is in the URL; no DNS
   shortcut).

## Accessing the Admin UI

LiteLLM mounts its Admin UI at `/ui` via Starlette `StaticFiles`, which
issues a 307 redirect from `/ui` (no trailing slash) to `/ui/`. That
redirect's `Location` header is built from whatever `Host` RunPod's internal
proxy hop hands the container, which is **not** the public
`<pod-id>-4000.proxy.runpod.net` domain — so the redirect points at an
unreachable internal address (or downgrades to `http://`, which the browser
blocks as mixed content on an `https://` page). Hitting `/ui/` directly
skips the need for that redirect and always works.

The fix baked into the image (`nginx/ui-proxy.conf.template` +
`entrypoint.sh`): nginx listens on the pod's actual exposed port 4000,
proxies to litellm on internal port 4001, and rewrites *any* absolute
redirect `Location` header back to the real public domain via
`proxy_redirect`, regardless of what internal host litellm put in it — same
mechanism as `local/nginx.conf.template` (a dev-only variant for proxying
to an already-running pod from your laptop without a rebuild), just
relocated inside the pod itself.

`RUNPOD_POD_ID` is injected by RunPod automatically inside every pod, so no
extra env var is needed on deploy. It defaults to `localhost` when unset
(e.g. the local sanity-check run below), in which case the redirect still
won't resolve — for local testing, just hit `/ui/` directly.

## Testing

OpenAI-compatible — either the `openai` SDK or raw `requests` against
`{router_url}/v1/models` and `/v1/chat/completions`, with
`Authorization: Bearer <LITELLM_MASTER_KEY>`.

Note: serverless RunPod backends (as opposed to dedicated pod backends) can
take ~90s to respond on the first request after being idle (cold start) —
use a generous client timeout when testing those models.

## Secrets

`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, and per-model API keys live only
in the pod's env vars on RunPod. Check current values via
`mcp__runpod__get-pod`, not by reading this repo — never commit real secret
values here.

