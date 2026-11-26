import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from app.config import DEFAULT_ROUTER_BASE_URL, Settings
from app.core.clock import SystemClock
from app.core.trace import OperationTrace
from app.health import HealthState


def feature_settings() -> Settings:
    return Settings(
        bot_token="unused",
        postgres_password="unused",
    )


def test_legacy_tool_client_keeps_lab2_surface_and_trusted_reminder() -> None:
    from app import __main__ as application
    from app.core.schemas.llm import ToolDefinition
    from app.core.schemas.mcp import ResourceDefinition
    from app.mcp.restricted import RestrictedToolClient

    client = Mock(available=True)
    client.tools = {
        name: ToolDefinition(name=name)
        for name in application.LEGACY_TOOL_NAMES | {"search_materials", "search_web"}
    }
    client.resources = {
        uri: ResourceDefinition(uri=uri, name=uri)
        for uri in application.LEGACY_RESOURCE_URIS | {"materials://catalog"}
    }

    legacy = application._create_legacy_tool_client(client)

    assert set(legacy.tools) == application.LEGACY_TOOL_NAMES
    assert "add_reminder" in legacy.tools
    assert "search_materials" not in legacy.tools
    assert "search_web" not in legacy.tools
    assert set(legacy.resources) == application.LEGACY_RESOURCE_URIS

    strict = RestrictedToolClient(
        client,
        application.LEGACY_TOOL_NAMES,
        application.LEGACY_RESOURCE_URIS,
    )
    assert "add_reminder" not in strict.tools


async def test_router_supervisor_is_skipped_without_effective_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import __main__ as application

    router_factory = Mock()
    monkeypatch.setattr(application, "DecisionsRouter", router_factory)
    caplog.set_level(logging.WARNING, logger="app")

    decisions_router, supervisor = await application._create_router_supervisor(
        feature_settings(),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
    )

    assert decisions_router is None
    assert supervisor is None
    router_factory.assert_not_called()
    assert "router_disabled" in caplog.text
    assert "api_key_missing" in caplog.text


async def test_router_supervisor_uses_fallback_key_and_builds_specialists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import __main__ as application

    settings = replace(
        feature_settings(),
        llm_api_key="fallback-key",
        llm_base_url=DEFAULT_ROUTER_BASE_URL,
        router_base_url=DEFAULT_ROUTER_BASE_URL,
    )
    llm = Mock()
    client = Mock()
    store = Mock()
    confirmations = Mock()
    agents = Mock()
    decisions_router = Mock(aclose=AsyncMock())
    supervisor = Mock()
    router_factory = Mock(return_value=decisions_router)
    specialists_factory = Mock(return_value=agents)
    supervisor_factory = Mock(return_value=supervisor)
    monkeypatch.setattr(application, "DecisionsRouter", router_factory)
    monkeypatch.setattr(application, "build_specialists", specialists_factory)
    monkeypatch.setattr(application, "Supervisor", supervisor_factory)

    result_router, result_supervisor = await application._create_router_supervisor(
        settings,
        llm,
        client,
        store,
        confirmations,
    )

    assert result_router is decisions_router
    assert result_supervisor is supervisor
    router_factory.assert_called_once_with(settings)
    specialists_factory.assert_called_once_with(settings, llm, client, store, confirmations)
    supervisor_factory.assert_called_once_with(decisions_router, agents, settings.router_model)


async def test_router_supervisor_closes_router_when_specialist_wiring_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import __main__ as application

    settings = replace(
        feature_settings(),
        router_api_key="router-key",
        router_base_url=DEFAULT_ROUTER_BASE_URL,
    )
    decisions_router = Mock(aclose=AsyncMock())
    monkeypatch.setattr(
        application,
        "DecisionsRouter",
        Mock(return_value=decisions_router),
    )
    monkeypatch.setattr(
        application,
        "build_specialists",
        Mock(side_effect=RuntimeError("specialist profile failed")),
    )

    with pytest.raises(RuntimeError, match="specialist profile failed"):
        await application._create_router_supervisor(
            settings,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
        )

    decisions_router.aclose.assert_awaited_once()


