from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ninjatech_deployment_lab.code_proposals.domain import (
    AgentActionSummary,
    AgentToolResultSummary,
    EvidenceRole,
    ProposalOutcome,
    ValidatedCodeChange,
)
from ninjatech_deployment_lab.code_proposals.model import (
    AgentRun,
    AgentRunOutcome,
    AgentRunStatus,
    AgentStep,
    AgentStepKind,
    AgentStepSource,
)
from ninjatech_deployment_lab.code_proposals.scanner import ModelEgressScanner
from ninjatech_deployment_lab.integrations.domain import canonical_json
from ninjatech_deployment_lab.integrations.model import SourceArtifact
from ninjatech_deployment_lab.tasks.domain import AttemptStatus, TaskStatus
from ninjatech_deployment_lab.tasks.model import Task, TaskAttempt
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import ExecutionFence, ExecutionInvariantError


class RunReservationKind(StrEnum):
    CREATED = "created"
    COMPLETED_REPLAY = "completed_replay"
    ACTIVE_BUSY = "active_busy"
    RETRY_WAIT = "retry_wait"
    RESUME = "resume"
    REBIND_INTERRUPTED = "rebind_interrupted"
    REBIND_ABANDONED = "rebind_abandoned"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class AgentRunSpec:
    run_scope_key: str
    workflow_version: str
    source_snapshot_hash: str
    provider: str
    model_name: str
    prompt_template_version: str
    prompt_contract_hash: str
    maximum_steps: int
    data_classification: str
    retention_until: datetime


@dataclass(frozen=True, slots=True)
class AgentStepDraft:
    kind: AgentStepKind
    logical_step_number: int
    provider_call_number: int | None = None
    tool_name: str | None = None
    request_fingerprint: str | None = None
    response_fingerprint: str | None = None
    safe_action_kind: str | None = None
    action_summary: AgentActionSummary | None = None
    tool_result_summary: AgentToolResultSummary | None = None
    input_token_count: int = 0
    output_token_count: int = 0
    duration_milliseconds: int = 0
    safe_error_code: str | None = None
    completes_logical_step: bool = False
    increments_model_call: bool = False
    increments_tool_call: bool = False


@dataclass(frozen=True, slots=True)
class StepSourceLink:
    source_artifact_id: UUID
    role: EvidenceRole


@dataclass(frozen=True, slots=True)
class RunReservation:
    kind: RunReservationKind
    run: AgentRun
    database_now: datetime


