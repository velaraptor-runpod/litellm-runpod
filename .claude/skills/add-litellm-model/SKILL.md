---
name: add-litellm-model
description: Add a model backend to the litellm-runpod router via LiteLLM's live API (not config.yaml), and set its cost/pricing so spend tracking reflects reality. Use when the user wants to add, wire up, or price a model on the router (RunPod Serverless, a dedicated vLLM pod, or any OpenAI-compatible backend).
---

# Add a model to the LiteLLM router

This repo's router ships with **no baked-in models** —
`config/litellm_config.yaml` has `model_list: []` on purpose. Models are
added at runtime through LiteLLM's model-management API and persisted to
Postgres (`general_settings.store_model_in_db: true`), so nothing here
touches `config.yaml`, needs an image rebuild, or needs a pod restart. See
`CLAUDE.md` / `README.md` "Adding a model backend" for the background.

## 0. Get the router URL and master key

Both live only in the pod's env vars — never hardcode or assume them.

```bash
# find the pod (mcp__runpod__list-pods, or ask the user which one)
# then read its env:
```
Use `mcp__runpod__get-pod` (with the pod id) and pull `LITELLM_MASTER_KEY`
from its `env`. The router URL is `https://<pod-id>-4000.proxy.runpod.net`.

If more than one litellm-router pod is running, ask the user which one —
don't guess.

## 1. Confirm the backend's served model id

`litellm_params.model` must exactly match what the backend actually serves
— it is not a free-form label.

```bash
curl -H "Authorization: Bearer <backend-api-key>" <backend-api-base>/models
```

## 2. Add the model via the API

Values in this request body are **literal** — `os.environ/...` refs only
resolve for YAML-defined models, not API-added ones.

```bash
curl -sS -X POST "{router_url}/model/new" \
  -H "Authorization: Bearer {LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "model_name": "<alias-clients-will-call>",
    "litellm_params": {
      "model": "openai/<served-model-id>",
      "api_base": "<backend-api-base>",
      "api_key": "<backend-api-key>"
    }
  }'
```

The response includes `model_id` (a UUID) — save it, it's needed for step 4
(pricing). `litellm_params.model` values get encrypted at rest; that's
expected, not a bug.

For RunPod backends specifically:
- **Serverless endpoint**: `api_base` is
  `https://api.runpod.ai/v2/<ENDPOINT_ID_OR_NAME>/openai/v1`; `api_key` is
  the RunPod **account** key (`rpa_...`) — same key for every serverless
  model, not per-endpoint.
- **Dedicated vLLM pod**: `api_base` is
  `https://<pod-id>-8000.proxy.runpod.net/v1`; `api_key` is whatever
  `VLLM_API_KEY` was set to on that pod.

## 3. Verify

```bash
curl "{router_url}/v1/models" -H "Authorization: Bearer {LITELLM_MASTER_KEY}"
curl -X POST "{router_url}/v1/chat/completions" \
  -H "Authorization: Bearer {LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -d '{"model":"<alias>","messages":[{"role":"user","content":"reply OK"}],"max_tokens":10}'
```
Serverless backends can take ~90s on first request after being idle (cold
start) — use a generous timeout.

## 4. Set pricing

LiteLLM's spend tracking is **per-token** (`model_info.input_cost_per_token`
/ `output_cost_per_token`, both floats). How you fill these in depends on
how the backend is actually billed — ask the user which case applies if it
isn't obvious from context:

### Case A: billed per-token (e.g. RunPod Serverless, most hosted APIs)

Convert the provider's quoted $-per-1M-tokens rate directly:

```
input_cost_per_token  = <input $ per 1M tokens>  / 1_000_000
output_cost_per_token = <output $ per 1M tokens> / 1_000_000
```
If the provider quotes one blended rate (not split input/output), use the
same value for both.

### Case B: billed as a flat $/hr (dedicated GPU pod, cost is the same
whether or not it's actively serving requests)

There's no native "per hour" field that feeds request-level spend logs —
only per-token math does. Ask the user for:
1. The $/hr cost.
2. An assumed sustained tokens/hr throughput (real numbers from vLLM's own
   throughput logs/a load test if they have them; otherwise agree on a
   placeholder like 60,000 tok/hr) — used only to *derive* a per-token
   estimate, not a measured rate.

Then:
```
input_cost_per_token = output_cost_per_token = <$/hr> / <assumed tokens/hr>
```

Store the hourly figure and the estimate's provenance alongside the derived
rate (`model_info` allows arbitrary extra fields) so it's clear later why
the number looks the way it does:

```json
{
  "cost_per_hour_usd": <$/hr>,
  "cost_basis": "dedicated pod, flat $/hr",
  "pricing_note": "per-token rate is an ESTIMATE, derived as $/hr divided by an assumed <N> tokens/hr sustained throughput -- not a measured/real per-token cost"
}
```

### Applying it

`PATCH` merges `model_info` fields in — it's safe to send only the pricing
fields, `litellm_params` (model/api_base/api_key) is untouched:

```bash
curl -X PATCH "{router_url}/model/{model_id}/update" \
  -H "Authorization: Bearer {LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -d '{
    "model_info": {
      "input_cost_per_token": <value>,
      "output_cost_per_token": <value>
    }
  }'
```
(add `cost_per_hour_usd` / `cost_basis` / `pricing_note` into the same
`model_info` object for Case B).

Confirm with `curl "{router_url}/v1/model/info" -H "Authorization: Bearer
{LITELLM_MASTER_KEY}"`.

## Removing a model

```bash
curl -X POST "{router_url}/model/delete" \
  -H "Authorization: Bearer {LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -d '{"id": "<model_id>"}'
```
