from __future__ import annotations

import os

os.environ.setdefault(
    "NINJATECH_DATABASE_URL",
    "postgresql+asyncpg://ninjatech:test-only@127.0.0.1:5432/ninjatech_test",
)
os.environ.setdefault("NINJATECH_ENVIRONMENT", "test")
