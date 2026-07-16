#!/usr/bin/env python3
"""
Create a RunPod CPU Pod running the LiteLLM router (built from
Dockerfile.litellm-router, see that file's header for build/push steps).

The router's Postgres data directory lives on a network volume mounted
at /runpod-volume, so the Pod must be created in that volume's
data center (see scripts/create_network_volume.py, run once beforehand).

CPU pods use computeType="CPU" + cpuFlavorIds instead of gpuTypeIds/gpuCount.

Usage:
    export RUNPOD_API_KEY=...
    python scripts/create_litellm_router_pod.py \
        --image <registry>/<you>/runpod-litellm-router:latest \
        --master-key sk-... \
        --postgres-password <random-password> \
        --network-volume-id <id-from-create_network_volume.py> \
        --data-center-id US-KS-2 \
        --runpod-api-key $RUNPOD_API_KEY
"""
from __future__ import annotations

import argparse
import sys

import requests

REST_BASE = "https://rest.runpod.io/v1"


def create_cpu_pod(api_key: str, image: str, env: dict[str, str],
                    network_volume_id: str, data_center_id: str) -> dict:
    payload = {
        "name": "runpod-litellm-router",
        "imageName": image,
        "computeType": "CPU",
        "cpuFlavorIds": ["cpu3c"],   # 3 vCPU/GB-per-core tier; router is light, no need for more
        "vcpuCount": 2,
        "containerDiskInGb": 10,
        "ports": ["4000/http"],
        "env": env,
        "networkVolumeId": network_volume_id,
        "volumeMountPath": "/runpod-volume",
        "dataCenterIds": [data_center_id],  # constrained by the volume's location
    }
    r = requests.post(
        f"{REST_BASE}/pods",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--master-key", required=True)
    ap.add_argument("--postgres-password", required=True, help="password for the in-pod Postgres user litellm connects as")
    ap.add_argument("--network-volume-id", required=True, help="from scripts/create_network_volume.py")
    ap.add_argument("--data-center-id", required=True, help="must match the network volume's data center")
    ap.add_argument("--runpod-api-key", required=True, help="value for the RUNPOD_API_KEY env var the router calls out with")
    ap.add_argument("--glm-key", default="")
    ap.add_argument("--config-file", help="path to a litellm config.yaml; its contents are passed via LITELLM_CONFIG_YAML "
                                           "so the pod uses it instead of the image's baked-in default")
    args = ap.parse_args()

    control_key = args.runpod_api_key  # your RunPod *account* API key, used to CREATE the pod
    if not control_key:
        sys.exit("--runpod-api-key is required")

    env = {
        "LITELLM_MASTER_KEY": args.master_key,
        "POSTGRES_PASSWORD": args.postgres_password,  # entrypoint.sh builds DATABASE_URL from this
        "RUNPOD_API_KEY": args.runpod_api_key,  # baked in so litellm can call your serverless endpoints
    }
    if args.glm_key:
        env["GLM_LOCAL_KEY"] = args.glm_key
    if args.config_file:
        with open(args.config_file) as f:
            env["LITELLM_CONFIG_YAML"] = f.read()

    pod = create_cpu_pod(control_key, args.image, env, args.network_volume_id, args.data_center_id)
    pod_id = pod["id"]
    print(f"created pod {pod_id}")
    print(f"router will be reachable at: https://{pod_id}-4000.proxy.runpod.net/v1")


if __name__ == "__main__":
    main()

