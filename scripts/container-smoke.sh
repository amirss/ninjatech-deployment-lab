#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ninjatech-smoke-${GITHUB_RUN_ID:-$$}}"
export POSTGRES_USER="ninjatech_smoke"
export POSTGRES_PASSWORD="smoke-${RANDOM}-${RANDOM}-$$"
export POSTGRES_DB="ninjatech_smoke"
export POSTGRES_PORT="${SMOKE_POSTGRES_PORT:-15432}"
export APP_PORT="${SMOKE_APP_PORT:-18080}"
export NINJATECH_APP_NAME="NinjaTech Deployment Lab Smoke Test"
export NINJATECH_ENVIRONMENT="test"
export NINJATECH_LOG_LEVEL="INFO"
export NINJATECH_DB_READY_TIMEOUT_SECONDS="2.0"
export NINJATECH_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"

api_url="http://127.0.0.1:${APP_PORT}"

cleanup() {
    local exit_code=$?
    trap - EXIT
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    exit "${exit_code}"
}
trap cleanup EXIT

announce() {
    printf '==> %s\n' "$1"
}

expect_http_status() {
    local path=$1
    local expected_status=$2
    local description=$3
    local actual_status=""

    for _ in {1..20}; do
        actual_status="$(
            curl \
                --silent \
                --output /dev/null \
                --write-out '%{http_code}' \
                --max-time 5 \
                "${api_url}${path}" || true
        )"
        if [[ "${actual_status}" == "${expected_status}" ]]; then
            printf 'Verified %s returned HTTP %s\n' "${description}" "${expected_status}"
            return 0
        fi
        sleep 1
    done

    printf \
        'Expected %s to return HTTP %s; last status was %s\n' \
        "${description}" \
        "${expected_status}" \
        "${actual_status:-unavailable}" >&2
    return 1
}

announce "Validating Docker Compose configuration"
docker compose config --quiet

announce "Building the application image"
docker compose build app

announce "Verifying the application image runs as a non-root user"
runtime_uid="$(docker compose run --rm --no-deps app id -u)"
if [[ "${runtime_uid}" == "0" ]]; then
    printf 'Application image unexpectedly runs as root\n' >&2
    exit 1
fi

announce "Starting PostgreSQL and waiting for its healthcheck"
docker compose up --detach --wait db

announce "Applying Alembic migrations to the fresh database"
docker compose run --rm --no-deps app alembic upgrade head

announce "Starting the API and waiting for its healthcheck"
docker compose up --detach --wait app

expect_http_status "/health" "200" "GET /health with PostgreSQL available"
expect_http_status "/ready" "200" "GET /ready with PostgreSQL available"

announce "Stopping PostgreSQL to verify liveness and fail-closed readiness"
docker compose stop --timeout 10 db

expect_http_status "/health" "200" "GET /health with PostgreSQL unavailable"
expect_http_status "/ready" "503" "GET /ready with PostgreSQL unavailable"

announce "Container smoke test passed"

