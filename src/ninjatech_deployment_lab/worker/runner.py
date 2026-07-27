from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections.abc import Collection
from time import perf_counter
from typing import Any

from ninjatech_deployment_lab.config import Settings
from ninjatech_deployment_lab.tasks.schemas import JsonValue
from ninjatech_deployment_lab.worker.domain import (
    ClaimedTask,
    ExecutionStopCause,
    FenceResult,
    HandlerContractError,
    HeartbeatResult,
    OwnershipLostError,
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    TaskCancelled,
    validate_handler_result,
)
from ninjatech_deployment_lab.worker.handlers import (
    HandlerContext,
    HandlerRegistry,
    TaskExecution,
)
from ninjatech_deployment_lab.worker.repository import WorkerRepository

logger = logging.getLogger(__name__)


class WorkerRunner:
    """Single-task worker loop with fenced persistence and cause-aware cancellation."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: WorkerRepository,
        registry: HandlerRegistry,
        worker_id: str,
        retry_policy: RetryPolicy | None = None,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.registry = registry
        self.worker_id = worker_id
        self.worker_id_hash = hashlib.sha256(worker_id.encode()).hexdigest()[:12]
        self.retry_policy = retry_policy or RetryPolicy(
            base_seconds=settings.worker_retry_base_seconds,
            cap_seconds=settings.worker_retry_cap_seconds,
            random_source=random.random,
        )
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._stop_claiming = False

    def request_stop(self) -> None:
        """Stop claiming and begin graceful active-handler shutdown."""
        self.shutdown_event.set()

    async def run(self) -> None:
        """Reconcile, claim, and execute until shutdown or ownership uncertainty."""
        logger.info(
            "worker_started",
            extra={
                "event": "worker_started",
                "worker_id_hash": self.worker_id_hash,
            },
        )
        try:
            while not self.shutdown_event.is_set() and not self._stop_claiming:
                try:
                    recovery = await self.repository.recover_one_expired(
                        backoff_seconds=self.retry_policy.delay_seconds
                    )
                    if recovery is not None:
                        logger.warning(
                            "task_lease_expired",
                            extra={
                                "event": "task_lease_expired",
                                "task_id": str(recovery.task_id),
                                "attempt_id": str(recovery.attempt_id),
                                "attempt_number": recovery.attempt_number,
                                "new_status": recovery.new_status,
                                "next_attempt_at": recovery.next_attempt_at,
                                "terminal_reason": recovery.terminal_reason,
                                "worker_id_hash": self.worker_id_hash,
                            },
                        )

                    claim = await self.repository.claim_one(
                        supported_task_types=self.registry.supported_task_types,
                        worker_id=self.worker_id,
                        lease_duration_seconds=self.settings.worker_lease_duration_seconds,
                    )
                except Exception:
                    logger.error(
                        "worker_database_unavailable",
                        extra={
                            "event": "worker_database_unavailable",
                            "worker_id_hash": self.worker_id_hash,
                        },
                    )
                    await self._poll_wait()
                    continue

                if claim is None:
                    await self._poll_wait()
                    continue

                logger.info(
                    "task_claimed",
                    extra=self._log_fields(claim, event="task_claimed"),
                )
                await self._execute_claim(claim)
        finally:
            logger.info(
                "worker_stopping",
                extra={
                    "event": "worker_stopping",
                    "worker_id_hash": self.worker_id_hash,
                },
            )

    async def _poll_wait(self) -> None:
        try:
            await asyncio.wait_for(
                self.shutdown_event.wait(),
                timeout=self.settings.worker_poll_interval_seconds,
            )
        except TimeoutError:
            return

    async def _execute_claim(self, claim: ClaimedTask) -> None:
        started_at = perf_counter()
        customer_cancellation = asyncio.Event()
        ownership_lost = asyncio.Event()
        handler = self.registry.get(claim.task_type)
        task = TaskExecution(
            task_id=claim.task_id,
            task_type=claim.task_type,
            task_input=claim.task_input,
            attempt_id=claim.attempt_id,
            attempt_number=claim.attempt_number,
            max_attempts=claim.max_attempts,
            execution_fence=claim.execution_fence,
        )
        context = HandlerContext(
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            attempt_number=claim.attempt_number,
            worker_id=claim.worker_id,
            customer_cancellation=customer_cancellation,
            ownership_lost=ownership_lost,
        )
        handler_task = asyncio.create_task(handler.execute(task, context))
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                claim,
                customer_cancellation=customer_cancellation,
                ownership_lost=ownership_lost,
            )
        )
        timeout_task = asyncio.create_task(
            asyncio.sleep(self.settings.worker_handler_timeout_seconds)
        )
        shutdown_task = asyncio.create_task(self.shutdown_event.wait())

        cause: ExecutionStopCause | None = None
        try:
            done, _ = await asyncio.wait(
                {handler_task, heartbeat_task, timeout_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            cause = self._completed_cause(
                done,
                heartbeat_task=heartbeat_task,
                timeout_task=timeout_task,
            )

            if shutdown_task in done and cause is None and handler_task not in done:
                done, _ = await asyncio.wait(
                    {handler_task, heartbeat_task, timeout_task},
                    timeout=self.settings.worker_shutdown_grace_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                cause = self._completed_cause(
                    done,
                    heartbeat_task=heartbeat_task,
                    timeout_task=timeout_task,
                )
                if not done:
                    cause = ExecutionStopCause.WORKER_SHUTDOWN

            if cause is not None:
                if cause is ExecutionStopCause.CUSTOMER_CANCELLATION:
                    customer_cancellation.set()
                if cause is ExecutionStopCause.OWNERSHIP_LOSS:
                    ownership_lost.set()
                    self._stop_claiming = True
                handler_task.cancel()
                if cause is ExecutionStopCause.WORKER_SHUTDOWN:
                    # The configured grace period was already consumed above.
                    # Yield once to deliver cancellation, then stop heartbeats so
                    # the still-running lease can expire and be recovered.
                    await asyncio.sleep(0)
                    handler_stopped = handler_task.done()
                else:
                    handler_stopped = await self._wait_for_cancelled_handler(handler_task)
                if not handler_stopped:
                    # A handler that suppresses cancellation is still executing.
                    # Preserve one-task-per-process semantics by retiring this
                    # worker loop after persisting only the cause-appropriate
                    # outcome below.
                    self._stop_claiming = True

            if cause is ExecutionStopCause.CUSTOMER_CANCELLATION:
                await self._finalize_customer_cancellation(claim, started_at)
                return
            if cause is ExecutionStopCause.TIMEOUT:
                logger.warning(
                    "handler_timeout",
                    extra={
                        **self._log_fields(claim, event="handler_timeout"),
                        "duration_ms": self._duration_ms(started_at),
                        "error_code": "execution_timeout",
                    },
                )
                await self._finalize_failure_policy(
                    claim,
                    error_code="execution_timeout",
                    error_summary="Task execution exceeded its time limit",
                    terminal_reason="execution_timeout",
                    retryable=True,
                    started_at=started_at,
                )
                return
            if cause in {
                ExecutionStopCause.WORKER_SHUTDOWN,
                ExecutionStopCause.OWNERSHIP_LOSS,
            }:
                return

            await self._handle_handler_completion(claim, handler_task, started_at)
        finally:
            for background_task in (heartbeat_task, timeout_task, shutdown_task):
                background_task.cancel()
            await asyncio.gather(
                heartbeat_task,
                timeout_task,
                shutdown_task,
                return_exceptions=True,
            )

    @staticmethod
    def _completed_cause(
        done: Collection[asyncio.Task[Any]],
        *,
        heartbeat_task: asyncio.Task[ExecutionStopCause],
        timeout_task: asyncio.Task[None],
    ) -> ExecutionStopCause | None:
        if heartbeat_task in done:
            return heartbeat_task.result()
        if timeout_task in done:
            return ExecutionStopCause.TIMEOUT
        return None

    async def _wait_for_cancelled_handler(
        self,
        handler_task: asyncio.Task[dict[str, JsonValue]],
    ) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.shield(handler_task),
                timeout=min(
                    self.settings.worker_shutdown_grace_seconds,
                    self.settings.worker_lease_duration_seconds,
                ),
            )
            return True
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            return True
        except Exception:
            # The original stop cause remains authoritative. A handler return or
            # exception after cancellation can never be converted into success.
            return True

    async def _heartbeat_loop(
        self,
        claim: ClaimedTask,
        *,
        customer_cancellation: asyncio.Event,
        ownership_lost: asyncio.Event,
    ) -> ExecutionStopCause:
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_interval_seconds)
            try:
                result = await self.repository.heartbeat(
                    claim,
                    lease_duration_seconds=self.settings.worker_lease_duration_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                ownership_lost.set()
                logger.warning(
                    "worker_ownership_lost",
                    extra=self._log_fields(
                        claim,
                        event="worker_ownership_lost",
                    ),
                )
                return ExecutionStopCause.OWNERSHIP_LOSS

            if result is HeartbeatResult.CANCELLATION_REQUESTED:
                customer_cancellation.set()
                return ExecutionStopCause.CUSTOMER_CANCELLATION
            logger.debug(
                "task_heartbeat",
                extra=self._log_fields(claim, event="task_heartbeat"),
            )

    async def _handle_handler_completion(
        self,
        claim: ClaimedTask,
        handler_task: asyncio.Task[dict[str, JsonValue]],
        started_at: float,
    ) -> None:
        try:
            raw_result = handler_task.result()
            result = validate_handler_result(
                raw_result,
                maximum_bytes=self.settings.worker_max_result_bytes,
            )
        except TaskCancelled:
            await self._finalize_customer_cancellation(claim, started_at)
            return
        except OwnershipLostError:
            self._stop_claiming = True
            logger.warning(
                "worker_ownership_lost",
                extra=self._log_fields(claim, event="worker_ownership_lost"),
            )
            return
        except RetryableTaskError as error:
            await self._finalize_failure_policy(
                claim,
                error_code=error.error_code,
                error_summary="Task execution failed and will be retried",
                terminal_reason="retryable_error",
                retryable=True,
                started_at=started_at,
                retry_after_seconds=error.retry_after_seconds,
            )
            return
        except HandlerContractError as error:
            await self._finalize_failure_policy(
                claim,
                error_code=error.error_code,
                error_summary="Handler returned an invalid result",
                terminal_reason="handler_contract_error",
                retryable=False,
                started_at=started_at,
            )
            return
        except PermanentTaskError as error:
            await self._finalize_failure_policy(
                claim,
                error_code=error.error_code,
                error_summary="Task execution failed permanently",
                terminal_reason="permanent_error",
                retryable=False,
                started_at=started_at,
            )
            return
        except asyncio.CancelledError:
            self._stop_claiming = True
            return
        except Exception as error:
            logger.error(
                "handler_unexpected_exception",
                extra={
                    **self._log_fields(
                        claim,
                        event="handler_unexpected_exception",
                    ),
                    "exception_type": type(error).__name__,
                },
            )
            await self._finalize_failure_policy(
                claim,
                error_code="unexpected_error",
                error_summary="Task execution failed unexpectedly",
                terminal_reason="unexpected_error",
                retryable=True,
                started_at=started_at,
            )
            return

        fence_result = await self.repository.finalize_success(claim, result)
        if await self._handle_failed_fence(claim, fence_result, started_at):
            return
        logger.info(
            "task_succeeded",
            extra={
                **self._log_fields(claim, event="task_succeeded"),
                "duration_ms": self._duration_ms(started_at),
                "new_status": "succeeded",
            },
        )

    async def _finalize_customer_cancellation(
        self,
        claim: ClaimedTask,
        started_at: float,
    ) -> None:
        fence_result = await self.repository.finalize_cancellation(claim)
        if fence_result is not FenceResult.APPLIED:
            await self._record_stale_execution(claim)
            return
        logger.info(
            "task_cancelled",
            extra={
                **self._log_fields(claim, event="task_cancelled"),
                "duration_ms": self._duration_ms(started_at),
                "new_status": "cancelled",
            },
        )

    async def _finalize_failure_policy(
        self,
        claim: ClaimedTask,
        *,
        error_code: str,
        error_summary: str,
        terminal_reason: str,
        retryable: bool,
        started_at: float,
        retry_after_seconds: float | None = None,
    ) -> None:
        if retryable and claim.attempt_number < claim.max_attempts:
            delay_seconds = (
                retry_after_seconds
                if retry_after_seconds is not None
                else self.retry_policy.delay_seconds(claim.attempt_number)
            )
            fence_result = await self.repository.schedule_retry(
                claim,
                delay_seconds=delay_seconds,
                error_code=error_code,
                error_summary=error_summary,
                terminal_reason=terminal_reason,
            )
            if await self._handle_failed_fence(claim, fence_result, started_at):
                return
            logger.info(
                "task_retry_scheduled",
                extra={
                    **self._log_fields(claim, event="task_retry_scheduled"),
                    "duration_ms": self._duration_ms(started_at),
                    "error_code": error_code,
                    "new_status": "approved",
                    "next_attempt_delay_seconds": delay_seconds,
                },
            )
            return

        final_reason = (
            "max_attempts_exhausted"
            if retryable and claim.attempt_number >= claim.max_attempts
            else terminal_reason
        )
        fence_result = await self.repository.finalize_failure(
            claim,
            error_code=error_code,
            error_summary=error_summary,
            terminal_reason=final_reason,
        )
        if await self._handle_failed_fence(claim, fence_result, started_at):
            return
        logger.warning(
            "task_failed",
            extra={
                **self._log_fields(claim, event="task_failed"),
                "duration_ms": self._duration_ms(started_at),
                "error_code": error_code,
                "new_status": "failed",
                "terminal_reason": final_reason,
            },
        )

    async def _handle_failed_fence(
        self,
        claim: ClaimedTask,
        fence_result: FenceResult,
        started_at: float,
    ) -> bool:
        if fence_result is FenceResult.APPLIED:
            return False
        if fence_result is FenceResult.CANCELLATION_REQUESTED:
            await self._finalize_customer_cancellation(claim, started_at)
            return True
        await self._record_stale_execution(claim)
        return True

    async def _record_stale_execution(self, claim: ClaimedTask) -> None:
        self._stop_claiming = True
        logger.warning(
            "stale_worker_update_rejected",
            extra=self._log_fields(
                claim,
                event="stale_worker_update_rejected",
            ),
        )

    def _log_fields(self, claim: ClaimedTask, *, event: str) -> dict[str, object]:
        return {
            "event": event,
            "task_id": str(claim.task_id),
            "task_type": claim.task_type,
            "attempt_id": str(claim.attempt_id),
            "attempt_number": claim.attempt_number,
            "worker_id_hash": self.worker_id_hash,
        }

    @staticmethod
    def _duration_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)
