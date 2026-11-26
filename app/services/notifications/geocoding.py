import httpx

from app.core.schemas.tools import ToolFailure
from app.db.notification_records import CityRecord
from mcp_servers.student_tools.geocoding import FetchJSON, LocationResolver


class HttpCityResolver:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._timeout = timeout

    async def resolve(self, query: str) -> tuple[CityRecord, ...]:
        if self._client is not None:
            return await self._resolve(self._client, query)

        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": "itmo-ai-in-sd-bot/0.1"},
        ) as client:
            return await self._resolve(client, query)

    async def _resolve(
        self,
        client: httpx.AsyncClient,
        query: str,
    ) -> tuple[CityRecord, ...]:
        resolver = LocationResolver(self._fetcher(client))
        locations = await resolver.resolve(query)
        return tuple(
            CityRecord(
                name=location.name,
                country=location.country,
                region=location.admin1,
                latitude=location.latitude,
                longitude=location.longitude,
            )
            for location in locations
        )

    def _fetcher(self, client: httpx.AsyncClient) -> FetchJSON:
        async def fetch(url: str, params: dict[str, str | int]) -> dict[str, object]:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
            except httpx.HTTPError as error:
                raise ToolFailure(
                    "weather_unavailable",
                    "Сервис геокодирования сейчас недоступен.",
                ) from error

            try:
                data = response.json()
            except ValueError:
                raise ToolFailure(
                    "weather_unavailable",
                    "Сервис геокодирования вернул некорректные данные.",
                ) from None

            if not isinstance(data, dict):
                raise ToolFailure(
                    "weather_unavailable",
                    "Сервис геокодирования вернул некорректные данные.",
                )

            return data

        return fetch
