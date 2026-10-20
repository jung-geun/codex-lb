"""Leader-owned subscription refresh and explicitly opted-in Claude warmups."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from app.core.scheduling.leader_election_handle import get_leader_election
from app.modules.model_sources.subscription_runtime import refresh_subscription_sources
from app.modules.model_sources.subscription_warmup import warm_subscription_sources

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SubscriptionRefreshScheduler:
    interval_seconds: float = 60.0
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="subscription-quota-refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _refresh_as_leader(self) -> bool:
        await refresh_subscription_sources()
        await warm_subscription_sources()
        return True

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await get_leader_election().run_if_leader(self._refresh_as_leader)
            except Exception as exc:
                # Provider adapters retain their last observation and safe error
                # status. Never render an exception that may contain credentials.
                logger.warning("Subscription metadata maintenance failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


def build_subscription_refresh_scheduler() -> SubscriptionRefreshScheduler:
    return SubscriptionRefreshScheduler()
