#!/bin/sh
# Curatio all-in-one entrypoint: starts the built-in PostgreSQL, then the server.
#
# /data/postgres     the database cluster
# /data/secrets.env  SECRET_KEY and the database password, generated on the
#                    first start and kept, so restarts and updates reuse them
# /data/logs         the PostgreSQL log
#
# Required settings: BASE_URL, TMDB_API_KEY, MASTER_PASSWORD.
set -eu

DATA="${CURATIO_DATA:-/data}"
PGDATA="$DATA/postgres"
PGBIN="/usr/lib/postgresql/${PG_MAJOR}/bin"
SECRETS="$DATA/secrets.env"
SOCKET=/run/postgresql

log() { echo "[curatio-aio] $*"; }

missing=""
for name in BASE_URL TMDB_API_KEY MASTER_PASSWORD; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || missing="$missing $name"
done
if [ -n "$missing" ]; then
    log "Set these settings on the container:$missing"
    exit 1
fi

mkdir -p "$DATA/logs" "$SOCKET"
chown postgres:postgres "$SOCKET" "$DATA/logs"

# --- Secrets: generated once, kept in /data ---------------------------------
if [ ! -f "$SECRETS" ]; then
    log "First start: generating secrets in $SECRETS"
    (
        umask 077
        printf 'GENERATED_SECRET_KEY=%s\nPOSTGRES_PASSWORD=%s\n' \
            "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" > "$SECRETS"
    )
fi
chmod 600 "$SECRETS"
# shellcheck disable=SC1090
. "$SECRETS"
# A SECRET_KEY set on the container wins; otherwise use the generated one.
# Never change it after setup: it encrypts stored Plex/Jellyfin sign-ins.
SECRET_KEY="${SECRET_KEY:-$GENERATED_SECRET_KEY}"
export SECRET_KEY

# --- PostgreSQL ---------------------------------------------------------------
if [ -f "$PGDATA/PG_VERSION" ] && [ "$(cat "$PGDATA/PG_VERSION")" != "$PG_MAJOR" ]; then
    log "The database in $PGDATA is PostgreSQL $(cat "$PGDATA/PG_VERSION"), this image runs $PG_MAJOR."
    log "Restore a backup into a fresh /data, or use an image built for that version."
    exit 1
fi
mkdir -p "$PGDATA"
chown postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    log "First start: creating the database"
    gosu postgres "$PGBIN/initdb" -D "$PGDATA" -U postgres -E UTF8 --locale=C.UTF-8 \
        --auth-local=peer --auth-host=scram-sha-256 >/dev/null
fi

gosu postgres "$PGBIN/pg_ctl" -D "$PGDATA" -w -t 120 -l "$DATA/logs/postgres.log" start \
    -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=$SOCKET" >/dev/null
log "PostgreSQL $PG_MAJOR started"

stop_postgres() {
    gosu postgres "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true
    log "PostgreSQL stopped"
}

psql_admin() { gosu postgres "$PGBIN/psql" -h "$SOCKET" -U postgres -v ON_ERROR_STOP=1 -qtA "$@"; }

if [ "$(psql_admin -c "SELECT 1 FROM pg_roles WHERE rolname='curatio'")" != "1" ]; then
    psql_admin -c "CREATE ROLE curatio LOGIN PASSWORD '$POSTGRES_PASSWORD'"
else
    psql_admin -c "ALTER ROLE curatio PASSWORD '$POSTGRES_PASSWORD'"
fi
if [ "$(psql_admin -c "SELECT 1 FROM pg_database WHERE datname='curatio'")" != "1" ]; then
    psql_admin -c "CREATE DATABASE curatio OWNER curatio"
fi

export DATABASE_URL="postgresql://curatio:${POSTGRES_PASSWORD}@127.0.0.1:5432/curatio"
unset GENERATED_SECRET_KEY POSTGRES_PASSWORD

# --- Curatio ------------------------------------------------------------------
PORT="${PORT:-8000}"
gosu appuser uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --no-access-log "$@" &
app_pid=$!

shutdown() {
    log "Stopping"
    kill -TERM "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
    stop_postgres
    exit 0
}
trap shutdown TERM INT

status=0
wait "$app_pid" || status=$?
# The server exited on its own (a configuration error, say): stop cleanly too.
stop_postgres
exit "$status"