async def test_notification_service_shares_http_and_saves_background_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import __main__ as application

    pool = Mock()
    student = Mock()
    bot = Mock()
    http = Mock()
    clock = Mock()
    weather = Mock()
    briefing = Mock()
    repository = Mock()
    delivery = Mock()
    resolver = Mock()
    service = Mock()
    weather_factory = Mock(return_value=weather)
    briefing_factory = Mock(return_value=briefing)
    repository_factory = Mock(return_value=repository)
    delivery_factory = Mock(return_value=delivery)
    resolver_factory = Mock(return_value=resolver)
    service_factory = Mock(return_value=service)
    save_trace = AsyncMock()
    monkeypatch.setattr(application, "WeatherService", weather_factory)
    monkeypatch.setattr(application, "BriefingSources", briefing_factory)
    monkeypatch.setattr(application, "PostgresNotificationRepository", repository_factory)
    monkeypatch.setattr(application, "TelegramBotDelivery", delivery_factory)
    monkeypatch.setattr(application, "HttpCityResolver", resolver_factory)
    monkeypatch.setattr(application, "NotificationService", service_factory)
    monkeypatch.setattr(application, "save_trace", save_trace)

    result = application._create_notification_service(pool, student, bot, http, clock)

    assert result is service
    weather_factory.assert_called_once_with(http, clock=clock)
    briefing_factory.assert_called_once_with(pool, student, weather)
    repository_factory.assert_called_once_with(pool)
    delivery_factory.assert_called_once_with(bot)
    resolver_factory.assert_called_once_with(http)
    service_factory.assert_called_once_with(
        repository,
        clock=clock,
        deliver=delivery,
        briefing=briefing,
        city_resolver=resolver,
        trace_sink=ANY,
    )

    trace = OperationTrace(42, "request", datetime(2031, 5, 1, tzinfo=UTC))
    trace_sink = service_factory.call_args.kwargs["trace_sink"]
    await trace_sink(trace)

    save_trace.assert_awaited_once_with(pool, trace)


