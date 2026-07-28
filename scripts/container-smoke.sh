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
export SIMULATOR_PORT="${SMOKE_SIMULATOR_PORT:-18090}"
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
export NINJATECH_WORKER_DEFAULT_MAX_ATTEMPTS="6"
export NINJATECH_ENABLE_DIAGNOSTIC_HANDLER="true"
export NINJATECH_ENABLE_DEPLOYMENT_CONTEXT_SYNC="true"
export NINJATECH_DEPLOYMENT_SCOPE_ID="smoke-controlled-scope"
export NINJATECH_DEPLOYMENT_ALLOWED_SERVICE_IDS='["payments-api","blocked-service"]'
export NINJATECH_DEPLOYMENT_ALLOWED_GITHUB_REPOSITORIES='["customer/example-service","customer/blocked-service"]'
export NINJATECH_DEPLOYMENT_ALLOWED_JIRA_PROJECTS='["ENG"]'
export NINJATECH_DEPLOYMENT_MINIMUM_POLICY_VERSION="7"
export NINJATECH_SERVICE_CATALOG_BASE_URL="http://simulator:8090/catalog"
export NINJATECH_JIRA_BASE_URL="http://simulator:8090/jira"
export NINJATECH_GITHUB_BASE_URL="http://simulator:8090/github"
export NINJATECH_GITHUB_EXPECTED_LOGIN="simulator-bot"
export NINJATECH_ENABLE_SLACK_NOTIFICATION="true"
export NINJATECH_SLACK_BASE_URL="http://simulator:8090/slack"
export NINJATECH_SLACK_BOT_TOKEN="smoke-slack-credential"
export NINJATECH_SLACK_EXPECTED_TEAM_ID="T1234567890"
export NINJATECH_SLACK_EXPECTED_USER_ID="U1234567890"
export NINJATECH_SLACK_EXPECTED_BOT_ID="B1234567890"
export NINJATECH_DEPLOYMENT_ALLOWED_SLACK_CHANNELS='["C1234567890","CUNKNOWN001","CPERMFAIL01","CDELAYED001","CRATELIMIT1"]'
export NINJATECH_SLACK_MAX_TEXT_CHARS="1000"
export NINJATECH_SLACK_WRITE_TIMEOUT_SECONDS="1.0"
export NINJATECH_SERVICE_CATALOG_TOKEN="smoke-catalog-credential"
export NINJATECH_JIRA_API_TOKEN="smoke-jira-credential"
export NINJATECH_GITHUB_TOKEN="smoke-github-credential"
export NINJATECH_INTEGRATION_PROVIDER_WRITE_TIMEOUT_SECONDS="1.0"
export NINJATECH_INTEGRATION_SETTLEMENT_DELAY_SECONDS="3.0"
export SIMULATOR_ACCEPT_DELAY_SECONDS="2.0"
export SIMULATOR_DELAYED_RESPONSE_SECONDS="10.0"
export SIMULATOR_SLACK_RESPONSE_DELAY_SECONDS="10.0"

api_url="http://127.0.0.1:${APP_PORT}"
simulator_url="http://127.0.0.1:${SIMULATOR_PORT}"

cleanup() {
    local exit_code=$?
    trap - EXIT
    docker compose --profile integration down --volumes --remove-orphans >/dev/null 2>&1 || true
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
    local task_type=$2
    local input_json=$3
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
            --data "{\"task_type\":\"${task_type}\",\"input\":${input_json}}" \
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

simulator_evidence_field() {
    local field=$1
    local response_body=""
    response_body="$(curl --silent --show-error "${simulator_url}/__simulator/evidence")"
    python3 -c \
        'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' \
        "${response_body}" \
        "${field}"
}

wait_for_simulator_counter() {
    local field=$1
    local minimum=$2
    local actual=0
    for _ in {1..80}; do
        actual="$(simulator_evidence_field "${field}")"
        if [[ "${actual}" -ge "${minimum}" ]]; then
            return 0
        fi
        sleep 0.1
    done
    printf 'Simulator counter %s did not reach %s; last value was %s\n' \
        "${field}" "${minimum}" "${actual}" >&2
    return 1
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
docker compose --profile integration build app simulator

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

announce "Starting the development/test-only provider simulator"
docker compose --profile integration up --detach --wait simulator

announce "Starting the explicitly enabled non-production diagnostic worker"
docker compose up --detach worker

announce "Executing a diagnostic success task"
success_task_id="$(
    create_task \
        "smoke-success-${GITHUB_RUN_ID:-local}" \
        "diagnostic" \
        '{"mode":"success"}'
)"
approve_task "${success_task_id}"
wait_for_task_status "${success_task_id}" "succeeded"

announce "Executing a diagnostic retry-then-success task"
retry_task_id="$(
    create_task \
        "smoke-retry-${GITHUB_RUN_ID:-local}" \
        "diagnostic" \
        '{"mode":"retry_then_success","failures":1}'
)"
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
        "diagnostic" \
        '{"mode":"wait_for_cancellation","checkpoint_seconds":0.05}'
)"
approve_task "${cancellation_task_id}"
wait_for_task_status "${cancellation_task_id}" "running"
cancel_running_task "${cancellation_task_id}"
wait_for_task_status "${cancellation_task_id}" "cancelled"

