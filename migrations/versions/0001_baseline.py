"""Establish the migration baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-26
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create no domain tables in the foundation milestone."""


def downgrade() -> None:
    """Remove no domain tables from the foundation milestone."""
