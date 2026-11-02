from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.core.exceptions import LLMErrorCode, LLMServiceError
from app.core.schemas.agent import AgentDecision, AgentResult
from app.core.schemas.llm import LLMResponse
from app.modes.definitions import STUDY_MODE
from scripts import run_lab1_experiment, run_routing_eval


async def test_lab1_keeps_retry_limit_run_numbers_and_prompt_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_lab1_experiment, "TEMPERATURES", [0.0, 1.0])
    monkeypatch.setattr(run_lab1_experiment, "RUNS_PER_TEMP", 2)
    monkeypatch.setattr(run_lab1_experiment.asyncio, "sleep", AsyncMock())
    response = LLMResponse(
        content="Процессы и потоки: GIL. Вывод. ```python\npass\n```", total_tokens=12
    )
    client = MagicMock(generate=AsyncMock(side_effect=[ConnectionError(), *[response] * 6]))
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        llm_model="test-model",
    )

    result = await run_lab1_experiment._run_experiment(settings, client)

    runs = result["runs"]
    assert isinstance(runs, list)
    assert [run["run_idx"] for run in runs] == [1, 2, 3, 4]
    assert [run["temperature"] for run in runs] == [0.0, 0.0, 1.0, 1.0]
    assert [run["repeat"] for run in runs] == [1, 2, 1, 2]
    assert all(run["format_ok"] for run in runs)
    assert all(run["full_content"] == response.content for run in runs)
    comparison = result["comparison"]
    assert isinstance(comparison, dict)
    assert set(comparison) == {"bare", "rctf"}
    assert client.generate.await_count == 7
    requests = [call.args[0] for call in client.generate.await_args_list]
    assert all(request.model == "test-model" and request.max_tokens == 1500 for request in requests)


async def test_routing_evaluation_retains_not_run_cases_after_two_connection_errors() -> None:
    cases: list[run_routing_eval.RoutingCase] = [
        {
            "id": f"case-{number}",
            "query": "Вопрос",
            "expected_action": "respond",
            "expected_arguments": {},
        }
        for number in range(4)
    ]
    service = MagicMock(
        run=AsyncMock(side_effect=[ConnectionError(), AgentResult(text="Ответ"), ConnectionError()])
    )
    context: run_routing_eval.EvaluationContext = {
        "now": "2026-10-28T12:00:00+03:00",
        "timezone": "Europe/Moscow",
    }

    results, counts = await run_routing_eval._evaluate_cases(
        service,
        cases,
        context,
        "test-model",
        STUDY_MODE,
    )

    assert counts == {"planned": 4, "attempted": 3, "decisions_received": 1, "not_run": 1}
    assert results[1]["success"] and results[1]["public_answer"] == "Ответ"
    assert results[3]["error_class"] == "NotRunAfterFailure"
    assert service.run.await_count == 3
    assert all(call.kwargs["dry_run"] for call in service.run.await_args_list)


async def test_routing_evaluation_keeps_raw_arguments_alongside_applied_defaults() -> None:
    cases: list[run_routing_eval.RoutingCase] = [
        {
            "id": "weather",
            "query": "Погода в Казани?",
            "expected_action": "get_weather",
            "expected_arguments": {"city": "Казань"},
        }
    ]
    arguments: dict[str, object] = {"city": " Казань ", "date": None}
    result = AgentResult(
        text="Выбран инструмент",
        decision=AgentDecision.TOOL,
        tool_name="get_weather",
        arguments=arguments,
    )
    service = MagicMock(run=AsyncMock(return_value=result))
    context: run_routing_eval.EvaluationContext = {
        "now": "2026-10-28T12:00:00+03:00",
        "timezone": "Europe/Moscow",
    }

    rows, counts = await run_routing_eval._evaluate_cases(
        service,
        cases,
        context,
        "test-model",
        STUDY_MODE,
    )

    assert counts["decisions_received"] == 1
    assert rows[0]["success"]
    assert rows[0]["arguments"] == arguments
    assert rows[0]["effective_arguments"] == {"city": "казань", "date": "2026-10-28"}
    assert result.arguments == arguments


@pytest.mark.parametrize(("status", "attempted"), [(503, 2), (429, 1)])
async def test_routing_records_provider_status_and_stops_on_rate_limit(
    status: int,
    attempted: int,
) -> None:
    cases: list[run_routing_eval.RoutingCase] = [
        {
            "id": str(number),
            "query": "Вопрос",
            "expected_action": "respond",
            "expected_arguments": {},
        }
        for number in range(2)
    ]
    code = LLMErrorCode.RATE_LIMIT if status == 429 else LLMErrorCode.UNAVAILABLE
    error = LLMServiceError("private provider detail", code=code, status_code=status)
    service = MagicMock(run=AsyncMock(side_effect=[error, AgentResult(text="Ответ")]))
    context: run_routing_eval.EvaluationContext = {
        "now": "2026-10-28T12:00:00+03:00",
        "timezone": "Europe/Moscow",
    }

    rows, counts = await run_routing_eval._evaluate_cases(
        service,
        cases,
        context,
        "test-model",
        STUDY_MODE,
    )

    assert counts["attempted"] == attempted
    assert rows[0]["status_code"] == status
    assert rows[0]["error_code"] == code
    assert "private provider detail" not in str(rows)
