"""Shared pytest fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .fake_ziggurat import FakeZiggurat

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def ziggurat() -> AsyncIterator[FakeZiggurat]:
    """Run a fake ziggurat-server; fails the test if the client broke a safety rule."""
    fake = FakeZiggurat()
    await fake.start()
    yield fake
    await fake.close()
    assert fake.violations == []
