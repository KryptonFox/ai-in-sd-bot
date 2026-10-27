from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeatherArguments(ToolDTO):
    city: str = Field(min_length=1, max_length=100, pattern=r"\S")
    date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        json_schema_extra={"format": "date"},
        description=(
            "Дата прогноза YYYY-MM-DD. Если не задана — текущая погода и прогноз на сегодня."
        ),
    )

    @field_validator("date")
    @classmethod
    def validate_forecast_date(cls, value: str | None) -> str | None:
        if value is not None and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
            raise ValueError("date must be a real date in YYYY-MM-DD format")
        return value

    @field_validator("city", mode="before")
    @classmethod
    def strip_city(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ScheduleArguments(ToolDTO):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"})

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be a real date in YYYY-MM-DD format") from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("date must be a real date in YYYY-MM-DD format")
        return value


class ScheduleWeekArguments(ToolDTO):
    date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        json_schema_extra={"format": "date"},
        description=(
            "Дата внутри конкретной недели. Для относительных недель используй week_offset."
        ),
    )
    week_offset: int = Field(
        default=0, ge=-52, le=52, description="0 — эта неделя, 1 — следующая, 2 — через две недели."
    )

    @field_validator("date")
    @classmethod
    def validate_week_date(cls, value: str | None) -> str | None:
        return ScheduleArguments(date=value).date if value is not None else None

    @model_validator(mode="after")
    def validate_selection(self) -> "ScheduleWeekArguments":
        if self.date is not None and self.week_offset != 0:
            raise ValueError("choose date or week_offset")
        return self


class ReminderArguments(ToolDTO):
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    remind_at: datetime

    @field_validator("text", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("remind_at", mode="before")
    @classmethod
    def accept_iso_string_or_datetime(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("remind_at must be an ISO 8601 string or datetime")
        return value

    @field_validator("remind_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("remind_at must include a timezone")
        return value


class CityCandidate(ToolDTO):
    name: str
    country: str
    region: str | None = None


class DailyForecast(ToolDTO):
    date: date
    precipitation_probability: float = Field(ge=0, le=100, allow_inf_nan=False)
    precipitation: float = Field(ge=0, allow_inf_nan=False)
    precipitation_unit: Literal["mm"] = "mm"
    period: Literal["whole_day"] = "whole_day"
    temperature_min: float | None = Field(default=None, allow_inf_nan=False)
    temperature_max: float | None = Field(default=None, allow_inf_nan=False)
    temperature_unit: Literal["°C"] = "°C"
    weather_code: int | None = None
    condition: str | None = None
    wind_speed_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wind_unit: Literal["km/h"] = "km/h"

    @model_validator(mode="after")
    def validate_temperature_range(self) -> "DailyForecast":
        if (
            self.temperature_min is not None
            and self.temperature_max is not None
            and self.temperature_min > self.temperature_max
        ):
            raise ValueError("minimum temperature exceeds maximum")
        return self


class WeatherResult(ToolDTO):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "ok"}}, "required": ["status"]},
                    "then": {
                        "required": [
                            "city",
                            "country",
                            "temperature",
                            "weather_code",
                            "wind_speed",
                            "wind_unit",
                            "observed_at",
                            "timezone",
                        ],
                        "properties": {
                            key: {"not": {"type": "null"}}
                            for key in (
                                "city",
                                "country",
                                "temperature",
                                "weather_code",
                                "wind_speed",
                                "wind_unit",
                                "observed_at",
                                "timezone",
                            )
                        },
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "ambiguous"}},
                        "required": ["status"],
                    },
                    "then": {
                        "required": ["candidates"],
                        "properties": {"candidates": {"minItems": 2}},
                    },
                },
            ]
        },
    )
    status: Literal["ok", "ambiguous", "not_found"]
    city: str | None = None
    country: str | None = None
    temperature: float | None = Field(default=None, allow_inf_nan=False)
    weather_code: int | None = None
    wind_speed: float | None = Field(default=None, allow_inf_nan=False)
    wind_unit: str | None = None
    observed_at: str | None = None
    condition: str | None = None
    timezone: str | None = None
    candidates: list[CityCandidate] = Field(default_factory=list)
    today: DailyForecast | None = None
    requested_date: date | None = None
    forecast: DailyForecast | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> "WeatherResult":
        if self.status == "ok" and any(
            value is None
            for value in (
                self.city,
                self.country,
                self.temperature,
                self.weather_code,
                self.wind_speed,
                self.wind_unit,
                self.observed_at,
                self.timezone,
            )
        ):
            raise ValueError("ok weather results must contain complete current conditions")
        if self.status == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous weather results must contain multiple candidates")
        return self


class Lesson(ToolDTO):
    start: str
    end: str
    title: str
    location: str


class ScheduleResult(ToolDTO):
    date: str
    lessons: list[Lesson] = Field(default_factory=list)
    data_available: bool = True
    is_free_day: bool | None = None
    weekday: str | None = None


class WeekSchedule(ToolDTO):
    timezone: str
    start_date: str
    end_date: str
    days: list[ScheduleResult] = Field(default_factory=list)
    free_days: list[str] = Field(default_factory=list)


class ReminderResult(ToolDTO):
    id: int
    text: str
    remind_at: datetime
    timezone: str
    created: bool

    @field_validator("remind_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("remind_at must include a timezone")
        return value


class ToolErrorDetail(ToolDTO):
    code: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    message: str


class ToolErrorResult(ToolDTO):
    error: ToolErrorDetail


class ToolFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        if not code or not code.replace("_", "").isalnum():
            raise ValueError("code must be a safe identifier")
        super().__init__(message)
        self.code = code
        self.message = message
