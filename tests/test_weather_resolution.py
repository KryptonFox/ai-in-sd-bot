import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import EventContext
from app.core.schemas.llm import LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure
from app.mcp.client import MCPClient
from app.mcp_server.server import tool_catalog
from app.mcp_server.weather import WeatherService
from app.services.agent_service import AgentService
from app.services.tool_execution import ToolExecutionService

CLOCK = FrozenClock(datetime(2026, 10, 26, 22, 30, tzinfo=UTC))
CITY = {
    "id": 551487,
    "name": "Казань",
    "country": "Россия",
    "country_code": "RU",
    "admin1": "Татарстан",
    "latitude": 55.79,
    "longitude": 49.12,
}


def forecast(chance: float = 80, amount: float = 3) -> dict[str, object]:
    return {
        "timezone": "Europe/Moscow",
        "current": {
            "temperature_2m": 8,
            "weather_code": 3,
            "wind_speed_10m": 5,
            "time": "2026-10-27T01:30",
        },
        "current_units": {"wind_speed_10m": "km/h"},
        "daily": {
            "time": ["2026-10-27"],
            "precipitation_probability_max": [chance],
            "precipitation_sum": [amount],
        },
        "daily_units": {"precipitation_probability_max": "%", "precipitation_sum": "mm"},
    }


async def test_unique_exact_city_wins_over_duplicates_and_prefix_matches() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "geocoding" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {**CITY, "id": 999, "name": "Казань-2"},
                        CITY,
                        CITY.copy(),
                    ]
                },
            )
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("казань")
    assert result.status == "ok" and result.city == "Казань"
    assert len(requests) == 2
    assert result.today is not None and result.today.date.isoformat() == "2026-10-27"
    assert result.today.precipitation_probability == 80
    assert result.today.period == "whole_day"
    assert set(requests[1].url.params["daily"].split(",")) == {
        "precipitation_probability_max",
        "precipitation_sum",
        "temperature_2m_min",
        "temperature_2m_max",
        "weather_code",
        "wind_speed_10m_max",
    }


@pytest.mark.parametrize("qualifier", ["Russia", "RU", "Татарстан"])
async def test_qualifier_reaches_provider_before_result_limit(qualifier: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            assert request.url.params["name"] == f"Казань, {qualifier}"
            return httpx.Response(200, json={"results": [CITY]})
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather(f"Казань, {qualifier}")
    assert result.status == "ok" and result.country == "Россия"


async def test_distinct_exact_places_remain_ambiguous() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    CITY,
                    {
                        **CITY,
                        "id": 999,
                        "admin1": "Другой регион",
                        "latitude": 60,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань")
    assert result.status == "ambiguous" and len(result.candidates) == 2
    assert len(calls) == 1
    assert result.candidates[0].region != result.candidates[1].region


@pytest.mark.parametrize("village_population", [None, 0, 500])
async def test_major_city_wins_over_homonymous_villages(village_population: int | None) -> None:
    requests: list[httpx.Request] = []
    village = {**CITY, "id": 999, "admin1": "Кировская область", "latitude": 60}
    if village_population is not None:
        village["population"] = village_population

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "geocoding" in request.url.host:
            return httpx.Response(
                200,
                json={"results": [village, {**CITY, "population": 1_300_000}]},
            )
        assert request.url.params["latitude"] == str(CITY["latitude"])
        assert request.url.params["longitude"] == str(CITY["longitude"])
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань")
    assert result.status == "ok" and result.city == "Казань"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("largest_population", "other_population"),
    [(1_300_000, 200_000), (10_000_000, 100_000), (150_000, 30_000), (90_000, 100)],
)
async def test_population_does_not_hide_genuinely_ambiguous_places(
    largest_population: int, other_population: int
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert "geocoding" in request.url.host
        return httpx.Response(
            200,
            json={
                "results": [
                    {**CITY, "population": largest_population},
                    {**CITY, "id": 999, "latitude": 60, "population": other_population},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань")
    assert result.status == "ambiguous" and len(result.candidates) == 2


async def test_explicit_region_takes_priority_over_city_population() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {**CITY, "population": 1_300_000},
                        {**CITY, "id": 999, "admin1": "Кировская область", "latitude": 60},
                    ]
                },
            )
        assert request.url.params["latitude"] == "60.0"
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань, Кировская область")
    assert result.status == "ok"


@pytest.mark.parametrize(
    "change", ["wrong_day", "negative_rain", "invalid_probability", "wrong_units"]
)
async def test_daily_forecast_rejects_wrong_date_units_and_values(change: str) -> None:
    data = forecast()
    daily = data["daily"]
    units = data["daily_units"]
    assert isinstance(daily, dict) and isinstance(units, dict)
    if change == "wrong_day":
        daily["time"] = ["2026-10-26"]
    elif change == "negative_rain":
        daily["precipitation_sum"] = [-1]
    elif change == "invalid_probability":
        daily["precipitation_probability_max"] = [110]
    else:
        units["precipitation_sum"] = "inch"

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [CITY]} if "geocoding" in request.url.host else data
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ToolFailure, match="некорректные данные"):
            await WeatherService(client, clock=CLOCK).get_weather("Казань")