ready_input='{"jira_issue_key":"ENG-123","github_repository":"customer/example-service","github_issue_number":42,"service_id":"payments-api","publish_slack_notification":true,"slack_channel_id":"C1234567890"}'

announce "Executing authoritative GitHub plus secondary Slack success"
github_create_before="$(simulator_evidence_field "create_calls")"
slack_message_before="$(simulator_evidence_field "slack_message_count")"
deployment_task_id="$(
    create_task \
        "smoke-deployment-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        "${ready_input}"
)"
approve_task "${deployment_task_id}"
wait_for_task_status "${deployment_task_id}" "succeeded"
github_create_after="$(simulator_evidence_field "create_calls")"
if [[ $((github_create_after - github_create_before)) -ne 1 ]]; then
    printf 'Authoritative workflow did not create exactly one GitHub comment\n' >&2
    exit 1
fi
slack_message_after="$(simulator_evidence_field "slack_message_count")"
if [[ $((slack_message_after - slack_message_before)) -ne 1 ]]; then
    printf 'Secondary workflow did not create exactly one Slack message\n' >&2
    exit 1
fi

deployment_response="$(curl --silent --show-error "${api_url}/tasks/${deployment_task_id}")"
python3 -c \
    'import json, sys
task = json.loads(sys.argv[1])
assert task["result"]["decision"]["outcome"] == "ready"
assert task["result"]["authoritative_github_action"]["provider"] == "github"
slack = task["result"]["secondary_slack_notification"]
assert slack["state"] == "succeeded"
assert slack["action"]["provider"] == "slack"
assert slack["action"]["provider_resource_identifier"]' \
    "${deployment_response}"

announce "Re-submitting the same business action scope without duplicating the comment"
replay_task_id="$(
    create_task \
        "smoke-deployment-replay-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        "${ready_input}"
)"
approve_task "${replay_task_id}"
wait_for_task_status "${replay_task_id}" "succeeded"
if [[ "$(simulator_evidence_field "create_calls")" -ne "${github_create_after}" ]]; then
    printf 'Independent task created a duplicate authoritative GitHub comment\n' >&2
    exit 1
fi
if [[ "$(simulator_evidence_field "slack_message_count")" -ne "${slack_message_after}" ]]; then
    printf 'Independent task created a duplicate Slack notification\n' >&2
    exit 1
fi

announce "Preserving GitHub success when Slack acceptance is ambiguous"
slack_unknown_before="$(simulator_evidence_field "slack_message_count")"
slack_unknown_task_id="$(
    create_task \
        "smoke-slack-unknown-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-124","github_repository":"customer/example-service","github_issue_number":43,"service_id":"payments-api","publish_slack_notification":true,"slack_channel_id":"CUNKNOWN001"}'
)"
approve_task "${slack_unknown_task_id}"
wait_for_task_status "${slack_unknown_task_id}" "succeeded"
slack_unknown_response="$(curl --silent --show-error "${api_url}/tasks/${slack_unknown_task_id}")"
python3 -c \
    'import json, sys
task = json.loads(sys.argv[1])
assert task["result"]["authoritative_github_action"]["provider_resource_identifier"]
assert task["result"]["secondary_slack_notification"]["state"] == "outcome_unknown"' \
    "${slack_unknown_response}"
slack_unknown_after="$(simulator_evidence_field "slack_message_count")"
if [[ $((slack_unknown_after - slack_unknown_before)) -ne 1 ]]; then
    printf 'Ambiguous Slack acceptance did not persist exactly one message\n' >&2
    exit 1
fi

slack_unknown_replay_id="$(
    create_task \
        "smoke-slack-unknown-replay-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-124","github_repository":"customer/example-service","github_issue_number":43,"service_id":"payments-api","publish_slack_notification":true,"slack_channel_id":"CUNKNOWN001"}'
)"
approve_task "${slack_unknown_replay_id}"
wait_for_task_status "${slack_unknown_replay_id}" "succeeded"
if [[ "$(simulator_evidence_field "slack_message_count")" -ne "${slack_unknown_after}" ]]; then
    printf 'Unknown Slack outcome was blindly resent\n' >&2
    exit 1