async def test_run_wires_feature_routers_middleware_di_and_background_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import __main__ as application
    from app.handlers import router as main_router
    from app.mcp.connection import ToolClients

    pool = Mock(close=AsyncMock())
    student = Mock(available=True)
    composite = Mock(
        start=AsyncMock(return_value=True),
        recover=AsyncMock(),
        aclose=AsyncMock(),
        available=True,
    )
    clients = ToolClients(student=student, math=None, composite=composite)
    llm = Mock(aclose=AsyncMock())
    confirmations = Mock(recover=AsyncMock())
    tools = Mock(available=True)
    agent = Mock()
    store = Mock(recover=AsyncMock())
    supervisor = Mock()
    dialog = Mock(recover=AsyncMock())
    notification_service = Mock(run=AsyncMock(return_value=None))
    calendar_service = Mock()
    middleware = Mock()
    http = Mock(aclose=AsyncMock())
    runner = Mock(cleanup=AsyncMock())
    bot = Mock(
        get_me=AsyncMock(return_value=Mock(username="test")),
        get_webhook_info=AsyncMock(return_value=Mock(url="")),
        session=Mock(close=AsyncMock()),
    )
    dispatcher = Mock(
        resolve_used_update_types=Mock(return_value=[]),
    )
    dispatcher.message = Mock(outer_middleware=Mock())

    async def stop_after_background_start(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("stop after wiring")

    dispatcher.start_polling = AsyncMock(side_effect=stop_after_background_start)

    notification_builder = Mock(return_value=notification_service)
    calendar_factory = Mock(return_value=calendar_service)
    middleware_factory = Mock(return_value=middleware)
    specialist_builder = AsyncMock(return_value=(None, supervisor))
    purge_traces = AsyncMock(return_value=None)
    legacy_client = Mock()
    restricted_factory = Mock(return_value=legacy_client)
    tools_factory = Mock(return_value=tools)
    monkeypatch.setattr(application, "create_tool_clients", Mock(return_value=clients))
    monkeypatch.setattr(application, "OpenAIClient", Mock(return_value=llm))
    monkeypatch.setattr(application, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))
    monkeypatch.setattr(application, "ConfirmationService", Mock(return_value=confirmations))
    monkeypatch.setattr(application, "RestrictedToolClient", restricted_factory)
    monkeypatch.setattr(application, "ToolExecutionService", tools_factory)
    monkeypatch.setattr(application, "AgentService", Mock(return_value=agent))
    monkeypatch.setattr(application, "PostgresTurnStore", Mock(return_value=store))
    monkeypatch.setattr(
        application,
        "_create_router_supervisor",
        specialist_builder,
    )
    monkeypatch.setattr(application, "DialogService", Mock(return_value=dialog))
    monkeypatch.setattr(application, "_create_notification_service", notification_builder)
    monkeypatch.setattr(application, "CalendarImportService", calendar_factory)
    monkeypatch.setattr(application, "NotificationChatMiddleware", middleware_factory)
    monkeypatch.setattr(application, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(application.httpx, "AsyncClient", Mock(return_value=http))
    monkeypatch.setattr(application, "_purge_traces_periodically", purge_traces)
    monkeypatch.setattr(application, "setup_bot_commands", AsyncMock())
    monkeypatch.setattr(application, "setup_bot_description", AsyncMock())
    monkeypatch.setattr(
        application,
        "start_health_server",
        AsyncMock(return_value=runner),
    )

    with pytest.raises(RuntimeError, match="stop after wiring"):
        await application.run(feature_settings())

    notification_call = notification_builder.call_args
    assert notification_call.args[0] is pool
    assert notification_call.args[1] is student
    assert notification_call.args[2] is bot
    assert notification_call.args[3] is http
    assert isinstance(notification_call.args[4], SystemClock)
    restricted_factory.assert_called_once_with(
        composite,
        application.LEGACY_TOOL_NAMES,
        application.LEGACY_RESOURCE_URIS,
        trusted_reminder=True,
    )
    assert tools_factory.call_args.args[0] is legacy_client
    calendar_factory.assert_called_once_with(pool, notification_call.args[4])
    middleware_factory.assert_called_once_with(notification_service)
    dispatcher.message.outer_middleware.assert_called_once_with(middleware)
    dispatcher.include_router.assert_called_once_with(main_router)

    polling = dispatcher.start_polling.call_args
    assert polling.args == (bot,)
    assert polling.kwargs["db"] is pool
    assert polling.kwargs["dialog_service"] is dialog
    assert polling.kwargs["notification_service"] is notification_service
    assert polling.kwargs["calendar_import_service"] is calendar_service
    assert polling.kwargs["allowed_updates"] == []
    assert polling.kwargs["close_bot_session"] is False
    notification_service.run.assert_awaited_once()
    purge_traces.assert_awaited_once()


async def test_shutdown_closes_router_http_and_cancels_background_tasks() -> None:
    from app.__main__ import _shutdown

    closed: list[str] = []

    def close_action(name: str):
        async def close() -> None:
            closed.append(name)

        return AsyncMock(side_effect=close)

    pool = Mock(close=close_action("database"))
    bot = Mock(session=Mock(close=close_action("bot")))
    llm = Mock(aclose=close_action("llm"))
    runner = Mock(cleanup=close_action("health"))
    mcp = Mock(aclose=close_action("mcp"))
    decisions_router = Mock(aclose=close_action("router"))
    http = Mock(aclose=close_action("http"))

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    notification_task = asyncio.create_task(wait_forever())
    trace_task = asyncio.create_task(wait_forever())
    state = HealthState(pool=pool, initialized=True)

    await _shutdown(
        state,
        bot,
        llm,
        mcp,
        runner,
        None,
        decisions_router=decisions_router,
        http=http,
        notification_task=notification_task,
        trace_task=trace_task,
    )

    assert closed == ["mcp", "health", "http", "router", "llm", "bot", "database"]
    assert notification_task.cancelled()
    assert trace_task.cancelled()
    assert not state.initialized


def test_feature_routers_are_registered_before_dialog() -> None:
    from app.handlers import router

    names = [sub_router.name for sub_router in router.sub_routers]
    dialog_index = names.index("dialog")

    assert names.index("calendar_import") < dialog_index
    assert names.index("notifications") < dialog_index
    assert names.index("trace") < dialog_index
