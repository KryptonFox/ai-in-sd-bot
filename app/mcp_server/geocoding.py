import logging
import unicodedata
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.schemas.tools import ToolFailure

type FetchJSON = Callable[[str, dict[str, str | int]], Awaitable[dict[str, object]]]
logger = logging.getLogger("app.mcp_server.geocoding")
MAJOR_CITY_POPULATION = 100_000
DOMINANT_POPULATION_RATIO = 20


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


class GeoLocation(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: int | None = None
    name: str = Field(min_length=1)
    country: str = Field(min_length=1)
    country_code: str | None = None
    admin1: str | None = None
    population: int = Field(default=0, ge=0)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class PhotonProperties(BaseModel):
    name: str
    country: str
    countrycode: str | None = None
    state: str | None = None
    type: str
    osm_value: str


class PhotonGeometry(BaseModel):
    coordinates: tuple[float, float]


class PhotonFeature(BaseModel):
    properties: PhotonProperties
    geometry: PhotonGeometry


class LocationResolver:
    url = "https://geocoding-api.open-meteo.com/v1/search"

    def __init__(self, fetch: FetchJSON) -> None:
        self._fetch = fetch

    async def resolve(self, query: str) -> list[GeoLocation]:
        city = query.strip()
        if not 1 <= len(city) <= 100:
            raise ToolFailure("invalid_city", "Укажите город длиной от 1 до 100 символов.")
        name, _, qualifier = city.partition(",")
        try:
            data = await self._fetch(
                self.url, {"name": city, "count": 10, "language": "ru", "format": "json"}
            )
        except ToolFailure as error:
            if error.code != "weather_unavailable" or not isinstance(
                error.__cause__, httpx.HTTPError
            ):
                raise
            logger.warning("geocoding_fallback provider=photon reason=%s", error.code)
            data = await self._photon(city)
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
        matches = exact or list(unique.values())
        return self._prefer_major_city(matches) if exact else matches

    @staticmethod
    def _prefer_major_city(locations: list[GeoLocation]) -> list[GeoLocation]:
        if len(locations) < 2:
            return locations
        ranked = sorted(locations, key=lambda place: place.population, reverse=True)
        largest, runner_up = ranked[:2]
        if (
            largest.population >= MAJOR_CITY_POPULATION
            and runner_up.population < MAJOR_CITY_POPULATION
            and largest.population >= DOMINANT_POPULATION_RATIO * max(runner_up.population, 1)
        ):
            return [largest]
        return locations

    async def _photon(self, city: str) -> dict[str, object]:
        data = await self._fetch(
            "https://photon.komoot.io/api", {"q": city, "limit": 10, "layer": "city"}
        )
        raw = data.get("features")
        if not isinstance(raw, list):
            raise ToolFailure("weather_unavailable", "Геокодирование вернуло некорректные данные.")
        features: list[PhotonFeature] = []
        for item in raw:
            try:
                feature = PhotonFeature.model_validate(item)
                if feature.properties.type == "city":
                    features.append(feature)
            except ValidationError:
                continue
        urban = [item for item in features if item.properties.osm_value in {"city", "town"}]
        features = urban or features
        name = normalized_name(city.partition(",")[0])
        exact = [item for item in features if normalized_name(item.properties.name) == name]
        features = exact or features
        locations: list[dict[str, object]] = []
        for feature in features:
            try:
                place = feature.properties
                longitude, latitude = feature.geometry.coordinates
                location = GeoLocation(
                    name=place.name,
                    country=place.country,
                    country_code=place.countrycode,
                    admin1=place.state,
                    latitude=latitude,
                    longitude=longitude,
                )
                locations.append(location.model_dump())
            except ValidationError:
                continue
        if raw and not locations:
            raise ToolFailure("weather_unavailable", "Геокодирование вернуло некорректные данные.")
        return {"results": locations}
