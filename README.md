# litellm-runpod

Runs a [LiteLLM](https://github.com/BerriAI/litellm) proxy/router as a RunPod
CPU Pod, with its own Postgres instance baked into the image (data lives on
a mounted network volume so it survives Pod restarts).

## Files

- `Dockerfile.litellm-router` — builds the image (litellm + Postgres + a default config).
- `entrypoint.sh` — starts Postgres on the mounted volume, writes the config, starts litellm.
- `config/litellm_config.yaml` — default config baked into the image (see below).
- `create_litellm_router_pod.py` — CLI to create the Pod via the RunPod API.

## Setting your own model config

`config/litellm_config.yaml` is only a **fallback**. At boot, `entrypoint.sh`
overwrites `/app/config.yaml` with the `LITELLM_CONFIG_YAML` env var whenever
it's set, so you don't need to rebuild the image to change models.

To use your own config:

1. Write a normal LiteLLM `config.yaml` (see [LiteLLM docs](https://docs.litellm.ai/docs/proxy/configs)
   for `model_list` / `router_settings` / etc).
2. Pass its contents as the `LITELLM_CONFIG_YAML` env var:
   - **RunPod console (template or Pod deploy)**: paste the whole YAML file into the
     `LITELLM_CONFIG_YAML` environment variable field.
   - **CLI**: `python create_litellm_router_pod.py --config-file path/to/config.yaml ...`

If `LITELLM_CONFIG_YAML` is left unset, the Pod falls back to the minimal
example in `config/litellm_config.yaml`.

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
