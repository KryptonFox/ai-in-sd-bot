import asyncio
import logging
import uuid

from app.core.clock import Clock
from app.services.notifications.contracts import (
    BotDelivery,
    BriefingProvider,
    CityResolver,
    CitySelection,
    NotificationPolicies,
    NotificationRepository,
    NotificationSettingsView,
    TickReport,
    TraceSink,
)
from app.services.notifications.delivery import NotificationDelivery
from app.services.notifications.preferences import NotificationPreferences

logger = logging.getLogger("app.services.notifications")


class NotificationService:
    def __init__(
        self,
        repository: NotificationRepository,
        *,
        clock: Clock,
        deliver: BotDelivery,
        briefing: BriefingProvider,
        city_resolver: CityResolver,
        policies: NotificationPolicies | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._policies = policies or NotificationPolicies()
        self._preferences = NotificationPreferences(
            repository,
            clock=clock,
            city_resolver=city_resolver,
            policies=self._policies,
        )
        self._delivery = NotificationDelivery(
            repository,
            clock=clock,
            deliver=deliver,
            briefing=briefing,
            policies=self._policies,
            trace_sink=trace_sink,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "notification_tick_failed exception=%s",
                    type(error).__name__,
                )

            await asyncio.sleep(self._policies.tick_interval_seconds)

    async def tick(self) -> TickReport:
        return await self._delivery.tick()

    async def remember_private_chat(
        self,
        user_id: int,
        chat_id: int,
        chat_type: str,
    ) -> bool:
        return await self._preferences.remember_private_chat(
            user_id,
            chat_id,
            chat_type,
        )

    async def notification_settings(self, user_id: int) -> NotificationSettingsView:
        return await self._preferences.notification_settings(user_id)

    async def prepare_city_selection(self, user_id: int, query: str) -> CitySelection:
        return await self._preferences.prepare_city_selection(user_id, query)

    async def confirm_city_selection(
        self,
        user_id: int,
        selection_id: uuid.UUID,
        index: int,
    ) -> NotificationSettingsView:
        return await self._preferences.confirm_city_selection(
            user_id,
            selection_id,
            index,
        )

    async def cancel_city_selection(self, user_id: int, selection_id: uuid.UUID) -> None:
        await self._preferences.cancel_city_selection(user_id, selection_id)

    async def enable_briefing(self, user_id: int) -> NotificationSettingsView:
        return await self._preferences.enable_briefing(user_id)

    async def disable_briefing(self, user_id: int) -> NotificationSettingsView:
        return await self._preferences.disable_briefing(user_id)
