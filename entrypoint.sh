#!/bin/bash
set -euo pipefail

# The container disk (everything outside $PGDATA) is wiped on every Pod
# restart, but the network volume mounted at /runpod-volume is not. So
# the actual Postgres data lives on the volume; we just point initdb/pg_ctl
# straight at the volume's data dir every boot -- nothing stale to reconcile.

PGDATA="${PGDATA:-/runpod-volume/pgdata}"
POSTGRES_USER="${POSTGRES_USER:-litellm}"
POSTGRES_DB="${POSTGRES_DB:-litellm}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

mkdir -p "$PGDATA"
chown -R postgres:postgres "$(dirname "$PGDATA")" "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[entrypoint] no existing cluster found on volume -- initializing at $PGDATA"
  su postgres -c "initdb -D $PGDATA --auth=trust"
  su postgres -c "pg_ctl -D $PGDATA -o '-c listen_addresses=localhost' -l /tmp/pg_startup.log -w start"
  su postgres -c "psql -c \"CREATE USER $POSTGRES_USER WITH SUPERUSER PASSWORD '$POSTGRES_PASSWORD';\""
  su postgres -c "createdb -O $POSTGRES_USER $POSTGRES_DB"
else
  echo "[entrypoint] existing cluster found on volume at $PGDATA -- reusing"
  su postgres -c "pg_ctl -D $PGDATA -o '-c listen_addresses=localhost' -l /tmp/pg_startup.log -w start"
fi

export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}"

if [ -n "${LITELLM_CONFIG_YAML:-}" ]; then
  echo "[entrypoint] writing config from LITELLM_CONFIG_YAML"
  printf '%s\n' "$LITELLM_CONFIG_YAML" > /app/config.yaml
else
  echo "[entrypoint] LITELLM_CONFIG_YAML not set -- using image's baked-in default config"
fi

echo "[entrypoint] starting litellm"
exec litellm --config /app/config.yaml --port 4000 --host 0.0.0.0
