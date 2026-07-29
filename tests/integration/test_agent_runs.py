from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.code_proposals.domain import (
    EvidenceRole,
    FileReadEvidenceSummary,
    FileReadToolResultSummary,
    FinishActionSummary,
    ModelConfidenceBand,
    PathSearchResultSummary,
    PathSearchToolResultSummary,
    ProposalOutcome,
    ValidatedCodeChange,
)
from ninjatech_deployment_lab.code_proposals.model import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepKind,
    AgentStepSource,
)
from ninjatech_deployment_lab.code_proposals.persistence import (
    AgentRunRepository,
    AgentRunSpec,
    AgentStepDraft,
    RunReservationKind,
    StepSourceLink,
)
from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.database import (
    create_database_engine,
    create_session_factory,
)
from ninjatech_deployment_lab.integrations.domain import DataClassification
from ninjatech_deployment_lab.integrations.persistence import SourceArtifactRepository
from ninjatech_deployment_lab.tasks.service import TaskService
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    ExecutionFence,
    ExecutionInvariantError,
)
from ninjatech_deployment_lab.worker.repository import WorkerRepository

pytestmark = pytest.mark.postgres


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    worker: str,
) -> ClaimedTask:
    async with session_factory() as session:
        created = await TaskService(session).create_task(
            idempotency_key=key,
            task_type="code_change_proposal",
            task_input={
                "jira_issue_key": "ENG-123",
                "github_repository": "customer/service",
                "github_issue_number": 42,
                "service_id": "payments",
            },
        )
        await TaskService(session).approve_task(created.task.id)
    claimed = await WorkerRepository(session_factory).claim_one(
        supported_task_types=("code_change_proposal",),
        worker_id=worker,
        lease_duration_seconds=30,
    )
    assert claimed is not None
    return claimed


def _spec(
    scope: str = "scope-1",
    *,
    prompt_contract_hash: str = "b" * 64,
    source_snapshot_hash: str = "a" * 64,
) -> AgentRunSpec:
    return AgentRunSpec(
        run_scope_key=scope,
        workflow_version="code_change_proposal:v1",
        source_snapshot_hash=source_snapshot_hash,
        provider="recorded",
        model_name="recorded-v1",
        prompt_template_version="v1",
        prompt_contract_hash=prompt_contract_hash,
        maximum_steps=8,
        data_classification="internal",
        retention_until=datetime.now(UTC) + timedelta(days=30),
    )