class LocalToolClient(MCPClient):
    call_tool: AsyncMock


@pytest.mark.parametrize(
    ("query", "city", "chance"),
    [
        ("Нужен ли сегодня зонт в казани?", "Казань", 80),
        ("Как одеться в Санкт-Петербурге?", "Санкт-Петербург", 5),
        ("Будет дождь в Нижнем Новгороде?", "Нижний Новгород", 60),
        ("А зонт сегодня нужен?", "Казань", 0),
    ],
)
async def test_weather_pipeline_separates_routing_location_forecast_and_answer(
    query: str,
    city: str,
    chance: int,
) -> None:
    settings = Settings(bot_token="unused", postgres_password="unused")
    tool = LocalToolClient(settings)
    tool.load_catalog(tool_catalog())
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "geocoding" in request.url.host:
            assert request.url.params["name"] == city
            return httpx.Response(
                200,
                json={
                    "results": [
                        {**CITY, "name": city, "population": 1_300_000},
                        {
                            **CITY,
                            "id": 999,
                            "name": city,
                            "admin1": "Кировская область",
                            "latitude": 60,
                        },
                    ]
                },
            )
        return httpx.Response(200, json=forecast(chance, 3 if chance else 0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        weather = WeatherService(http, clock=CLOCK)

        async def execute(name, arguments, **kwargs):
            assert name == "get_weather"
            payload = (await weather.get_weather(arguments["city"])).model_dump(mode="json")
            return tool._validated_output(name, payload)

        tool.call_tool = AsyncMock(side_effect=execute)

        async def generate(request):
            results = [message for message in request.messages if message.role == MessageRole.TOOL]
            if not results:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="weather", name="get_weather", arguments={"city": city})
                    ],
                )
            data = json.loads(results[-1].content)["data"]
            assert data["city"] == city
            assert data["today"]["date"] == "2026-10-27"
            assert data["today"]["period"] == "whole_day"
            return LLMResponse(
                content=(
                    f"Прогноз за весь день в {city}: "
                    f"вероятность осадков {data['today']['precipitation_probability']}%."
                )
            )

        llm = MagicMock(generate=AsyncMock(side_effect=generate))
        agent = AgentService(llm, ToolExecutionService(tool, clock=CLOCK), clock=CLOCK)
        result = await agent.run(
            [
                ChatMessage(role=MessageRole.SYSTEM, content="Условия учебного бота"),
                ChatMessage(role=MessageRole.USER, content="Я в Казани."),
                ChatMessage(role=MessageRole.ASSISTANT, content="Понял."),
                ChatMessage(role=MessageRole.USER, content=query),
            ],
            EventContext(user_id=1, event_id="test", request_id="test"),
        )
    first_request = llm.generate.await_args_list[0].args[0]
    assert "в Казани → Казань" in first_request.messages[0].content
    assert "используй Санкт-Петербург по умолчанию, не спрашивай город" in (
        first_request.messages[0].content
    )
    tool.call_tool.assert_awaited_once_with("get_weather", {"city": city})
    assert len(requests) == 2
    assert result.decision == "tool" and str(float(chance)) in result.text


async def test_missing_probability_preserves_current_weather_without_inventing_forecast() -> None:
    data = forecast()
    daily = data["daily"]
    assert isinstance(daily, dict)
    daily["precipitation_probability_max"] = [None]

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [CITY]} if "geocoding" in request.url.host else data
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань")
    assert result.status == "ok" and result.temperature == 8
    assert result.today is None


async def test_unknown_location_returns_clarification_without_forecast_or_second_generation() -> (
    None
):
    settings = Settings(bot_token="unused", postgres_password="unused")
    tool = LocalToolClient(settings)
    tool.load_catalog(tool_catalog())
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "geocoding" in request.url.host
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        weather = WeatherService(http, clock=CLOCK)

        async def execute(name, arguments, **kwargs):
            return (await weather.get_weather(arguments["city"])).model_dump(mode="json")

        tool.call_tool = AsyncMock(side_effect=execute)
        llm = MagicMock(
            generate=AsyncMock(
                return_value=LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="weather",
                            name="get_weather",
                            arguments={"city": "Несуществующий город"},
                        )
                    ],
                )
            )
        )
        result = await AgentService(llm, ToolExecutionService(tool)).run(
            [], EventContext(user_id=1, event_id="unknown", request_id="test")
        )
    assert result.decision == "clarify" and "не найден" in result.text
    assert len(requests) == 1
    llm.generate.assert_awaited_once()


async def test_missing_weather_arguments_use_saint_petersburg_and_today() -> None:
    tool = LocalToolClient(Settings(bot_token="unused", postgres_password="unused"))
    tool.load_catalog(tool_catalog())
    tool.validate_arguments("get_weather", {})
    tool.validate_arguments("get_schedule", {})

    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            assert request.url.params["name"] == "Санкт-Петербург"
            return httpx.Response(200, json={"results": [{**CITY, "name": "Санкт-Петербург"}]})
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather()
    assert result.status == "ok" and result.city == "Санкт-Петербург"
    assert result.requested_date is not None
    assert result.requested_date.isoformat() == "2026-10-27"
