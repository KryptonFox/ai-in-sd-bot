from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types

from app.config import Settings
from app.core.exceptions import LLMErrorCode, LLMServiceError
from app.core.schemas.agent import AgentDecision, AgentResult
from app.core.schemas.execution import RunState
from app.core.schemas.llm import LLMResponse
from app.core.schemas.message import ToolCall
from app.core.schemas.tools import ToolFailure
from app.mcp.connection import create_student_tools_client
from app.modes.definitions import STUDY_MODE
from app.services.tool_execution import READ_RESOURCE_FUNCTION
from scripts import run_lab1_experiment, run_routing_eval
from tests.mcp_catalog import resource_catalog, tool_catalog


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
    assert all("dry_run" not in call.kwargs for call in service.run.await_args_list)


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


async def test_routing_evaluation_records_resource_uri_as_its_own_action() -> None:
    case: run_routing_eval.RoutingCase = {
        "id": "schedule",
        "query": "Покажи расписание на эту неделю",
        "expected_action": "resource",
        "expected_arguments": {"uri": "schedule://current-week"},
    }
    score = run_routing_eval.score_case(
        case,
        "resource",
        {},
        resource_uri="schedule://current-week",
    )

    assert score == {
        "route_correct": True,
        "arguments_applicable": True,
        "arguments_correct": True,
        "success": True,
    }


async def test_evaluation_executor_records_reminder_without_confirmation() -> None:
    from app.core.clock import FrozenClock
    from app.core.schemas.message import ToolCall

    clock = FrozenClock(run_routing_eval.datetime.fromisoformat("2026-10-12T09:00:00+03:00"))
    executor = run_routing_eval.RecordingToolExecutionService(MagicMock(), clock)
    state = RunState(
        context=run_routing_eval.EventContext(
            user_id=1,
            event_id="eval:reminder",
            request_id="eval:reminder",
        ),
        conversation=[],
        timezone="Europe/Moscow",
    )
    call = ToolCall(
        id="call-1",
        name="add_reminder",
        arguments={
            "text": "отчёт",
            "remind_at": "2026-10-13T18:30:00+03:00",
        },
    )

    with pytest.raises(run_routing_eval.SelectedAction) as captured:
        await executor.execute(state, [call])

    assert captured.value.action == "add_reminder"
    assert captured.value.arguments == {
        "text": "отчёт",
        "remind_at": "2026-10-13T18:30:00+03:00",
    }
    assert captured.value.resource_uri is None


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_code"),
    [
        (
            "get_weather",
            {"city": "Казань", "private_path": "/tmp/private"},
            "invalid_arguments",
        ),
        ("delete_everything", {"city": "Казань"}, "unsupported_action"),
    ],
)
async def test_recording_executor_validates_before_capturing_or_dispatching(
    tool_name: str,
    arguments: dict[str, object],
    expected_code: str,
) -> None:
    mcp = create_student_tools_client(Settings(bot_token="unused", postgres_password="unused"))
    catalog = tool_catalog()
    if tool_name == "delete_everything":
        mutation = catalog[0].model_copy(
            update={
                "name": tool_name,
                "annotations": types.ToolAnnotations(read_only_hint=False),
            }
        )
        catalog.append(mutation)
    mcp.load_catalog(catalog)
    mcp.load_resources(resource_catalog())
    mcp.call_tool = AsyncMock()
    clock = run_routing_eval.FrozenClock(
        run_routing_eval.datetime.fromisoformat("2026-10-12T09:00:00+03:00")
    )
    executor = run_routing_eval.RecordingToolExecutionService(mcp, clock)
    state = RunState(
        context=run_routing_eval.EventContext(
            user_id=1,
            event_id="eval:invalid-action",
            request_id="eval:invalid-action",
        ),
        conversation=[],
        timezone="Europe/Moscow",
    )

    with pytest.raises(ToolFailure) as captured:
        await executor.execute(
            state,
            [ToolCall(id="call-1", name=tool_name, arguments=arguments)],
        )

    assert captured.value.code == expected_code
    assert not isinstance(captured.value, run_routing_eval.SelectedAction)
    assert not run_routing_eval._is_availability_failure(captured.value)
    mcp.call_tool.assert_not_awaited()


async def test_recording_executor_captures_resource_tool_call_without_rpc() -> None:
    mcp = create_student_tools_client(Settings(bot_token="unused", postgres_password="unused"))
    mcp.load_catalog(tool_catalog())
    mcp.load_resources(resource_catalog())
    mcp.call_tool = AsyncMock()
    executor = run_routing_eval.RecordingToolExecutionService(
        mcp,
        run_routing_eval.FrozenClock(
            run_routing_eval.datetime.fromisoformat("2026-10-12T09:00:00+03:00")
        ),
    )
    state = RunState(
        context=run_routing_eval.EventContext(
            user_id=1,
            event_id="eval:unknown-resource",
            request_id="eval:unknown-resource",
        ),
        conversation=[],
        timezone="Europe/Moscow",
    )

    with pytest.raises(run_routing_eval.SelectedAction) as captured:
        await executor.execute(
            state,
            [
                ToolCall(
                    id="resource",
                    name=READ_RESOURCE_FUNCTION,
                    arguments={"uri": "schedule://current-week"},
                )
            ],
        )

    assert captured.value.action == "resource"
    assert captured.value.resource_uri == "schedule://current-week"
    mcp.call_tool.assert_not_awaited()


async def test_recording_executor_rejects_resource_outside_discovered_catalog() -> None:
    mcp = create_student_tools_client(Settings(bot_token="unused", postgres_password="unused"))
    mcp.load_catalog(tool_catalog())
    mcp.load_resources(resource_catalog())
    mcp.call_tool = AsyncMock()
    executor = run_routing_eval.RecordingToolExecutionService(
        mcp,
        run_routing_eval.FrozenClock(
            run_routing_eval.datetime.fromisoformat("2026-10-12T09:00:00+03:00")
        ),
    )
    state = RunState(
        context=run_routing_eval.EventContext(
            user_id=1,
            event_id="eval:unknown-resource",
            request_id="eval:unknown-resource",
        ),
        conversation=[],
        timezone="Europe/Moscow",
    )

    with pytest.raises(ToolFailure, match="Ресурс не обнаружен"):
        await executor.execute(
            state,
            [
                ToolCall(
                    id="resource",
                    name=READ_RESOURCE_FUNCTION,
                    arguments={"uri": "schedule://not-discovered"},
                )
            ],
        )

    mcp.call_tool.assert_not_awaited()


def test_routing_revision_hash_includes_mcp_server_sources(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_source = tmp_path / "app" / "service.py"
    server_source = tmp_path / "mcp_servers" / "student_tools" / "server.py"
    app_source.parent.mkdir(parents=True)
    server_source.parent.mkdir(parents=True)
    app_source.write_text("app", encoding="utf-8")
    server_source.write_text("server", encoding="utf-8")
    monkeypatch.setattr(run_routing_eval, "ROOT", tmp_path)

    first_hash = run_routing_eval._app_python_sha256()
    server_source.write_text("changed server", encoding="utf-8")

    assert run_routing_eval._app_python_sha256() != first_hash


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
