import unicodedata
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.schemas.tools import ToolFailure

type FetchJSON = Callable[[str, dict[str, str | int]], Awaitable[dict[str, object]]]


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


class GeoLocation(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: int | None = None
    name: str = Field(min_length=1)
    country: str = Field(min_length=1)
    country_code: str | None = None
    admin1: str | None = None
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class LocationResolver:
    url = "https://geocoding-api.open-meteo.com/v1/search"

    def __init__(self, fetch: FetchJSON) -> None:
        self._fetch = fetch

    async def resolve(self, query: str) -> list[GeoLocation]:
        city = query.strip()
        if not 1 <= len(city) <= 100:
            raise ToolFailure("invalid_city", "Укажите город длиной от 1 до 100 символов.")
        name, _, qualifier = city.partition(",")
        data = await self._fetch(
            self.url, {"name": city, "count": 10, "language": "ru", "format": "json"}
        )
        raw = data.get("results", [])
        if not isinstance(raw, list):
            raise ToolFailure("weather_unavailable", "Геокодирование вернуло некорректные данные.")
        locations: list[GeoLocation] = []
        for item in raw:
            try:
                locations.append(GeoLocation.model_validate(item))
            except ValidationError:
                continue
        if raw and not locations:
            raise ToolFailure("weather_unavailable", "Геокодирование вернуло некорректные данные.")
        qualified = (
            [
                place
                for place in locations
                if normalized_name(qualifier)
                in {
                    normalized_name(place.country),
                    normalized_name(place.admin1 or ""),
                    normalized_name(place.country_code or ""),
                }
            ]
            if qualifier.strip()
            else []
        )
        if qualified:
            locations = qualified
        unique: dict[tuple[object, ...], GeoLocation] = {}
        for place in locations:
            key = (
                (place.id,)
                if place.id is not None
                else (
                    normalized_name(place.name),
                    normalized_name(place.country),
                    round(place.latitude, 5),
                    round(place.longitude, 5),
                )
            )
            unique.setdefault(key, place)
        exact = [
            place
            for place in unique.values()
            if normalized_name(place.name) == normalized_name(name)
        ]
        return exact or list(unique.values())
