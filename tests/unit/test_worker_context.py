from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from ninjatech_deployment_lab.worker.domain import OwnershipLostError, TaskCancelled
from ninjatech_deployment_lab.worker.handlers import HandlerContext


def _context(
    customer: asyncio.Event,
    ownership: asyncio.Event,
) -> HandlerContext:
    return HandlerContext(
        task_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        worker_id="worker",
        customer_cancellation=customer,
        ownership_lost=ownership,
    )


def test_customer_cancellation_has_distinct_signal() -> None:
    customer = asyncio.Event()
    customer.set()

    with pytest.raises(TaskCancelled):
        _context(customer, asyncio.Event()).raise_if_cancelled()


def test_ownership_loss_has_distinct_signal_and_priority() -> None:
    customer = asyncio.Event()
    ownership = asyncio.Event()
    customer.set()
    ownership.set()

    with pytest.raises(OwnershipLostError):
        _context(customer, ownership).raise_if_cancelled()
