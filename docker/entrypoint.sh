#!/bin/bash
# Container entrypoint: provision secrets, then drop privileges and exec the server.
#
# Runs as root only long enough to write the secrets volume; the server itself
# always runs as the unprivileged `appuser`.
set -euo pipefail

SECRETS_DIR="${PALIMPSEST_SECRETS_DIR:-/app/.secrets}"
OPERATOR_KEY_FROM_ENV="${PALIMPSEST_GATE_OPERATOR_KEY:+1}"

log() { printf '[entrypoint] %s\n' "$1"; }

# The secrets directory must exist and belong to appuser before the privilege
# drop at the end (`setpriv` ships with util-linux in the Debian slim image).
if [ "$(id -u)" = "0" ]; then
    install -d -o appuser -g appuser -m 0700 "$SECRETS_DIR"
fi

# --- secret provisioning ---------------------------------------------------
# Each secret comes from, in order: an explicit environment variable, the value
# persisted by a previous boot, or a freshly generated one. The master key must
# never be regenerated once used: every stored credential depends on it.
ensure_secret() {
    local name="$1" envvar="$2" generator="$3"
    local file="$SECRETS_DIR/$name"
    local value="${!envvar:-}"

    if [ -z "$value" ] && [ -s "$file" ]; then
        value="$(cat "$file")"
        log "reusing persisted $name"
    fi

    if [ -z "$value" ]; then
        value="$("$generator")"
        printf '%s' "$value" >"$file"
        chmod 0600 "$file"
        log "generated a new $name (persisted in $SECRETS_DIR)"
    fi

    export "$envvar=$value"
}

generate_master_key() {
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
}

generate_operator_key() {
    python -c "import secrets; print('pgw_opk_' + secrets.token_urlsafe(32))"
}

ensure_secret master_key PALIMPSEST_GATE_MASTER_KEY generate_master_key
ensure_secret operator_key PALIMPSEST_GATE_OPERATOR_KEY generate_operator_key

if [ -z "${PALIMPSEST_PROVIDER_API_KEY:-}" ]; then
    log "no provider API key set — add PALIMPSEST_PROVIDER_API_KEY to serve /v1/chat/completions"
    log "the API and dashboard work without it; chat requests will be rejected"
fi

# The operator key is printed so a local run can sign in straight from the log.
# Set PALIMPSEST_LOG_OPERATOR_KEY=false wherever logs are shipped or shared:
# the key grants the whole operator realm.
if [ "${PALIMPSEST_LOG_OPERATOR_KEY:-true}" = "true" ]; then
    log "================================================================"
    log "  Dashboard:     http://localhost:${PALIMPSEST_PORT:-8000}/dashboard/"
    log "  Operator key:  $PALIMPSEST_GATE_OPERATOR_KEY"
    log "================================================================"
elif [ -n "$OPERATOR_KEY_FROM_ENV" ]; then
    log "operator key: taken from PALIMPSEST_GATE_OPERATOR_KEY"
else
    log "operator key: read it with  docker compose exec app cat $SECRETS_DIR/operator_key"
fi
log "starting: $*"

if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi
exec "$@"