async def _database(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_database_engine(Settings(database_url=database_url, environment="test"))
    return engine, create_session_factory(engine)


def test_concurrent_reservation_creates_one_run_and_reports_active_busy(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first, second = await asyncio.gather(
                _claim(sessions, key="agent-concurrent-a", worker="worker-a"),
                _claim(sessions, key="agent-concurrent-b", worker="worker-b"),
            )
            repository = AgentRunRepository(sessions)
            results = await asyncio.gather(
                repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec()),
                repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec()),
            )
            assert {item.kind for item in results} == {
                RunReservationKind.CREATED,
                RunReservationKind.ACTIVE_BUSY,
            }
            async with sessions() as session:
                assert await session.scalar(select(func.count(AgentRun.id))) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completed_run_replays_for_independent_task_without_model_response(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first = await _claim(sessions, key="agent-complete-a", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec())
            await repository.mark_running(fence=first.execution_fence, run_id=created.run.id)
            await repository.complete(
                fence=first.execution_fence,
                run_id=created.run.id,
                outcome=ProposalOutcome.NEEDS_HUMAN_REVIEW,
                proposal=None,
            )
            second = await _claim(sessions, key="agent-complete-b", worker="worker-b")
            replay = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert replay.kind is RunReservationKind.COMPLETED_REPLAY
            assert await repository.completed_response_count(created.run.id) == 0
            async with sessions() as session:
                run = await session.get(AgentRun, created.run.id)
                assert run is not None
                assert run.bound_task_id == second.task_id
                kinds = (
                    await session.execute(
                        select(AgentStep.step_kind)
                        .where(AgentStep.agent_run_id == created.run.id)
                        .order_by(AgentStep.sequence_number)
                    )
                ).scalars()
                assert AgentStepKind.COMPLETED_RUN_REPLAYED.value in set(kinds)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_run_observes_database_not_before_and_resumes_same_run(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first = await _claim(sessions, key="agent-retry-a", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec())
            await repository.mark_retryable(
                fence=first.execution_fence,
                run_id=created.run.id,
                safe_error_code="provider_rate_limited",
                delay_seconds=60,
            )
            second = await _claim(sessions, key="agent-retry-b", worker="worker-b")
            waiting = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert waiting.kind is RunReservationKind.RETRY_WAIT
            async with sessions.begin() as session:
                await session.execute(
                    update(AgentRun)
                    .where(AgentRun.id == created.run.id)
                    .values(resume_not_before=func.clock_timestamp() - text("interval '1 second'"))
                )
            resumed = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert resumed.kind is RunReservationKind.RESUME
            assert resumed.run.id == created.run.id
            assert resumed.run.status == AgentRunStatus.RESERVED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_interrupted_run_rebinds_without_poisoning_semantic_scope(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first = await _claim(sessions, key="agent-interrupt-a", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec())
            await repository.interrupt(
                fence=first.execution_fence,
                run_id=created.run.id,
                safe_error_code="customer_cancelled",
            )
            second = await _claim(sessions, key="agent-interrupt-b", worker="worker-b")
            rebound = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert rebound.kind is RunReservationKind.REBIND_INTERRUPTED
            assert rebound.run.id == created.run.id
            assert rebound.run.originating_task_id == first.task_id
            assert rebound.run.bound_task_id == second.task_id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_expired_bound_owner_can_be_rebound_but_current_owner_stays_busy(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first = await _claim(sessions, key="agent-expiry-a", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec())
            await repository.mark_running(fence=first.execution_fence, run_id=created.run.id)
            second = await _claim(sessions, key="agent-expiry-b", worker="worker-b")
            busy = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert busy.kind is RunReservationKind.ACTIVE_BUSY
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE tasks SET lease_expires_at = clock_timestamp() - "
                        "interval '1 second' WHERE id = :task_id"
                    ),
                    {"task_id": first.task_id},
                )
            rebound = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert rebound.kind is RunReservationKind.REBIND_ABANDONED
            assert rebound.run.id == created.run.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_failed_run_is_terminal_and_not_overwritten(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            first = await _claim(sessions, key="agent-failed-a", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=first.execution_fence, spec=_spec())
            await repository.fail(
                fence=first.execution_fence,
                run_id=created.run.id,
                safe_error_code="provider_contract_failure",
            )
            second = await _claim(sessions, key="agent-failed-b", worker="worker-b")
            result = await repository.reserve_or_rebind(fence=second.execution_fence, spec=_spec())
            assert result.kind is RunReservationKind.TERMINAL_FAILURE
            assert result.run.safe_error_code == "provider_contract_failure"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_prompt_or_source_change_uses_distinct_run_scope(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claims = [
                await _claim(sessions, key=f"agent-scope-{index}", worker=f"worker-{index}")
                for index in range(3)
            ]
            repository = AgentRunRepository(sessions)
            specs = (
                _spec("scope-base"),
                _spec("scope-prompt", prompt_contract_hash="c" * 64),
                _spec("scope-source", source_snapshot_hash="d" * 64),
            )
            results = [
                await repository.reserve_or_rebind(fence=claim.execution_fence, spec=spec)
                for claim, spec in zip(claims, specs, strict=True)
            ]
            assert len({item.run.id for item in results}) == 3
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_step_source_links_are_relational_and_invalid_link_rolls_back_finalization(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claim = await _claim(sessions, key="agent-sources", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=claim.execution_fence, spec=_spec())
            artifact = await SourceArtifactRepository(sessions, maximum_bytes=4096).record(
                fence=claim.execution_fence,
                provider="jira",
                resource_type="work_item",
                provider_resource_identifier="ENG-123",
                source_url="https://jira.example/browse/ENG-123",
                source_version="jira-v1",
                data_classification=DataClassification.INTERNAL,
                redaction_applied=True,
                normalized_payload={"key": "ENG-123"},
                fetched_at=datetime.now(UTC),
            )
            step = await repository.append_step(
                fence=claim.execution_fence,
                run_id=created.run.id,
                draft=AgentStepDraft(
                    kind=AgentStepKind.FILE_READ_COMPLETED,
                    logical_step_number=1,
                    tool_name="read_repository_files",
                    tool_result_summary=FileReadToolResultSummary(
                        kind="file_read",
                        files=(
                            FileReadEvidenceSummary(
                                evidence_handle="E-1234567890abcdef",
                                path="src/app.py",
                                blob_sha="a" * 40,
                                source_version="a" * 40,
                                line_count=1,
                                byte_size=12,
                                content_hash="b" * 64,
                            ),
                        ),
                    ),
                    completes_logical_step=True,
                    increments_tool_call=True,
                ),
                sources=(
                    StepSourceLink(
                        source_artifact_id=artifact.id,
                        role=EvidenceRole.REQUIREMENT_INPUT,
                    ),
                ),
            )
            async with sessions() as session:
                links = (
                    (
                        await session.execute(
                            select(AgentStepSource).where(AgentStepSource.agent_step_id == step.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(links) == 1
                assert links[0].source_artifact_id == artifact.id

            with pytest.raises(ExecutionInvariantError, match="source artifact"):
                await repository.complete(
                    fence=claim.execution_fence,
                    run_id=created.run.id,
                    outcome=ProposalOutcome.NEEDS_HUMAN_REVIEW,
                    proposal=None,
                    sources=(
                        StepSourceLink(
                            source_artifact_id=uuid4(),
                            role=EvidenceRole.PROPOSAL_CITATION,
                        ),
                    ),
                )
            async with sessions() as session:
                run = await session.get(AgentRun, created.run.id)
                assert run is not None
                assert run.status == AgentRunStatus.RESERVED.value
                assert (
                    await session.scalar(
                        select(func.count(AgentStep.id)).where(AgentStep.agent_run_id == run.id)
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_stale_worker_cannot_append_change_counters_or_finalize(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claim = await _claim(sessions, key="agent-stale", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=claim.execution_fence, spec=_spec())
            stale = ExecutionFence(
                task_id=claim.task_id,
                attempt_id=claim.attempt_id,
                attempt_number=claim.attempt_number,
                worker_id="stale-worker",
                lease_token_hash=claim.lease_token_hash,
            )
            with pytest.raises(ExecutionInvariantError):
                await repository.append_step(
                    fence=stale,
                    run_id=created.run.id,
                    draft=AgentStepDraft(
                        kind=AgentStepKind.MODEL_RESPONSE_RECORDED,
                        logical_step_number=1,
                        response_fingerprint="c" * 64,
                        increments_model_call=True,
                    ),
                )
            with pytest.raises(ExecutionInvariantError):
                await repository.complete(
                    fence=stale,
                    run_id=created.run.id,
                    outcome=ProposalOutcome.REFUSED,
                    proposal=None,
                )
            async with sessions() as session:
                run = await session.get(AgentRun, created.run.id)
                assert run is not None
                assert run.model_call_count == 0
                assert run.status == AgentRunStatus.RESERVED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_step_appends_keep_unique_order_and_consistent_counters(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claim = await _claim(sessions, key="agent-step-order", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=claim.execution_fence, spec=_spec())
            await asyncio.gather(
                repository.append_step(
                    fence=claim.execution_fence,
                    run_id=created.run.id,
                    draft=AgentStepDraft(
                        kind=AgentStepKind.PATH_SEARCH_COMPLETED,
                        logical_step_number=1,
                        tool_name="search_repository_paths",
                        tool_result_summary=PathSearchToolResultSummary(
                            kind="path_search",
                            results=(
                                PathSearchResultSummary(
                                    path="src/a.py",
                                    blob_sha="a" * 40,
                                    byte_size=10,
                                ),
                            ),
                        ),
                        completes_logical_step=True,
                        increments_tool_call=True,
                    ),
                ),
                repository.append_step(
                    fence=claim.execution_fence,
                    run_id=created.run.id,
                    draft=AgentStepDraft(
                        kind=AgentStepKind.FILE_READ_COMPLETED,
                        logical_step_number=2,
                        tool_name="read_repository_files",
                        tool_result_summary=FileReadToolResultSummary(
                            kind="file_read",
                            files=(
                                FileReadEvidenceSummary(
                                    evidence_handle="E-1234567890abcdef",
                                    path="src/a.py",
                                    blob_sha="a" * 40,
                                    source_version="a" * 40,
                                    line_count=1,
                                    byte_size=10,
                                    content_hash="b" * 64,
                                ),
                            ),
                        ),
                        completes_logical_step=True,
                        increments_tool_call=True,
                    ),
                ),
            )
            async with sessions() as session:
                run = await session.get(AgentRun, created.run.id)
                assert run is not None
                assert run.completed_steps == 2
                assert run.tool_call_count == 2
                sequences = (
                    (
                        await session.execute(
                            select(AgentStep.sequence_number)
                            .where(AgentStep.agent_run_id == run.id)
                            .order_by(AgentStep.sequence_number)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert sequences == [1, 2, 3]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_cancellation_records_interruption_through_valid_fence(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claim = await _claim(sessions, key="agent-cancel", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=claim.execution_fence, spec=_spec())
            async with sessions() as session:
                await TaskService(session).cancel_task(claim.task_id)
            interrupted = await repository.interrupt(
                fence=claim.execution_fence,
                run_id=created.run.id,
                safe_error_code="customer_cancelled",
            )
            assert interrupted.status == AgentRunStatus.INTERRUPTED.value
            assert interrupted.safe_error_code == "customer_cancelled"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_database_engine_hides_agent_run_bound_parameters(
    postgres_database_url: str, clean_tasks: None
) -> None:
    engine = create_database_engine(
        Settings(database_url=postgres_database_url, environment="test")
    )
    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        asyncio.run(engine.dispose())


def test_validated_proposal_and_recorded_response_index_survive_new_sessions(
    postgres_database_url: str, clean_tasks: None
) -> None:
    async def scenario() -> None:
        engine, sessions = await _database(postgres_database_url)
        try:
            claim = await _claim(sessions, key="agent-persist", worker="worker-a")
            repository = AgentRunRepository(sessions)
            created = await repository.reserve_or_rebind(fence=claim.execution_fence, spec=_spec())
            await repository.append_step(
                fence=claim.execution_fence,
                run_id=created.run.id,
                draft=AgentStepDraft(
                    kind=AgentStepKind.MODEL_CALL_STARTED,
                    logical_step_number=1,
                    provider_call_number=1,
                    request_fingerprint="c" * 64,
                ),
            )
            assert await repository.completed_response_count(created.run.id) == 0
            await repository.append_step(
                fence=claim.execution_fence,
                run_id=created.run.id,
                draft=AgentStepDraft(
                    kind=AgentStepKind.MODEL_RESPONSE_RECORDED,
                    logical_step_number=1,
                    provider_call_number=1,
                    response_fingerprint="d" * 64,
                    safe_action_kind="finish",
                    action_summary=FinishActionSummary(
                        kind="finish",
                        proposal_size_bytes=500,
                    ),
                    increments_model_call=True,
                    completes_logical_step=True,
                ),
            )
            proposal = ValidatedCodeChange(
                proposal_version="1",
                base_commit_sha="e" * 40,
                jira_issue_key="ENG-123",
                jira_source_version="jira-v1",
                summary="Bounded validated proposal.",
                assumptions=(),
                file_changes=(),
                test_intents=(),
                risk_flags=(),
                citations=(),
                model_confidence_band=ModelConfidenceBand.MEDIUM,
                proposal_fingerprint="f" * 64,
                proposal_size_bytes=500,
            )
            await repository.complete(
                fence=claim.execution_fence,
                run_id=created.run.id,
                outcome=ProposalOutcome.PROPOSED,
                proposal=proposal,
            )
            restarted = AgentRunRepository(create_session_factory(engine))
            assert await restarted.completed_response_count(created.run.id) == 1
            async with create_session_factory(engine)() as session:
                run = await session.get(AgentRun, created.run.id)
                assert run is not None
                assert run.proposal is not None
                assert run.proposal["summary"] == "Bounded validated proposal."
        finally:
            await engine.dispose()

    asyncio.run(scenario())