class AgentRunRepository:
    """Fenced, transactional persistence for semantic model-run evidence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve_or_rebind(
        self,
        *,
        fence: ExecutionFence,
        spec: AgentRunSpec,
    ) -> RunReservation:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                inserted = cast(
                    CursorResult[Any],
                    await session.execute(
                        pg_insert(AgentRun)
                        .values(
                            originating_task_id=fence.task_id,
                            bound_task_id=fence.task_id,
                            bound_task_attempt_id=fence.attempt_id,
                            run_scope_key=spec.run_scope_key,
                            workflow_version=spec.workflow_version,
                            source_snapshot_hash=spec.source_snapshot_hash,
                            provider=spec.provider,
                            model_name=spec.model_name,
                            prompt_template_version=spec.prompt_template_version,
                            prompt_contract_hash=spec.prompt_contract_hash,
                            status=AgentRunStatus.RESERVED.value,
                            maximum_steps=spec.maximum_steps,
                            completed_steps=0,
                            model_call_count=0,
                            tool_call_count=0,
                            input_token_count=0,
                            output_token_count=0,
                            data_classification=spec.data_classification,
                            retention_until=spec.retention_until,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=[AgentRun.run_scope_key])
                        .returning(AgentRun.id)
                    ),
                ).scalar_one_or_none()
                run = await _lock_run_by_scope(session, spec.run_scope_key)
                if inserted is not None:
                    await _append_step(
                        session,
                        run=run,
                        fence=fence,
                        draft=AgentStepDraft(
                            kind=AgentStepKind.RUN_RESERVED,
                            logical_step_number=0,
                        ),
                        sources=(),
                        now=now,
                    )
                    return RunReservation(RunReservationKind.CREATED, run, now)

                _require_compatible_scope(run, spec)
                status = AgentRunStatus(run.status)
                if status is AgentRunStatus.COMPLETED:
                    rebound = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(AgentRun)
                            .where(
                                AgentRun.id == run.id,
                                AgentRun.status == AgentRunStatus.COMPLETED.value,
                            )
                            .values(
                                bound_task_id=fence.task_id,
                                bound_task_attempt_id=fence.attempt_id,
                                updated_at=now,
                            )
                        ),
                    )
                    _require_one_row(rebound, "completed agent run replay binding")
                    run = await _lock_run(session, run.id)
                    await _append_step(
                        session,
                        run=run,
                        fence=fence,
                        draft=AgentStepDraft(
                            kind=AgentStepKind.COMPLETED_RUN_REPLAYED,
                            logical_step_number=run.completed_steps,
                        ),
                        sources=(),
                        now=now,
                    )
                    return RunReservation(RunReservationKind.COMPLETED_REPLAY, run, now)
                if status is AgentRunStatus.FAILED:
                    return RunReservation(RunReservationKind.TERMINAL_FAILURE, run, now)
                if status is AgentRunStatus.RETRYABLE and run.resume_not_before is not None:
                    if now < run.resume_not_before:
                        return RunReservation(RunReservationKind.RETRY_WAIT, run, now)

                same_fence = (
                    run.bound_task_id == fence.task_id
                    and run.bound_task_attempt_id == fence.attempt_id
                )
                bound_active = await _bound_execution_is_active(session, run, now)
                if status in {AgentRunStatus.RESERVED, AgentRunStatus.RUNNING}:
                    if bound_active and not same_fence:
                        return RunReservation(RunReservationKind.ACTIVE_BUSY, run, now)
                    kind = (
                        RunReservationKind.RESUME
                        if bound_active and same_fence
                        else RunReservationKind.REBIND_ABANDONED
                    )
                elif status is AgentRunStatus.INTERRUPTED:
                    kind = RunReservationKind.REBIND_INTERRUPTED
                else:
                    kind = RunReservationKind.RESUME

                needs_binding_update = (
                    status in {AgentRunStatus.RETRYABLE, AgentRunStatus.INTERRUPTED}
                    or kind is not RunReservationKind.RESUME
                    or not same_fence
                )
                if needs_binding_update:
                    previous = run.status
                    result = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(AgentRun)
                            .where(AgentRun.id == run.id, AgentRun.status == previous)
                            .values(
                                bound_task_id=fence.task_id,
                                bound_task_attempt_id=fence.attempt_id,
                                status=AgentRunStatus.RESERVED.value,
                                safe_error_code=None,
                                resume_not_before=None,
                                updated_at=now,
                            )
                        ),
                    )
                    _require_one_row(result, "agent run rebind")
                    run = await _lock_run(session, run.id)
                    await _append_step(
                        session,
                        run=run,
                        fence=fence,
                        draft=AgentStepDraft(
                            kind=AgentStepKind.RUN_REBOUND,
                            logical_step_number=run.completed_steps,
                        ),
                        sources=(),
                        now=now,
                    )
                return RunReservation(kind, run, now)

    async def mark_running(self, *, fence: ExecutionFence, run_id: UUID) -> AgentRun:
        return await self._transition(
            fence=fence,
            run_id=run_id,
            expected={AgentRunStatus.RESERVED, AgentRunStatus.RUNNING},
            status=AgentRunStatus.RUNNING,
            draft=None,
        )

    async def append_step(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        draft: AgentStepDraft,
        sources: tuple[StepSourceLink, ...] = (),
    ) -> AgentStep:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                run = await _lock_run(session, run_id)
                _require_bound_run(run, fence)
                if AgentRunStatus(run.status) not in {
                    AgentRunStatus.RESERVED,
                    AgentRunStatus.RUNNING,
                }:
                    raise ExecutionInvariantError("agent run is not writable")
                return await _append_step(
                    session,
                    run=run,
                    fence=fence,
                    draft=draft,
                    sources=sources,
                    now=now,
                )

    async def mark_retryable(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        safe_error_code: str,
        delay_seconds: float,
    ) -> AgentRun:
        if delay_seconds < 0:
            raise ValueError("retry delay must not be negative")
        return await self._transition(
            fence=fence,
            run_id=run_id,
            expected={AgentRunStatus.RESERVED, AgentRunStatus.RUNNING},
            status=AgentRunStatus.RETRYABLE,
            draft=AgentStepDraft(
                kind=AgentStepKind.RUN_RETRYABLE,
                logical_step_number=0,
                safe_error_code=safe_error_code,
            ),
            safe_error_code=safe_error_code,
            resume_delay_seconds=delay_seconds,
        )

    async def interrupt(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        safe_error_code: str,
    ) -> AgentRun:
        return await self._transition(
            fence=fence,
            run_id=run_id,
            expected={AgentRunStatus.RESERVED, AgentRunStatus.RUNNING},
            status=AgentRunStatus.INTERRUPTED,
            draft=AgentStepDraft(
                kind=AgentStepKind.RUN_INTERRUPTED,
                logical_step_number=0,
                safe_error_code=safe_error_code,
            ),
            safe_error_code=safe_error_code,
        )

    async def fail(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        safe_error_code: str,
    ) -> AgentRun:
        return await self._transition(
            fence=fence,
            run_id=run_id,
            expected={AgentRunStatus.RESERVED, AgentRunStatus.RUNNING},
            status=AgentRunStatus.FAILED,
            draft=AgentStepDraft(
                kind=AgentStepKind.RUN_FAILED,
                logical_step_number=0,
                safe_error_code=safe_error_code,
            ),
            safe_error_code=safe_error_code,
        )

    async def complete(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        outcome: ProposalOutcome,
        proposal: ValidatedCodeChange | None,
        sources: tuple[StepSourceLink, ...] = (),
    ) -> AgentRun:
        if outcome is ProposalOutcome.PROPOSED and proposal is None:
            raise ValueError("proposed completion requires a validated proposal")
        if outcome is not ProposalOutcome.PROPOSED and proposal is not None:
            raise ValueError("non-proposed completion must not persist a proposal")
        proposal_json = (
            cast(dict[str, JsonValue], proposal.model_dump(mode="json")) if proposal else None
        )
        return await self._transition(
            fence=fence,
            run_id=run_id,
            expected={AgentRunStatus.RESERVED, AgentRunStatus.RUNNING},
            status=AgentRunStatus.COMPLETED,
            draft=AgentStepDraft(
                kind=AgentStepKind.RUN_COMPLETED,
                logical_step_number=0,
            ),
            completed_outcome=AgentRunOutcome(outcome.value),
            proposal=proposal_json,
            proposal_fingerprint=proposal.proposal_fingerprint if proposal else None,
            proposal_size_bytes=proposal.proposal_size_bytes if proposal else None,
            sources=sources,
        )

    async def completed_response_count(self, run_id: UUID) -> int:
        async with self._session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(AgentStep.id)).where(
                            AgentStep.agent_run_id == run_id,
                            AgentStep.step_kind == AgentStepKind.MODEL_RESPONSE_RECORDED.value,
                        )
                    )
                ).scalar_one()
            )

    async def _transition(
        self,
        *,
        fence: ExecutionFence,
        run_id: UUID,
        expected: set[AgentRunStatus],
        status: AgentRunStatus,
        draft: AgentStepDraft | None,
        safe_error_code: str | None = None,
        resume_delay_seconds: float | None = None,
        completed_outcome: AgentRunOutcome | None = None,
        proposal: dict[str, JsonValue] | None = None,
        proposal_fingerprint: str | None = None,
        proposal_size_bytes: int | None = None,
        sources: tuple[StepSourceLink, ...] = (),
    ) -> AgentRun:
        async with self._session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                await _assert_active_fence(session, fence, now)
                run = await _lock_run(session, run_id)
                _require_bound_run(run, fence)
                previous = AgentRunStatus(run.status)
                if previous not in expected:
                    raise ExecutionInvariantError("agent run state changed unexpectedly")
                values: dict[str, Any] = {
                    "status": status.value,
                    "safe_error_code": safe_error_code,
                    "resume_not_before": (
                        now + timedelta(seconds=resume_delay_seconds)
                        if resume_delay_seconds is not None
                        else None
                    ),
                    "updated_at": now,
                    "completed_outcome": (
                        completed_outcome.value if completed_outcome is not None else None
                    ),
                    "proposal": proposal,
                    "proposal_fingerprint": proposal_fingerprint,
                    "proposal_size_bytes": proposal_size_bytes,
                    "completed_at": now if status is AgentRunStatus.COMPLETED else None,
                }
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(AgentRun)
                        .where(AgentRun.id == run.id, AgentRun.status == previous.value)
                        .values(**values)
                    ),
                )
                _require_one_row(result, "agent run transition")
                run = await _lock_run(session, run.id)
                if draft is not None:
                    await _append_step(
                        session,
                        run=run,
                        fence=fence,
                        draft=replace(draft, logical_step_number=run.completed_steps),
                        sources=sources,
                        now=now,
                    )
                return await _lock_run(session, run.id)


async def _append_step(
    session: AsyncSession,
    *,
    run: AgentRun,
    fence: ExecutionFence,
    draft: AgentStepDraft,
    sources: tuple[StepSourceLink, ...],
    now: datetime,
) -> AgentStep:
    _require_bound_run(run, fence)
    _validate_step_draft(draft)
    sequence = (
        int(
            (
                await session.execute(
                    select(func.coalesce(func.max(AgentStep.sequence_number), 0)).where(
                        AgentStep.agent_run_id == run.id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    if draft.completes_logical_step and run.completed_steps >= run.maximum_steps:
        raise ExecutionInvariantError("agent run step budget is exhausted")
    step = AgentStep(
        agent_run_id=run.id,
        task_id=fence.task_id,
        task_attempt_id=fence.attempt_id,
        sequence_number=sequence,
        logical_step_number=draft.logical_step_number,
        provider_call_number=draft.provider_call_number,
        step_kind=draft.kind.value,
        tool_name=draft.tool_name,
        request_fingerprint=draft.request_fingerprint,
        response_fingerprint=draft.response_fingerprint,
        safe_action_kind=draft.safe_action_kind,
        action_summary=(
            cast(dict[str, JsonValue], draft.action_summary.model_dump(mode="json"))
            if draft.action_summary is not None
            else None
        ),
        tool_result_summary=(
            cast(dict[str, JsonValue], draft.tool_result_summary.model_dump(mode="json"))
            if draft.tool_result_summary is not None
            else None
        ),
        input_token_count=draft.input_token_count,
        output_token_count=draft.output_token_count,
        duration_milliseconds=draft.duration_milliseconds,
        safe_error_code=draft.safe_error_code,
        created_at=now,
    )
    session.add(step)
    await session.flush()
    await _append_source_links(
        session,
        step.id,
        sources,
        allowed_task_ids={run.originating_task_id, run.bound_task_id},
    )
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.completed_steps == run.completed_steps,
                AgentRun.model_call_count == run.model_call_count,
                AgentRun.tool_call_count == run.tool_call_count,
            )
            .values(
                completed_steps=run.completed_steps + int(draft.completes_logical_step),
                model_call_count=run.model_call_count + int(draft.increments_model_call),
                tool_call_count=run.tool_call_count + int(draft.increments_tool_call),
                input_token_count=run.input_token_count + draft.input_token_count,
                output_token_count=run.output_token_count + draft.output_token_count,
                updated_at=now,
            )
        ),
    )
    _require_one_row(result, "agent run counters")
    await session.flush()
    return step


async def _append_source_links(
    session: AsyncSession,
    step_id: UUID,
    sources: tuple[StepSourceLink, ...],
    *,
    allowed_task_ids: set[UUID],
) -> None:
    if not sources:
        return
    if len({(source.source_artifact_id, source.role) for source in sources}) != len(sources):
        raise ExecutionInvariantError("duplicate agent step source link")
    source_ids = {source.source_artifact_id for source in sources}
    found = (
        await session.execute(
            select(SourceArtifact.id, SourceArtifact.task_id).where(
                SourceArtifact.id.in_(source_ids)
            )
        )
    ).all()
    if {item.id for item in found} != source_ids:
        raise ExecutionInvariantError("agent step source artifact was not found")
    if any(item.task_id not in allowed_task_ids for item in found):
        raise ExecutionInvariantError("agent step source belongs to an unrelated task")
    session.add_all(
        AgentStepSource(
            agent_step_id=step_id,
            source_artifact_id=source.source_artifact_id,
            ordinal=ordinal,
            evidence_role=source.role.value,
        )
        for ordinal, source in enumerate(sources, start=1)
    )
    await session.flush()


async def _database_now(session: AsyncSession) -> datetime:
    return cast(datetime, (await session.execute(select(func.clock_timestamp()))).scalar_one())


async def _lock_run(session: AsyncSession, run_id: UUID) -> AgentRun:
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise ExecutionInvariantError("agent run was not found")
    return run


async def _lock_run_by_scope(session: AsyncSession, run_scope_key: str) -> AgentRun:
    run = (
        await session.execute(
            select(AgentRun).where(AgentRun.run_scope_key == run_scope_key).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise ExecutionInvariantError("agent run reservation was not found")
    return run


def _require_compatible_scope(run: AgentRun, spec: AgentRunSpec) -> None:
    actual = (
        run.workflow_version,
        run.source_snapshot_hash,
        run.provider,
        run.model_name,
        run.prompt_template_version,
        run.prompt_contract_hash,
    )
    expected = (
        spec.workflow_version,
        spec.source_snapshot_hash,
        spec.provider,
        spec.model_name,
        spec.prompt_template_version,
        spec.prompt_contract_hash,
    )
    if actual != expected:
        raise ExecutionInvariantError("agent run scope key conflicts with semantic inputs")


def _require_bound_run(run: AgentRun, fence: ExecutionFence) -> None:
    if run.bound_task_id != fence.task_id or run.bound_task_attempt_id != fence.attempt_id:
        raise ExecutionInvariantError("agent run is bound to another execution")


async def _assert_active_fence(
    session: AsyncSession,
    fence: ExecutionFence,
    database_now: datetime,
) -> None:
    active = (
        await session.execute(
            select(Task.id)
            .join(
                TaskAttempt,
                and_(
                    TaskAttempt.id == fence.attempt_id,
                    TaskAttempt.task_id == Task.id,
                ),
            )
            .where(
                Task.id == fence.task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == fence.worker_id,
                Task.attempt_count == fence.attempt_number,
                Task.lease_token_hash == fence.lease_token_hash,
                Task.lease_expires_at > database_now,
                TaskAttempt.attempt_number == fence.attempt_number,
                TaskAttempt.lease_token_hash == fence.lease_token_hash,
                TaskAttempt.status == AttemptStatus.RUNNING,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active is None:
        raise ExecutionInvariantError("active task execution fence was not confirmed")


async def _bound_execution_is_active(
    session: AsyncSession,
    run: AgentRun,
    database_now: datetime,
) -> bool:
    active = (
        await session.execute(
            select(Task.id)
            .join(
                TaskAttempt,
                and_(
                    TaskAttempt.id == run.bound_task_attempt_id,
                    TaskAttempt.task_id == Task.id,
                ),
            )
            .where(
                Task.id == run.bound_task_id,
                Task.status == TaskStatus.RUNNING,
                Task.attempt_count == TaskAttempt.attempt_number,
                Task.worker_id == TaskAttempt.worker_id,
                Task.lease_token_hash == TaskAttempt.lease_token_hash,
                Task.lease_expires_at > database_now,
                TaskAttempt.status == AttemptStatus.RUNNING,
            )
        )
    ).scalar_one_or_none()
    return active is not None


def _require_one_row(result: CursorResult[Any], operation: str) -> None:
    if result.rowcount != 1:
        raise ExecutionInvariantError(f"{operation} did not update exactly one row")


def _validate_step_draft(draft: AgentStepDraft) -> None:
    if draft.logical_step_number < 0:
        raise ValueError("logical step number must not be negative")
    if draft.provider_call_number is not None and draft.provider_call_number < 1:
        raise ValueError("provider call number must be positive")
    if (
        min(
            draft.input_token_count,
            draft.output_token_count,
            draft.duration_milliseconds,
        )
        < 0
    ):
        raise ValueError("agent step counters must not be negative")
    for summary in (draft.action_summary, draft.tool_result_summary):
        if summary is None:
            continue
        serialized = canonical_json(summary.model_dump(mode="json"))
        if len(serialized) > 32768:
            raise ValueError("agent step summary exceeds byte limit")
        normalized = summary.model_dump(mode="json")
        _reject_unsafe_summary_keys(normalized)
        _scan_summary_strings(normalized)


def _reject_unsafe_summary_keys(value: object) -> None:
    forbidden = {
        "authorization",
        "credential",
        "diff",
        "message",
        "model_output",
        "prompt",
        "raw_response",
        "secret",
        "source_content",
        "token",
        "unified_diff",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in forbidden:
                raise ValueError("agent step summary contains forbidden raw data")
            _reject_unsafe_summary_keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_unsafe_summary_keys(item)


def _scan_summary_strings(value: object) -> None:
    if isinstance(value, str):
        ModelEgressScanner().require_safe(value)
    elif isinstance(value, dict):
        for item in value.values():
            _scan_summary_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _scan_summary_strings(item)
