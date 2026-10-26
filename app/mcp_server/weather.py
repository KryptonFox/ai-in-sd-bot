from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.schemas.tools import CityCandidate, ToolFailure, WeatherResult


class WeatherService:
    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_url = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_weather(self, city: str) -> WeatherResult:
        city = city.strip()
        if not 1 <= len(city) <= 100:
            raise ToolFailure("invalid_city", "Укажите город длиной от 1 до 100 символов.")
        requested_city, requested_country = self._split_location(city)
        params: dict[str, str | int] = {
            "name": requested_city,
            "count": 10,
            "language": "ru",
            "format": "json",
        }
        try:
            geo_response = await self._get(self.geocoding_url, params)
            geo_data = geo_response.json()
        except ToolFailure:
            raise
        except (ValueError, TypeError) as exc:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды вернул некорректные данные."
            ) from exc
        if not isinstance(geo_data, dict) or not isinstance(geo_data.get("results", []), list):
            raise ToolFailure(
                "weather_unavailable", "Сервис геокодирования вернул некорректные данные."
            )
        results = geo_data.get("results") or []
        if requested_country:
            results = [item for item in results if self._qualifier_matches(item, requested_country)]
        if not results:
            return WeatherResult(status="not_found")
        candidates = [self._candidate(item) for item in results if self._valid_geo(item)]
        if not candidates:
            raise ToolFailure(
                "weather_unavailable", "Сервис геокодирования вернул некорректные данные."
            )
        if len(candidates) > 1:
            return WeatherResult(status="ambiguous", candidates=candidates)
        location = next(item for item in results if self._valid_geo(item))
        return await self._weather_for(location)

    async def _weather_for(self, location: dict[str, Any]) -> WeatherResult:
        params = {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
        }
        try:
            response = await self._get(self.forecast_url, params)
            data = response.json()
        except ToolFailure:
            raise
        except (ValueError, TypeError) as exc:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды вернул некорректные данные."
            ) from exc
        current = data.get("current") if isinstance(data, dict) else None
        units = data.get("current_units") if isinstance(data, dict) else None
        required = ("temperature_2m", "weather_code", "wind_speed_10m", "time")
        if (
            not isinstance(current, dict)
            or not isinstance(units, dict)
            or any(key not in current for key in required)
            or not isinstance(data.get("timezone"), str)
            or not data["timezone"]
        ):
            raise ToolFailure("weather_unavailable", "Сервис погоды вернул неполные данные.")
        try:
            return WeatherResult(
                status="ok",
                city=location["name"],
                country=location.get("country"),
                temperature=float(current["temperature_2m"]),
                weather_code=int(current["weather_code"]),
                wind_speed=float(current["wind_speed_10m"]),
                wind_unit=units.get("wind_speed_10m", "km/h"),
                observed_at=str(current["time"]),
                timezone=data["timezone"],
            )
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды вернул некорректные данные."
            ) from exc

    async def _get(self, url: str, params: dict[str, str | int]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.get(url, params=params, timeout=5.0)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise ToolFailure(
                        "weather_unavailable", "Сервис погоды сейчас недоступен."
                    ) from exc
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt == 0:
                await asyncio.sleep(0)
        raise ToolFailure("weather_unavailable", "Сервис погоды сейчас недоступен.") from last_error

    @staticmethod
    def _split_location(city: str) -> tuple[str, str | None]:
        pieces = [part.strip() for part in city.split(",", maxsplit=1)]
        return pieces[0], pieces[1] if len(pieces) == 2 and pieces[1] else None

    @staticmethod
    def _qualifier_matches(item: object, qualifier: str) -> bool:
        return isinstance(item, dict) and any(
            str(item.get(key, "")).casefold() == qualifier.casefold()
            for key in ("country", "admin1")
        )

    @staticmethod
    def _valid_geo(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("country"), str)
            and isinstance(item.get("latitude"), (int, float))
            and isinstance(item.get("longitude"), (int, float))
        )

    @staticmethod
    def _candidate(item: dict[str, Any]) -> CityCandidate:
        return CityCandidate(
            name=item["name"],
            country=item["country"],
            region=item.get("admin1") if isinstance(item.get("admin1"), str) else None,
        )
