# CLAUDE.md

Guidance for working in this repo: a self-hosted LiteLLM proxy/router running
as a RunPod CPU Pod, with its own Postgres baked into the same container.

## Files

- `Dockerfile.litellm-router` — image: litellm + Postgres, based on
  `ghcr.io/berriai/litellm-database:main-stable` (this is **Wolfi**, not
  Debian — use `apk`, not `apt-get`; it also ships no `postgres` user, which
  the Dockerfile creates via `addgroup`/`adduser`).
- `entrypoint.sh` — inits/starts Postgres on **local** disk, restores/syncs
  it to the network volume as a backup (see "Persistence" below), writes
  `/app/config.yaml` from `$LITELLM_CONFIG_YAML` if set, starts litellm.
- `config/litellm_config.yaml` — the model list, baked into the image as a
  fallback. The **live** config is whatever `LITELLM_CONFIG_YAML` is set to
  on the pod — keep this file in sync with that so the repo reflects reality.
- `create_litellm_router_pod.py` — REST-API pod creation script (see
  "Attaching a network volume" below for when this is actually needed instead
  of the RunPod MCP tools).

## Build & push the image

Rebuild only when `Dockerfile.litellm-router` or `entrypoint.sh` changes —
not for model/config changes (those go through `LITELLM_CONFIG_YAML`, no
rebuild needed).

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
docker rm -f litellm-router-test
```

## Changing models / secrets on the running pod

1. Edit `config/litellm_config.yaml`.
2. Push it live with `mcp__runpod__update-pod`, setting **the full YAML** as
   the `LITELLM_CONFIG_YAML` env var, plus every `os.environ/*` var it
   references (API base/key per model).
3. `update-pod`'s `env` is the full replacement env map, not a merge — always
   include every existing var (`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`,
   etc.) alongside whatever you're adding/changing, or you'll drop the rest.
4. No separate restart step needed — `update-pod` restarts the container on
   this v1 CPU pod to apply the new env. (`mcp__runpod__restart-pod` is
   v2-only and 501s here.)

`litellm_params.model` must exactly match the model id the backend actually
serves — it is **not** a free-form label. Check with:
`curl -H "Authorization: Bearer <key>" <api_base>/models`

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

This means a pod restart is only lossless if it's **graceful** (gives the
container time to catch the `TERM` signal and run the final sync) — a hard
kill between periodic syncs loses whatever changed since the last one
(≤5 min of spend logs/keys, not the model config, which is redelivered via
`LITELLM_CONFIG_YAML` regardless).

Verified locally end-to-end: boot → write a row → graceful stop (triggers
final sync) → fresh container against the same volume dir → restore →
row still there.

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

