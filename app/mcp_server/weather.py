from __future__ import annotations

import asyncio
from datetime import date
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.core.clock import Clock, SystemClock
from app.core.schemas.tools import CityCandidate, DailyForecast, ToolFailure, WeatherResult
from app.mcp_server.geocoding import GeoLocation, LocationResolver


class WeatherService:
    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_url = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, client: httpx.AsyncClient, *, clock: Clock | None = None) -> None:
        self._client = client
        self._clock = clock or SystemClock()
        self._locations = LocationResolver(self._fetch)

    async def get_weather(self, city: str) -> WeatherResult:
        locations = await self._locations.resolve(city)
        if not locations:
            return WeatherResult(status="not_found")
        if len(locations) > 1:
            return WeatherResult(
                status="ambiguous",
                candidates=[
                    CityCandidate(name=place.name, country=place.country, region=place.admin1)
                    for place in locations
                ],
            )
        return await self._weather_for(locations[0])

    async def _fetch(self, url: str, params: dict[str, str | int]) -> dict[str, object]:
        response = await self._get(url, params)
        try:
            data = response.json()
        except ValueError:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды вернул некорректные данные."
            ) from None
        if not isinstance(data, dict):
            raise ToolFailure("weather_unavailable", "Сервис погоды вернул некорректные данные.")
        return cast(dict[str, object], data)

    async def _weather_for(self, location: GeoLocation) -> WeatherResult:
        params: dict[str, str | int] = {
            "latitude": str(location.latitude),
            "longitude": str(location.longitude),
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
            "daily": "precipitation_probability_max,precipitation_sum",
            "forecast_days": 1,
            "precipitation_unit": "mm",
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
                city=location.name,
                country=location.country,
                temperature=float(current["temperature_2m"]),
                weather_code=int(current["weather_code"]),
                wind_speed=float(current["wind_speed_10m"]),
                wind_unit=units.get("wind_speed_10m", "km/h"),
                observed_at=str(current["time"]),
                timezone=data["timezone"],
                today=self._today(data),
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

    def _today(self, data: dict[str, object]) -> DailyForecast | None:
        daily = data.get("daily")
        if daily is None:
            return None
        if not isinstance(daily, dict):
            raise ValueError("invalid_daily_forecast")
        dates = daily.get("time")
        chances = daily.get("precipitation_probability_max")
        amounts = daily.get("precipitation_sum")
        units = data.get("daily_units")
        if not all(
            isinstance(values, list) and len(values) == 1 for values in (dates, chances, amounts)
        ):
            raise ValueError("invalid_daily_forecast")
        assert isinstance(dates, list) and isinstance(chances, list) and isinstance(amounts, list)
        if chances[0] is None or amounts[0] is None:
            return None
        if (
            not isinstance(units, dict)
            or units.get("precipitation_sum") != "mm"
            or units.get("precipitation_probability_max") != "%"
        ):
            raise ValueError("invalid_daily_units")
        forecast_date = date.fromisoformat(dates[0])
        timezone = data.get("timezone")
        if (
            not isinstance(timezone, str)
            or forecast_date != self._clock.now().astimezone(ZoneInfo(timezone)).date()
        ):
            raise ValueError("forecast_date_mismatch")
        return DailyForecast(
            date=forecast_date, precipitation_probability=chances[0], precipitation=amounts[0]
        )
