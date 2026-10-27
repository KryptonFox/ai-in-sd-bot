from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.core.budget import ContextBudget
from app.core.exceptions import LLMErrorCode, LLMServiceError, LLMTimeoutError
from app.core.schemas.agent import Acquired
from app.core.schemas.user import UserSettings
from app.handlers import dialog
from app.presentation.agent import decision_text
from app.services import dialog_service
from app.services.context_builder import ContextBuilder, PreparedContext
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
    service.is_busy = AsyncMock(return_value=False)
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
    service.is_busy = AsyncMock(return_value=False)
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
    llm = MagicMock(generate=AsyncMock(side_effect=error))
    store = MagicMock(
        claim=AsyncMock(return_value=Acquired()), interrupt=AsyncMock(return_value=False)
    )
    service = DialogService(
        MagicMock(), Settings(bot_token="unused", postgres_password="unused"), llm, store=store
    )
    monkeypatch.setattr(
        dialog_service, "get_or_create_user", AsyncMock(return_value=UserSettings(user_id=42))
    )
    monkeypatch.setattr(dialog_service, "get_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ContextBuilder, "build", AsyncMock(return_value=PreparedContext([], ContextBudget(32768)))
    )

    with pytest.raises(LLMTimeoutError) as caught:
        await service.process_user_message(42, "question", event_id="event")

    assert caught.value is error
    store.interrupt.assert_awaited_once()
    assert store.interrupt.await_args.args[1] == "llm_timeout"
    assert "PRIVATE ERROR" not in str(store.interrupt.await_args)


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

    response = decision_text(await service.why(42))

    assert reason not in response
    assert "языковой модели" in response
    assert "Повторите запрос позже" in response
