from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.core.exceptions import LLMErrorCode, LLMServiceError, LLMTimeoutError
from app.core.schemas.agent import EventContext
from app.handlers import dialog
from app.services import agent_service, dialog_service
from app.services.agent_service import AgentService
from app.services.dialog_service import DialogService


@pytest.mark.parametrize(
    ("code", "explanation"),
    [
        (LLMErrorCode.AUTHENTICATION, "отклонил API-ключ"),
        (LLMErrorCode.CONNECTION, "Не удалось соединиться"),
        (LLMErrorCode.RATE_LIMIT, "ограничил запросы или исчерпана квота"),
        (LLMErrorCode.INVALID_RESPONSE, "неподдерживаемом формате"),
        (LLMErrorCode.GENERATION_LIMIT, "исчерпала лимит генерации"),
    ],
)
async def test_service_error_explains_reason_without_exposing_exception(
    code: LLMErrorCode,
    explanation: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = MagicMock()
    message.bot = None
    service = MagicMock()
    service.queue_manager = None
    service.process_user_message = AsyncMock(
        side_effect=LLMServiceError("PRIVATE ERROR secret-key", code=code, status_code=401)
    )
    send = AsyncMock()
    monkeypatch.setattr(dialog, "send_formatted_message", send)

    await dialog._execute_dialog(message, service, 42, "PRIVATE PROMPT")

    assert send.await_args is not None
    response = send.await_args.args[1]
    assert explanation in response
    assert f"code={code}" in caplog.text
    assert "status=401" in caplog.text
    assert explanation in caplog.text
    for private in ("PRIVATE ERROR", "secret-key", "PRIVATE PROMPT"):
        assert private not in caplog.text + response


async def test_handler_llm_error_log_includes_safe_api_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = MagicMock()
    message.bot = None
    service = MagicMock()
    service.queue_manager = None
    diagnosis = "provider_reason=API отклонил temperature: параметр не поддерживается моделью"
    service.process_user_message = AsyncMock(
        side_effect=LLMServiceError(
            "PRIVATE ERROR",
            code=LLMErrorCode.INVALID_REQUEST,
            status_code=400,
            diagnostic_details=diagnosis,
        )
    )
    monkeypatch.setattr(dialog, "send_formatted_message", AsyncMock())

    await dialog._execute_dialog(message, service, 42, "PRIVATE PROMPT")

    assert "llm_error code=llm_invalid_request status=400" in caplog.text
    assert diagnosis in caplog.text
    assert "PRIVATE ERROR" not in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text


async def test_agent_preserves_failure_reason_and_releases_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = LLMTimeoutError("PRIVATE ERROR")
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=error)
    mcp = MagicMock()
    mcp.available = False
    mcp.tools = {}
    pool = MagicMock()
    service = AgentService(llm, mcp, audit_pool=pool)
    context = EventContext(user_id=42, event_id="event", request_id="request")
    claim = AsyncMock(return_value={"claimed": True, "calls_used": 0})
    fail = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(agent_service.event_repo, "begin", claim)
    monkeypatch.setattr(agent_service.event_repo, "fail", fail)
    monkeypatch.setattr(agent_service, "record_decision", record)

    with pytest.raises(LLMTimeoutError) as caught:
        await service.run([], context)

    assert caught.value is error
    fail.assert_awaited_once()
    assert record.await_args is not None
    assert record.await_args.kwargs["reason"] == "llm_timeout"
    assert record.await_args.kwargs["validation"] == "llm_timeout"
    assert "PRIVATE ERROR" not in str(record.await_args)


@pytest.mark.parametrize("reason", ["llm_error", "llm_connection"])
async def test_why_explains_current_and_legacy_llm_failure_codes(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = AsyncMock(
        return_value={
            "created_at": datetime(2026, 10, 26, tzinfo=UTC),
            "decision": "error",
            "tool_name": None,
            "arguments": {},
            "validation": reason,
            "execution": "failed",
            "duration_ms": 0.0,
            "reason": reason,
        }
    )
    monkeypatch.setattr(dialog_service, "latest_decision", latest)
    service = DialogService(
        MagicMock(),
        Settings(bot_token="unused", postgres_password="unused"),
        MagicMock(),
    )

    response = await service.why(42)

    assert reason not in response
    assert "языковой модели" in response
    assert "Повторите запрос позже" in response