fi

announce "Preserving GitHub success after a permanent Slack failure"
slack_permanent_task_id="$(
    create_task \
        "smoke-slack-permanent-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-124","github_repository":"customer/example-service","github_issue_number":44,"service_id":"payments-api","publish_slack_notification":true,"slack_channel_id":"CPERMFAIL01"}'
)"
approve_task "${slack_permanent_task_id}"
wait_for_task_status "${slack_permanent_task_id}" "succeeded"
slack_permanent_response="$(curl --silent --show-error "${api_url}/tasks/${slack_permanent_task_id}")"
python3 -c \
    'import json, sys
task = json.loads(sys.argv[1])
assert task["result"]["authoritative_github_action"]["provider_resource_identifier"]
assert task["result"]["secondary_slack_notification"]["state"] == "permanent_failure"
assert task["result"]["secondary_slack_notification"]["safe_error_code"] == "slack_channel_not_found"' \
    "${slack_permanent_response}"

announce "Reconciling an accepted write with a malformed successful response"
ambiguous_before="$(simulator_evidence_field "comment_count")"
ambiguous_task_id="$(
    create_task \
        "smoke-ambiguous-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-198","github_repository":"customer/example-service","github_issue_number":198,"service_id":"payments-api","publish_slack_notification":false}'
)"
approve_task "${ambiguous_task_id}"
wait_for_task_status "${ambiguous_task_id}" "succeeded"
ambiguous_after="$(simulator_evidence_field "comment_count")"
if [[ $((ambiguous_after - ambiguous_before)) -ne 1 ]]; then
    printf 'Ambiguous-write reconciliation produced an unexpected comment count\n' >&2
    exit 1
fi

announce "Proving policy-blocked work performs no GitHub or Jira access"
blocked_github_before="$(simulator_evidence_field "github_read_calls")"
blocked_jira_before="$(simulator_evidence_field "jira_calls")"
blocked_create_before="$(simulator_evidence_field "create_calls")"
blocked_slack_identity_before="$(simulator_evidence_field "slack_identity_calls")"
blocked_slack_post_before="$(simulator_evidence_field "slack_post_calls")"
blocked_task_id="$(
    create_task \
        "smoke-blocked-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-123","github_repository":"customer/blocked-service","github_issue_number":42,"service_id":"blocked-service","publish_slack_notification":true,"slack_channel_id":"C1234567890"}'
)"
approve_task "${blocked_task_id}"
wait_for_task_status "${blocked_task_id}" "succeeded"
if [[ "$(simulator_evidence_field "github_read_calls")" -ne "${blocked_github_before}" ]] \
    || [[ "$(simulator_evidence_field "jira_calls")" -ne "${blocked_jira_before}" ]] \
    || [[ "$(simulator_evidence_field "create_calls")" -ne "${blocked_create_before}" ]] \
    || [[ "$(simulator_evidence_field "slack_identity_calls")" -ne "${blocked_slack_identity_before}" ]] \
    || [[ "$(simulator_evidence_field "slack_post_calls")" -ne "${blocked_slack_post_before}" ]]; then
    printf 'Policy-blocked workflow accessed a downstream provider\n' >&2
    exit 1
fi

announce "Proving delayed provider acceptance across lease expiry does not duplicate"
delayed_create_before="$(simulator_evidence_field "create_calls")"
delayed_comment_before="$(simulator_evidence_field "comment_count")"
delayed_task_id="$(
    create_task \
        "smoke-delayed-${GITHUB_RUN_ID:-local}" \
        "deployment_context_sync" \
        '{"jira_issue_key":"ENG-199","github_repository":"customer/example-service","github_issue_number":199,"service_id":"payments-api","publish_slack_notification":false}'
)"
approve_task "${delayed_task_id}"
wait_for_simulator_counter "create_calls" "$((delayed_create_before + 1))"
docker compose kill worker
docker compose up --detach worker
wait_for_task_status "${delayed_task_id}" "succeeded"
delayed_create_after="$(simulator_evidence_field "create_calls")"
delayed_comment_after="$(simulator_evidence_field "comment_count")"
if [[ $((delayed_create_after - delayed_create_before)) -ne 1 ]] \
    || [[ $((delayed_comment_after - delayed_comment_before)) -ne 1 ]]; then
    printf 'Delayed accepted write was issued more than once\n' >&2
    exit 1
fi

announce "Stopping PostgreSQL to verify liveness and fail-closed readiness"
docker compose stop --timeout 10 db

expect_http_status "/health" "200" "GET /health with PostgreSQL unavailable"
expect_http_status "/ready" "503" "GET /ready with PostgreSQL unavailable"

announce "Container smoke test passed"
