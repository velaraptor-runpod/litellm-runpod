# litellm-runpod

Runs a [LiteLLM](https://github.com/BerriAI/litellm) proxy/router as a RunPod
CPU Pod, with its own Postgres instance baked into the image (data lives on
a mounted network volume so it survives Pod restarts).

## Files

- `Dockerfile.litellm-router` — builds the image (litellm + Postgres + a default config).
- `entrypoint.sh` — starts Postgres on the mounted volume, writes the config, starts litellm.
- `config/litellm_config.yaml` — default config baked into the image (see below).
- `create_litellm_router_pod.py` — CLI to create the Pod via the RunPod API.

## Setting your own proxy config

`config/litellm_config.yaml` ships with **no models** — `model_list: []`.
It only carries `router_settings` / `litellm_settings` / `general_settings`
(including `store_model_in_db: true`), which is what lets models be added
live through the API/UI instead of being baked into the YAML. At boot,
`entrypoint.sh` overwrites `/app/config.yaml` with the `LITELLM_CONFIG_YAML`
env var whenever it's set, so you don't need to rebuild the image to change
these settings.

To use your own settings:

1. Write a normal LiteLLM `config.yaml` (see [LiteLLM docs](https://docs.litellm.ai/docs/proxy/configs)
   for `router_settings` / `litellm_settings` / `general_settings`). Keep
   `general_settings.store_model_in_db: true` unless you actually want to go
   back to YAML-defined models.
2. Pass its contents as the `LITELLM_CONFIG_YAML` env var:
   - **RunPod console (template or Pod deploy)**: paste the whole YAML file into the
     `LITELLM_CONFIG_YAML` environment variable field.
   - **CLI**: `python create_litellm_router_pod.py --config-file path/to/config.yaml ...`

If `LITELLM_CONFIG_YAML` is left unset, the Pod falls back to the (model-less)
default in `config/litellm_config.yaml`.

## Adding a model backend

Models are **not** added to `config/litellm_config.yaml` — they're added at
runtime through LiteLLM's model-management API, which persists them to
Postgres (`store_model_in_db: true`) so they survive restarts without any
config edit or pod update. `api_base`/`api_key` are stored as literal values
in the request body (not `os.environ/...` refs — those only resolve for
YAML-defined models), so use a per-model virtual key or rotate them there if
they change.

```bash
curl -L -X POST '{router_url}/model/new' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model_name": "my-model",
    "litellm_params": {
      "model": "openai/<served-model-id>",
      "api_base": "<backend-api-base>",
      "api_key": "<backend-api-key>"
    }
  }'
```

`litellm_params.model` is **not** a free-form label — `openai/` just tells
LiteLLM to speak the standard OpenAI wire protocol against your custom
`api_base`; the rest must exactly match the model id the backend actually
serves. Confirm it with:

```bash
curl -H "Authorization: Bearer <api_key>" <api_base>/models
```

Manage existing models the same way:

```bash
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" '{router_url}/v1/model/info'   # list
curl -X POST -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"id": "<model_id>"}' '{router_url}/model/delete'                            # remove
```

Or use the Admin UI's **Models** tab (`{router_url}/ui/`) — note the
trailing slash, see "Accessing the Admin UI" in `CLAUDE.md`.

### Option A: RunPod Serverless endpoint

1. Create a Serverless endpoint on RunPod running the vLLM worker image for
   the model you want, and note its endpoint ID.
2. `api_base` is `https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1`.
3. `api_key` is your RunPod **account** API key (`rpa_...`) — RunPod
   Serverless's OpenAI-compatible route authenticates with that, not a
   per-endpoint key.
4. First request after any idle period can take ~90s while the endpoint
   cold-starts a worker — use a generous client timeout.

### Option B: dedicated Pod running vLLM

1. Create a GPU Pod with image `vllm/vllm-openai:latest`, port `8000/http`
   exposed, and a docker start command that sets `--model <hf-model-id>`
   (plus any other vLLM flags you need). Set `VLLM_API_KEY` in the pod's env
   if you want the endpoint to require auth.
2. `api_base` is `https://<pod-id>-8000.proxy.runpod.net/v1`.
3. `api_key` is whatever you set `VLLM_API_KEY` to on that pod.

### Wiring either one into this router

1. Call `POST {router_url}/model/new` (above) with `litellm_params.api_base`
   set to whichever of Option A/B's `api_base` applies, and `api_key` set
   accordingly. No config edit or pod update needed — it's stored in Postgres
   immediately.
2. Confirm it: `curl {router_url}/v1/models` should list your new
   `model_name`, and a `/v1/chat/completions` call against it should reach
   the new backend.

## Build & push the image

Rebuild only when `Dockerfile.litellm-router` or `entrypoint.sh` changes —
not when your model config changes.

```bash
docker build -t <registry>/<you>/runpod-litellm-router:latest -f Dockerfile.litellm-router .
docker push <registry>/<you>/runpod-litellm-router:latest
```

## Create the Pod

```bash
export RUNPOD_API_KEY=...
python create_litellm_router_pod.py \
  --image <registry>/<you>/runpod-litellm-router:latest \
  --master-key sk-... \
  --postgres-password <random-password> \
  --network-volume-id <id> \
  --data-center-id <id-matching-the-volume> \
  --runpod-api-key $RUNPOD_API_KEY \
  --config-file path/to/config.yaml   # optional, otherwise uses the baked-in default
```

The router is reachable at `https://<pod-id>-4000.proxy.runpod.net/v1` once it's up.
