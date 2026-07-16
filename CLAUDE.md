# CLAUDE.md

Guidance for working in this repo: a self-hosted LiteLLM proxy/router running
as a RunPod CPU Pod, with its own Postgres baked into the same container.

## Files

- `Dockerfile.litellm-router` — image: litellm + Postgres, based on
  `ghcr.io/berriai/litellm-database:main-stable` (this is **Wolfi**, not
  Debian — use `apk`, not `apt-get`; it also ships no `postgres` user, which
  the Dockerfile creates via `addgroup`/`adduser`).
- `entrypoint.sh` — inits/starts Postgres against `$PGDATA`, writes
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

## Known limitation

No persistent storage is currently attached (see above) — Postgres data
lives on the pod's ephemeral container disk, so **a pod restart wipes
LiteLLM's metadata DB** (spend logs, virtual keys, etc). The model config
itself is unaffected since it's redelivered via `LITELLM_CONFIG_YAML` on
every boot. A 20GB network volume was created for this purpose but is not
attached — resolving that requires the REST API workaround above.
