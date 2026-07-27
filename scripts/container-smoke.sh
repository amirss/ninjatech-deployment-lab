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
export NINJATECH_WORKER_POLL_INTERVAL_SECONDS="0.1"
export NINJATECH_WORKER_LEASE_DURATION_SECONDS="3.0"
export NINJATECH_WORKER_HEARTBEAT_INTERVAL_SECONDS="0.5"
export NINJATECH_WORKER_HANDLER_TIMEOUT_SECONDS="2.0"
export NINJATECH_WORKER_SHUTDOWN_GRACE_SECONDS="1.0"
export NINJATECH_WORKER_RETRY_BASE_SECONDS="0.1"
export NINJATECH_WORKER_RETRY_CAP_SECONDS="0.2"
export NINJATECH_ENABLE_DIAGNOSTIC_HANDLER="true"

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

create_task() {
    local idempotency_key=$1
    local input_json=$2
    local response=""
    local response_body=""
    local response_status=""

    response="$(
        curl \
            --silent \
            --show-error \
            --request POST \
            --header "Content-Type: application/json" \
            --header "Idempotency-Key: ${idempotency_key}" \
            --data "{\"task_type\":\"diagnostic\",\"input\":${input_json}}" \
            --write-out $'\n%{http_code}' \
            "${api_url}/tasks"
    )"
    response_status="${response##*$'\n'}"
    response_body="${response%$'\n'*}"
    if [[ "${response_status}" != "201" ]]; then
        printf 'Task creation returned HTTP %s\n' "${response_status}" >&2
        return 1
    fi
    python3 -c 'import json, sys; print(json.loads(sys.argv[1])["id"])' "${response_body}"
}

approve_task() {
    local task_id=$1
    local response_status=""
    response_status="$(
        curl \
            --silent \
            --output /dev/null \
            --write-out '%{http_code}' \
            --request POST \
            "${api_url}/tasks/${task_id}/approve"
    )"
    if [[ "${response_status}" != "200" ]]; then
        printf 'Task approval returned HTTP %s\n' "${response_status}" >&2
        return 1
    fi
}

cancel_running_task() {
    local task_id=$1
    local response_status=""
    response_status="$(
        curl \
            --silent \
            --output /dev/null \
            --write-out '%{http_code}' \
            --request POST \
            "${api_url}/tasks/${task_id}/cancel"
    )"
    if [[ "${response_status}" != "202" ]]; then
        printf 'Running task cancellation returned HTTP %s\n' "${response_status}" >&2
        return 1
    fi
}

wait_for_task_status() {
    local task_id=$1
    local expected_status=$2
    local actual_status=""
    local response_body=""

    for _ in {1..120}; do
        response_body="$(curl --silent --show-error "${api_url}/tasks/${task_id}")"
        actual_status="$(
            python3 -c \
                'import json, sys; print(json.loads(sys.argv[1])["status"])' \
                "${response_body}"
        )"
        if [[ "${actual_status}" == "${expected_status}" ]]; then
            printf 'Verified task %s reached %s\n' "${task_id}" "${expected_status}"
            return 0
        fi
        sleep 0.25
    done

    printf \
        'Task %s did not reach %s; last status was %s\n' \
        "${task_id}" \
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

announce "Starting the explicitly enabled non-production diagnostic worker"
docker compose up --detach worker

announce "Executing a diagnostic success task"
success_task_id="$(create_task "smoke-success-${GITHUB_RUN_ID:-local}" '{"mode":"success"}')"
approve_task "${success_task_id}"
wait_for_task_status "${success_task_id}" "succeeded"

announce "Executing a diagnostic retry-then-success task"
retry_task_id="$(create_task "smoke-retry-${GITHUB_RUN_ID:-local}" '{"mode":"retry_then_success","failures":1}')"
approve_task "${retry_task_id}"
wait_for_task_status "${retry_task_id}" "succeeded"

retry_task_response="$(curl --silent --show-error "${api_url}/tasks/${retry_task_id}")"
retry_attempt_count="$(
    python3 -c \
        'import json, sys; print(json.loads(sys.argv[1])["attempt_count"])' \
        "${retry_task_response}"
)"
if [[ "${retry_attempt_count}" -ne 2 ]]; then
    printf 'Retry task used %s attempts instead of 2\n' "${retry_attempt_count}" >&2
    exit 1
fi

announce "Executing a cooperative customer-cancellation task"
cancellation_task_id="$(
    create_task \
        "smoke-cancellation-${GITHUB_RUN_ID:-local}" \
        '{"mode":"wait_for_cancellation","checkpoint_seconds":0.05}'
)"
approve_task "${cancellation_task_id}"
wait_for_task_status "${cancellation_task_id}" "running"
cancel_running_task "${cancellation_task_id}"
wait_for_task_status "${cancellation_task_id}" "cancelled"

announce "Stopping PostgreSQL to verify liveness and fail-closed readiness"
docker compose stop --timeout 10 db

expect_http_status "/health" "200" "GET /health with PostgreSQL unavailable"
expect_http_status "/ready" "503" "GET /ready with PostgreSQL unavailable"

announce "Container smoke test passed"
