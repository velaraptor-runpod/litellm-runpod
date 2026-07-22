#!/bin/bash
set -euo pipefail

# RunPod Network Volumes don't reliably honor POSIX directory permission
# bits -- neither chmod on an existing directory nor mkdir()'s own mode
# argument makes Postgres's strict PGDATA permission check (0700/0750)
# pass when PGDATA lives directly on the volume. So Postgres's actual
# PGDATA lives on the container's own local disk (which handles
# permissions correctly), and the network volume is used purely as a
# durable backup target: restore from it on boot, sync back periodically
# and on shutdown.

PGDATA="/var/lib/postgresql/pgdata"
PGDATA_BACKUP="${PGDATA_BACKUP:-/runpod-volume/pgdata_backup}"
POSTGRES_USER="${POSTGRES_USER:-litellm}"
POSTGRES_DB="${POSTGRES_DB:-litellm}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

mkdir -p "$PGDATA"
chown postgres:postgres "$PGDATA"
chmod 0700 "$PGDATA"

if [ -s "$PGDATA_BACKUP/PG_VERSION" ]; then
  echo "[entrypoint] restoring Postgres data from network volume backup"
  cp -a "$PGDATA_BACKUP/." "$PGDATA/"
  chown -R postgres:postgres "$PGDATA"
  chmod 0700 "$PGDATA"
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[entrypoint] no existing cluster -- initializing at $PGDATA"
  su postgres -c "initdb -D $PGDATA --auth=trust"
  su postgres -c "pg_ctl -D $PGDATA -o '-c listen_addresses=localhost' -l /tmp/pg_startup.log -w start"
  su postgres -c "psql -c \"CREATE USER $POSTGRES_USER WITH SUPERUSER PASSWORD '$POSTGRES_PASSWORD';\""
  su postgres -c "createdb -O $POSTGRES_USER $POSTGRES_DB"
else
  echo "[entrypoint] reusing restored/existing cluster at $PGDATA"
  su postgres -c "pg_ctl -D $PGDATA -o '-c listen_addresses=localhost' -l /tmp/pg_startup.log -w start"
fi

export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}"

sync_to_volume() {
  mkdir -p "$PGDATA_BACKUP"
  su postgres -c "psql -c 'CHECKPOINT;'" >/dev/null 2>&1 || true
  cp -a "$PGDATA/." "$PGDATA_BACKUP/"
}

( while true; do sleep 300; sync_to_volume; done ) &
SYNC_LOOP_PID=$!

shutdown() {
  echo "[entrypoint] shutting down -- final sync to network volume"
  kill "$SYNC_LOOP_PID" 2>/dev/null || true
  sync_to_volume
  kill "$LITELLM_PID" 2>/dev/null || true
  kill "$NGINX_PID" 2>/dev/null || true
  su postgres -c "pg_ctl -D $PGDATA stop -m fast" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

if [ -n "${LITELLM_CONFIG_YAML:-}" ]; then
  echo "[entrypoint] writing config from LITELLM_CONFIG_YAML"
  printf '%s\n' "$LITELLM_CONFIG_YAML" > /app/config.yaml
else
  echo "[entrypoint] LITELLM_CONFIG_YAML not set -- using image's baked-in default config"
fi

# RunPod injects RUNPOD_POD_ID inside the pod itself; default it so local
# sanity-check runs (docker run without that var) still start cleanly --
# the UI redirect will just point at an unresolvable host until you access
# it via /ui/ directly.
export RUNPOD_POD_ID="${RUNPOD_POD_ID:-localhost}"
echo "[entrypoint] starting nginx (UI redirect fix, listens on 4000, proxies to litellm on 4001)"
envsubst '${RUNPOD_POD_ID}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf
nginx -g 'daemon off;' &
NGINX_PID=$!

echo "[entrypoint] starting litellm"
litellm --config /app/config.yaml --port 4001 --host 0.0.0.0 &
LITELLM_PID=$!
wait "$LITELLM_PID"
