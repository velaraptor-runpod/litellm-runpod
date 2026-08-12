#!/bin/bash
set -euo pipefail

# RunPod Network Volumes don't reliably honor POSIX directory permission
# bits -- neither chmod on an existing directory nor mkdir()'s own mode
# argument makes Postgres's strict PGDATA permission check (0700/0750)
# pass when PGDATA lives directly on the volume. So Postgres's actual
# PGDATA lives on the container's own local disk (which handles
# permissions correctly), and the network volume is used purely as a
# durable backup target: restore from it on boot, sync back periodically
# and on shutdown, plus dated daily snapshot tarballs for disaster
# recovery / point-in-time restore.

PGDATA="/var/lib/postgresql/pgdata"
PGDATA_BACKUP="${PGDATA_BACKUP:-/runpod-volume/pgdata_backup}"
PGDATA_BACKUP_DAILY="${PGDATA_BACKUP_DAILY:-/runpod-volume/pgdata_backup_daily}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-litellm}"
POSTGRES_DB="${POSTGRES_DB:-litellm}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

mkdir -p "$PGDATA"
chown postgres:postgres "$PGDATA"
chmod 0700 "$PGDATA"

# Boot restore precedence: the live-synced mirror (freshest: every 5 min +
# on shutdown) wins when present; otherwise fall back to the newest dated
# daily snapshot. Extraction goes through a temp dir so a corrupt/partial
# tarball degrades to a fresh initdb below instead of crash-looping.
if [ -s "$PGDATA_BACKUP/PG_VERSION" ]; then
  echo "[entrypoint] restoring Postgres data from network volume backup"
  cp -a "$PGDATA_BACKUP/." "$PGDATA/"
  chown -R postgres:postgres "$PGDATA"
  chmod 0700 "$PGDATA"
else
  latest_daily="$(ls -1t "$PGDATA_BACKUP_DAILY"/pgdata-*.tar.gz 2>/dev/null | head -n1 || true)"
  if [ -n "$latest_daily" ]; then
    echo "[entrypoint] no mirror backup -- restoring latest daily snapshot: $latest_daily"
    extract_tmp="$(dirname "$PGDATA")/.pgdata_restore_tmp"
    rm -rf "$extract_tmp"
    mkdir -p "$extract_tmp"
    if tar -xzf "$latest_daily" -C "$extract_tmp"; then
      rm -rf "$PGDATA"
      mv "$extract_tmp/$(basename "$PGDATA")" "$PGDATA"
      chown -R postgres:postgres "$PGDATA"
      chmod 0700 "$PGDATA"
    else
      echo "[entrypoint] daily snapshot extraction failed -- falling through to fresh initdb"
    fi
    rm -rf "$extract_tmp"
  fi
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

# Dated daily snapshot of PGDATA (one file per UTC day; same-day runs
# overwrite). Written via a .tmp file + mv so a shutdown mid-tar can't
# leave a truncated file matching pgdata-*.tar.gz at boot. All failures
# are swallowed -- this runs under `set -e` inside a background loop that
# must never die.
daily_backup() {
  mkdir -p "$PGDATA_BACKUP_DAILY" || return 0
  rm -f "$PGDATA_BACKUP_DAILY"/.pgdata-*.tar.gz.tmp
  su postgres -c "psql -c 'CHECKPOINT;'" >/dev/null 2>&1 || true
  stamp="$(date -u +%Y%m%d)"
  if tar -czf "$PGDATA_BACKUP_DAILY/.pgdata-$stamp.tar.gz.tmp" -C "$(dirname "$PGDATA")" "$(basename "$PGDATA")" 2>/dev/null; then
    mv "$PGDATA_BACKUP_DAILY/.pgdata-$stamp.tar.gz.tmp" "$PGDATA_BACKUP_DAILY/pgdata-$stamp.tar.gz"
    echo "[entrypoint] daily backup written: $PGDATA_BACKUP_DAILY/pgdata-$stamp.tar.gz"
  else
    echo "[entrypoint] daily backup failed -- will retry next cycle"
    rm -f "$PGDATA_BACKUP_DAILY/.pgdata-$stamp.tar.gz.tmp"
  fi
  find "$PGDATA_BACKUP_DAILY" -name 'pgdata-*.tar.gz' -mtime "+$BACKUP_RETENTION_DAYS" -delete 2>/dev/null || true
}

( while true; do sleep 300; sync_to_volume; done ) &
SYNC_LOOP_PID=$!

# Daily snapshot loop: once at boot, then every 24h.
( daily_backup; while true; do sleep 86400; daily_backup; done ) &
DAILY_LOOP_PID=$!

shutdown() {
  echo "[entrypoint] shutting down -- final sync to network volume"
  kill "$SYNC_LOOP_PID" "$DAILY_LOOP_PID" 2>/dev/null || true
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
