import base64
import binascii
import io
import warnings
import zlib
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from PIL import Image
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_WEATHER_CITY = "Санкт-Петербург"
PNG_MIME_TYPE = "image/png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 256 * 1024
MAX_IMAGE_PIXELS = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_DECODED_IMAGE_BYTES = 32 * 1024 * 1024
MAX_AGENT_IMAGES = 2
MAX_AGENT_IMAGE_BYTES = MAX_IMAGE_BYTES * MAX_AGENT_IMAGES
MAX_BASE64_LENGTH = ((MAX_IMAGE_BYTES + 2) // 3) * 4
_PNG_COLOR_DEPTHS: dict[int, frozenset[int]] = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
CityName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"\S",
        description="Город. Если не указан — Санкт-Петербург.",
    ),
]
ReminderText = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
WeekOffset = Annotated[
    int,
    Field(
        ge=-52,
        le=52,
        description="0 — эта неделя, 1 — следующая, 2 — через две недели.",
    ),
]


def _validate_iso_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("date must be a real date in YYYY-MM-DD format")
    return value


ISODate = Annotated[
    str,
    Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"}),
    AfterValidator(_validate_iso_date),
]


class WeatherStatus(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class ToolDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _png_pixel_data_size(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace_method: int,
) -> int:
    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth

    if interlace_method == 0:
        row_bytes = (width * bits_per_pixel + 7) // 8
        return height * (1 + row_bytes)

    total = 0
    for x_start, y_start, x_step, y_step in _ADAM7_PASSES:
        pass_width = (width - x_start + x_step - 1) // x_step if width > x_start else 0
        pass_height = (height - y_start + y_step - 1) // y_step if height > y_start else 0
        if pass_width == 0 or pass_height == 0:
            continue

        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        total += pass_height * (1 + row_bytes)

    return total


def _validate_png_stream(data: bytes, expected_size: int) -> None:
    decompressor = zlib.decompressobj()

    try:
        decoded = decompressor.decompress(data, expected_size + 1)
    except zlib.error:
        raise ValueError("invalid PNG pixel data") from None

    if len(decoded) != expected_size or decompressor.unused_data or not decompressor.eof:
        raise ValueError("invalid PNG pixel data")


def _validate_png_image(raw: bytes) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG":
                    raise ValueError("image data must be a PNG file")
                image.load()
    except Exception as error:
        raise ValueError("image data must be a valid PNG file") from error


def _validate_png(raw: bytes) -> None:
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("image data must be a PNG file")

    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int] | None = None
    image_data: list[bytes] = []
    seen_end = False

    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated PNG chunk")
        length = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(raw):
            raise ValueError("truncated PNG chunk")
        if not chunk_type.isalpha():
            raise ValueError("invalid PNG chunk type")

        payload = raw[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(raw[offset + 8 + length : chunk_end], "big")
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            raise ValueError("PNG chunk checksum mismatch")

        if header is None:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("PNG must start with an IHDR chunk")
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth = payload[8]
            color_type = payload[9]
            compression_method = payload[10]
            filter_method = payload[11]
            interlace_method = payload[12]
            if width == 0 or height == 0:
                raise ValueError("PNG dimensions must be positive")
            if bit_depth not in _PNG_COLOR_DEPTHS.get(color_type, frozenset()):
                raise ValueError("unsupported PNG color depth")
            if compression_method != 0 or filter_method != 0 or interlace_method not in {0, 1}:
                raise ValueError("unsupported PNG encoding")
            header = (width, height, bit_depth, color_type, interlace_method)
        elif chunk_type == b"IHDR":
            raise ValueError("duplicate PNG header")
        elif chunk_type == b"IDAT":
            image_data.append(payload)
        elif chunk_type == b"IEND":
            if length != 0:
                raise ValueError("invalid PNG end chunk")
            if chunk_end != len(raw):
                raise ValueError("unexpected data after PNG end")
            seen_end = True
            break

        offset = chunk_end

    if header is None or not image_data or not seen_end:
        raise ValueError("incomplete PNG file")

    width, height, bit_depth, color_type, interlace_method = header
    if (
        width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ValueError("PNG dimensions exceed the allowed limit")

    expected_size = _png_pixel_data_size(width, height, bit_depth, color_type, interlace_method)
    if expected_size > MAX_DECODED_IMAGE_BYTES:
        raise ValueError("PNG pixel data exceeds the allowed limit")

    _validate_png_stream(b"".join(image_data), expected_size)
    _validate_png_image(raw)


def _decode_png_base64(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("image data must be a non-empty base64 string")
    if len(value) > MAX_BASE64_LENGTH:
        raise ValueError("image exceeds the size limit")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("image data must be valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds the size limit")
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("image data must use canonical base64")
    _validate_png(raw)
    return raw


class ToolImage(ToolDTO):
    data: str
    mime_type: Literal["image/png"] = PNG_MIME_TYPE

    @field_validator("data")
    @classmethod
    def validate_png_data(cls, value: str) -> str:
        _decode_png_base64(value)
        return value


class PhotoToolResult(ToolDTO):
    payload: dict[str, object]
    images: list[ToolImage] = Field(default_factory=list, max_length=MAX_AGENT_IMAGES)


class WeatherArguments(ToolDTO):
    city: CityName = DEFAULT_WEATHER_CITY
    date: ISODate | None = Field(
        default=None,
        description=(
            "Дата прогноза YYYY-MM-DD. Если не задана — текущая погода и прогноз на сегодня."
        ),
    )

    @field_validator("city", mode="before")
    @classmethod
    def strip_city(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ScheduleArguments(ToolDTO):
    date: ISODate | None = Field(
        default=None,
        description="Дата YYYY-MM-DD. Если не задана — сегодня в часовом поясе расписания.",
    )


class ScheduleWeekArguments(ToolDTO):
    date: ISODate | None = Field(
        default=None,
        description=(
            "Дата внутри конкретной недели. Для относительных недель используй week_offset."
        ),
    )
    week_offset: WeekOffset = 0

    @model_validator(mode="after")
    def validate_selection(self) -> "ScheduleWeekArguments":
        if self.date is not None and self.week_offset != 0:
            raise ValueError("choose date or week_offset")
        return self


class ReminderArguments(ToolDTO):
    text: ReminderText
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
    status: WeatherStatus
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
        if self.status == WeatherStatus.OK and any(
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
        if self.status == WeatherStatus.AMBIGUOUS and len(self.candidates) < 2:
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


TOOL_ERROR_MESSAGES: dict[str, str] = {
    "forecast_date_unavailable": "Прогноз доступен на сегодня и следующие 15 дней.",
    "weather_unavailable": "Сервис погоды сейчас недоступен. Попробуйте позже.",
    "schedule_unavailable": "Не удалось прочитать расписание. Попробуйте позже.",
    "materials_unavailable": "Учебные материалы сейчас недоступны. Попробуйте позже.",
    "unknown_subject": "Предмет не найден в каталоге учебных материалов.",
    "web_search_disabled": "Веб-поиск не настроен.",
    "web_search_not_allowed": "Веб-поиск требует явного разрешения пользователя.",
    "web_search_unavailable": "Веб-поиск сейчас недоступен. Попробуйте позже.",
    "timeout": "Вычисление не уложилось в отведённое время.",
    "queue_timeout": "Вычислитель занят: запрос не дождался свободного исполнителя.",
    "worker_start_timeout": "Вычислительный процесс не запустился вовремя.",
    "worker_failed": "Вычислительный процесс завершился аварийно.",
    "memory_limit": "Вычисление превысило выделенный лимит памяти.",
    "resource_limit": "Результат превышает допустимый размер или сложность.",
    "evaluation_failed": "Не удалось вычислить выражение.",
    "request_deadline": "Общее время обработки запроса истекло.",
}


class ToolFailure(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        if not code or not code.replace("_", "").isalnum():
            raise ValueError("code must be a safe identifier")
        self.code = code
        if message is None:
            message = TOOL_ERROR_MESSAGES.get(code, "Инструмент не смог выполнить запрос.")
        self.message = message
        super().__init__(self.message)
