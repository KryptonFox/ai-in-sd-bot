import math
import uuid
from collections.abc import Sequence

from app.core.clock import Clock
from app.core.schemas.tools import ToolFailure
from app.db.notification_records import (
    CityRecord,
    CitySelectionRecord,
    CitySelectionState,
)
from app.services.notifications.contracts import (
    CityResolver,
    CitySelection,
    NotificationFailure,
    NotificationPolicies,
    NotificationPreferencesRepository,
    NotificationSettingsView,
)
from app.services.notifications.rendering import settings_view
from app.services.notifications.windows import valid_zone


class NotificationPreferences:
    def __init__(
        self,
        repository: NotificationPreferencesRepository,
        *,
        clock: Clock,
        city_resolver: CityResolver,
        policies: NotificationPolicies,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._city_resolver = city_resolver
        self._policies = policies

    async def remember_private_chat(
        self,
        user_id: int,
        chat_id: int,
        chat_type: str,
    ) -> bool:
        if chat_type != "private" or user_id <= 0 or chat_id <= 0:
            return False

        await self._repository.remember_private_chat(
            user_id,
            chat_id,
            self._clock.now(),
        )
        return True

    async def notification_settings(self, user_id: int) -> NotificationSettingsView:
        settings = await self._repository.settings(user_id)
        return settings_view(settings)

    async def prepare_city_selection(self, user_id: int, query: str) -> CitySelection:
        city_query = query.strip()
        if not 1 <= len(city_query) <= 100:
            raise NotificationFailure(
                "invalid_city",
                "Укажите город и страну, например: /city Казань, Россия.",
            )

        try:
            resolved = await self._city_resolver.resolve(city_query)
        except ToolFailure as error:
            if error.code == "invalid_city":
                raise NotificationFailure("invalid_city", error.message) from None
            raise NotificationFailure(
                "geocoding_unavailable",
                "Не удалось найти город. Попробуйте позже.",
            ) from None
        except Exception:
            raise NotificationFailure(
                "geocoding_unavailable",
                "Не удалось найти город. Попробуйте позже.",
            ) from None

        candidates = _unique_candidates(resolved)[: self._policies.city_candidate_limit]
        if not candidates:
            raise NotificationFailure(
                "city_not_found",
                "Город не найден. Уточните название или добавьте страну.",
            )

        now = self._clock.now()
        selection = CitySelectionRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            candidates=candidates,
            state=CitySelectionState.PENDING,
            created_at=now,
            expires_at=now + self._policies.city_selection_ttl,
            selected_index=None,
        )
        await self._repository.create_city_selection(selection)
        return CitySelection(
            id=selection.id,
            candidates=selection.candidates,
            expires_at=selection.expires_at,
        )

    async def confirm_city_selection(
        self,
        user_id: int,
        selection_id: uuid.UUID,
        index: int,
    ) -> NotificationSettingsView:
        settings = await self._repository.confirm_city_selection(
            selection_id,
            user_id,
            index,
            self._clock.now(),
        )
        if settings is None:
            raise NotificationFailure(
                "selection_unavailable",
                "Выбор города недоступен или устарел. Отправьте /city ещё раз.",
            )
        return settings_view(settings)

    async def cancel_city_selection(self, user_id: int, selection_id: uuid.UUID) -> None:
        cancelled = await self._repository.cancel_city_selection(
            selection_id,
            user_id,
            self._clock.now(),
        )
        if not cancelled:
            raise NotificationFailure(
                "selection_unavailable",
                "Выбор города уже недоступен.",
            )

    async def enable_briefing(self, user_id: int) -> NotificationSettingsView:
        settings = await self._repository.settings(user_id)
        if settings is None or settings.destination_chat_id is None:
            raise NotificationFailure(
                "destination_required",
                "Сначала отправьте боту сообщение в личном чате.",
            )
        if settings.city is None or settings.city_confirmed_at is None:
            raise NotificationFailure(
                "city_required",
                "Сначала выберите город командой /city <город, страна> и подтвердите выбор.",
            )
        if valid_zone(settings.timezone) is None:
            raise NotificationFailure(
                "timezone_required",
                "Сначала задайте часовой пояс IANA, например /timezone Europe/Moscow.",
            )

        updated = await self._repository.set_briefing_enabled(
            user_id,
            True,
            self._clock.now(),
        )
        if updated is None:
            raise NotificationFailure(
                "briefing_unavailable",
                "Не удалось включить сводку. Проверьте город и часовой пояс.",
            )
        return settings_view(updated)

    async def disable_briefing(self, user_id: int) -> NotificationSettingsView:
        updated = await self._repository.set_briefing_enabled(
            user_id,
            False,
            self._clock.now(),
        )
        return settings_view(updated)


def _unique_candidates(candidates: Sequence[CityRecord]) -> tuple[CityRecord, ...]:
    unique: dict[tuple[object, ...], CityRecord] = {}

    for candidate in candidates:
        name = candidate.name.strip()
        country = candidate.country.strip()
        if not name or not country:
            continue
        if not math.isfinite(candidate.latitude) or not -90 <= candidate.latitude <= 90:
            continue
        if not math.isfinite(candidate.longitude) or not -180 <= candidate.longitude <= 180:
            continue

        normalized = CityRecord(
            name=name,
            country=country,
            region=candidate.region.strip() if candidate.region else None,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
        )
        key = (
            normalized.name.casefold(),
            normalized.country.casefold(),
            round(normalized.latitude, 5),
            round(normalized.longitude, 5),
        )
        unique.setdefault(key, normalized)

    return tuple(unique.values())
