from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.core.clock import Clock, SystemClock
from app.core.schemas.tools import (
    DEFAULT_WEATHER_CITY,
    CityCandidate,
    DailyForecast,
    ToolFailure,
    WeatherResult,
    WeatherStatus,
)
from mcp_servers.student_tools.conditions import weather_condition
from mcp_servers.student_tools.geocoding import GeoLocation, LocationResolver


class WeatherService:
    geocoding_url = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_url = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._clock = clock or SystemClock()
        self._locations = LocationResolver(self._fetch)

    async def get_weather(
        self, city: str = DEFAULT_WEATHER_CITY, forecast_date: str | None = None
    ) -> WeatherResult:
        requested = date.fromisoformat(forecast_date) if forecast_date is not None else None

        try:
            async with asyncio.timeout(8):
                return await self._resolve_weather(city, requested)
        except TimeoutError:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды не ответил вовремя. Попробуйте позже."
            ) from None

    async def _resolve_weather(
        self,
        city: str,
        requested: date | None,
    ) -> WeatherResult:
        locations = await self._locations.resolve(city)
        if not locations:
            return WeatherResult(status=WeatherStatus.NOT_FOUND)

        if len(locations) > 1:
            return WeatherResult(
                status=WeatherStatus.AMBIGUOUS,
                candidates=[
                    CityCandidate(
                        name=place.name,
                        country=place.country,
                        region=place.admin1,
                    )
                    for place in locations
                ],
            )

        return await self._weather_for(locations[0], requested)

    async def _fetch(
        self,
        url: str,
        params: dict[str, str | int],
    ) -> dict[str, object]:
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

    async def _weather_for(
        self,
        location: GeoLocation,
        requested: date | None,
    ) -> WeatherResult:
        params: dict[str, str | int] = {
            "latitude": str(location.latitude),
            "longitude": str(location.longitude),
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
            "daily": (
                "precipitation_probability_max,precipitation_sum,temperature_2m_min,"
                "temperature_2m_max,weather_code,wind_speed_10m_max"
            ),
            "forecast_days": 16 if requested is not None else 1,
            "precipitation_unit": "mm",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }
        data = await self._fetch(self.forecast_url, params)
        return self._weather_result(location, requested, data)

    def _weather_result(
        self,
        location: GeoLocation,
        requested: date | None,
        data: dict[str, object],
    ) -> WeatherResult:
        current = data.get("current")
        units = data.get("current_units")
        timezone = data.get("timezone")
        required = ("temperature_2m", "weather_code", "wind_speed_10m", "time")
        if (
            not isinstance(current, dict)
            or not isinstance(units, dict)
            or any(key not in current for key in required)
            or not isinstance(timezone, str)
            or not timezone
        ):
            raise ToolFailure("weather_unavailable", "Сервис погоды вернул неполные данные.")

        try:
            today = self._clock.now().astimezone(ZoneInfo(timezone)).date()
            target = requested or today
            days_ahead = (target - today).days
            if not 0 <= days_ahead < 16:
                raise ToolFailure(
                    "forecast_date_unavailable",
                    "Прогноз доступен только на сегодня и следующие 15 дней.",
                )

            forecasts = self._daily_forecasts(data, today, target)
            weather_code = int(current["weather_code"])

            return WeatherResult(
                status=WeatherStatus.OK,
                city=location.name,
                country=location.country,
                temperature=float(current["temperature_2m"]),
                weather_code=weather_code,
                condition=weather_condition(weather_code),
                wind_speed=float(current["wind_speed_10m"]),
                wind_unit=units.get("wind_speed_10m", "km/h"),
                observed_at=str(current["time"]),
                timezone=timezone,
                today=forecasts[today],
                requested_date=target,
                forecast=forecasts[target],
            )
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            raise ToolFailure(
                "weather_unavailable", "Сервис погоды вернул некорректные данные."
            ) from exc

    async def _get(
        self,
        url: str,
        params: dict[str, str | int],
    ) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    timeout=5.0 if url == self.forecast_url else 2.0,
                    headers={"User-Agent": "itmo-ai-in-sd-bot/0.1"},
                )
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

    def _daily_forecasts(
        self,
        data: dict[str, object],
        today: date,
        target: date,
    ) -> dict[date, DailyForecast | None]:
        forecasts: dict[date, DailyForecast | None] = {today: None, target: None}
        daily = data.get("daily")
        if daily is None:
            return forecasts

        if not isinstance(daily, dict):
            raise ValueError("invalid_daily_forecast")

        dates = daily.get("time")
        chances = daily.get("precipitation_probability_max")
        amounts = daily.get("precipitation_sum")
        units = data.get("daily_units")
        if not isinstance(dates, list) or not dates:
            raise ValueError("invalid_daily_forecast")

        if not isinstance(chances, list) or len(chances) != len(dates):
            raise ValueError("invalid_daily_forecast")

        if not isinstance(amounts, list) or len(amounts) != len(dates):
            raise ValueError("invalid_daily_forecast")

        date_indexes: dict[date, int] = {}
        expected_date = today

        for index, value in enumerate(dates):
            forecast_date = date.fromisoformat(value)
            if forecast_date != expected_date:
                raise ValueError("forecast_date_mismatch")

            date_indexes[forecast_date] = index
            expected_date += timedelta(days=1)

        for forecast_date in forecasts:
            index = date_indexes.get(forecast_date)
            if index is None or chances[index] is None or amounts[index] is None:
                continue

            if (
                not isinstance(units, dict)
                or units.get("precipitation_sum") != "mm"
                or units.get("precipitation_probability_max") != "%"
            ):
                raise ValueError("invalid_daily_units")

            conditions = self._daily_conditions(daily, units, index, len(dates))
            forecasts[forecast_date] = DailyForecast.model_validate(
                {
                    "date": forecast_date,
                    "precipitation_probability": chances[index],
                    "precipitation": amounts[index],
                    **conditions,
                }
            )

        return forecasts

    @staticmethod
    def _daily_conditions(
        daily: dict[str, object],
        units: dict[str, object],
        index: int,
        count: int,
    ) -> dict[str, object]:
        fields = {
            "temperature_2m_min": ("temperature_min", "°C"),
            "temperature_2m_max": ("temperature_max", "°C"),
            "weather_code": ("weather_code", "wmo code"),
            "wind_speed_10m_max": ("wind_speed_max", "km/h"),
        }
        result: dict[str, object] = {}

        for source, (field, unit) in fields.items():
            values = daily.get(source)
            if values is None:
                continue

            if not isinstance(values, list) or len(values) != count or units.get(source) != unit:
                raise ValueError("invalid_daily_conditions")

            result[field] = values[index]

        code = result.get("weather_code")
        if isinstance(code, int):
            result["condition"] = weather_condition(code)

        return result
