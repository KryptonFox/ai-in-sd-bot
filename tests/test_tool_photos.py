import base64
import json
import struct
import zlib
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from aiogram.types import BufferedInputFile
from fastmcp.client.client import CallToolResult
from mcp import types
from pydantic import ValidationError

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, AgentResult, EventContext, Replay, TurnState
from app.core.schemas.llm import LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import (
    MAX_AGENT_IMAGES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    PNG_SIGNATURE,
    PhotoToolResult,
    ToolFailure,
    ToolImage,
)
from app.db.repositories import turn_repo
from app.db.repositories.turn_repo import PostgresTurnStore
from app.handlers import dialog
from app.mcp.client import MCPClient
from app.presentation.agent import reply_text
from app.services.agent_service import AgentService
from app.services.dialog_service import DialogService
from app.services.tool_execution import ToolExecutionService
from tests.fake_llm import ScriptedLLM
from tests.fake_mcp import MockToolClient, mock_sdk_client
from tests.mcp_catalog import tool_catalog

SECRET = "photo-test-secret"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def png_with_idat(
    idat: bytes,
    *,
    width: int = 1,
    height: int = 1,
    bit_depth: int = 8,
    color_type: int = 2,
    interlace_method: int = 0,
) -> bytes:
    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        0,
        0,
        interlace_method,
    )
    return (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", idat)
        + png_chunk(b"IEND", b"")
    )


def png_bytes(width: int = 1, height: int = 1, pixel: bytes = b"\xff\x00\x00") -> bytes:
    pixels = b"".join(b"\x00" + pixel * width for _ in range(height))
    return png_with_idat(zlib.compress(pixels), width=width, height=height)


def encoded_png(width: int = 1, height: int = 1) -> str:
    return base64.b64encode(png_bytes(width, height)).decode("ascii")


def image(width: int = 1, height: int = 1) -> ToolImage:
    return ToolImage(data=encoded_png(width, height))


def context() -> EventContext:
    return EventContext(user_id=77, event_id="photo-event", request_id="photo-request")


def tool_call(name: str, arguments: dict[str, object], identifier: str) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=arguments)


def schedule_payload() -> dict[str, object]:
    return {"date": "2026-10-12", "lessons": []}


def reminder_payload() -> dict[str, object]:
    return {
        "id": 12,
        "text": "Проверить расписание",
        "remind_at": "2026-10-12T11:00:00+03:00",
        "timezone": "Europe/Moscow",
        "created": True,
    }


def make_mcp_client() -> MCPClient:
    sdk, _ = mock_sdk_client()
    client = MCPClient(sdk, secret=SECRET)
    client.load_catalog(tool_catalog())
    client.available = True
    return client


def call_result(
    payload: dict[str, object] | None,
    *images: types.ImageContent,
    is_error: bool = False,
) -> CallToolResult:
    content: list[types.ContentBlock] = [
        types.TextContent(type="text", text="payload"),
        *images,
    ]
    return CallToolResult(
        is_error=is_error,
        structured_content=payload,
        content=content,
        meta=None,
    )


def mock_mcp_call(client: MCPClient, result: CallToolResult) -> AsyncMock:
    command = AsyncMock(return_value=result)
    cast(AsyncMock, client._client).call_tool = command
    client.available = True
    return command


def png_content(data: str | None = None, mime_type: str = "image/png") -> types.ImageContent:
    return types.ImageContent(data=data or encoded_png(), mime_type=mime_type)


def make_agent(
    responses: list[LLMResponse],
    call_tool: AsyncMock,
    *,
    max_calls: int = 2,
    confirmations: MagicMock | None = None,
) -> tuple[AgentService, ScriptedLLM, MockToolClient]:
    llm = ScriptedLLM(responses)
    mcp = MockToolClient()
    mcp.load_catalog(tool_catalog())
    mcp.call_tool = call_tool
    service = AgentService(
        llm,
        ToolExecutionService(
            mcp,
            confirmations,
            max_calls=max_calls,
        ),
        clock=FrozenClock(datetime(2026, 10, 12, 9, tzinfo=UTC)),
    )
    return service, llm, mcp


def messages() -> list[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.SYSTEM, content="System policy"),
        ChatMessage(role=MessageRole.USER, content="Построй график"),
    ]


@pytest.mark.parametrize(("width", "height"), [(1, 1), (2, 2), (31, 7)])
def test_tool_image_accepts_valid_png_with_default_mime_type(width: int, height: int) -> None:
    item = ToolImage(data=encoded_png(width, height))

    assert item.mime_type == "image/png"
    assert base64.b64decode(item.data) == png_bytes(width, height)


def test_photo_tool_result_keeps_payload_and_images_separate() -> None:
    result = PhotoToolResult(
        payload={"title": "График", "range": [-10, 10]},
        images=[image()],
    )

    assert result.payload == {"title": "График", "range": [-10, 10]}
    assert result.images == [image()]


@pytest.mark.parametrize(
    "data",
    [
        "",
        "not base64!",
        base64.b64encode(PNG_SIGNATURE).decode("ascii"),
        base64.b64encode(PNG_SIGNATURE + b"\x00" * 64).decode("ascii"),
        base64.b64encode(PNG_SIGNATURE + b"\x00" * MAX_IMAGE_BYTES).decode("ascii"),
        encoded_png()[:40] + "\n" + encoded_png()[40:],
    ],
)
def test_tool_image_rejects_non_png_or_non_strict_base64(data: str) -> None:
    with pytest.raises(ValidationError):
        ToolImage(data=data)


def test_tool_image_rejects_corrupted_png_chunk_checksum() -> None:
    corrupted = bytearray(png_bytes())
    corrupted[45] ^= 0xFF

    with pytest.raises(ValidationError):
        ToolImage(data=base64.b64encode(corrupted).decode("ascii"))


def test_tool_image_rejects_structurally_valid_png_with_malformed_idat() -> None:
    malformed = png_with_idat(b"not a zlib image")

    with pytest.raises(ValidationError):
        ToolImage(data=base64.b64encode(malformed).decode("ascii"))


@pytest.mark.parametrize(
    "idat",
    [
        zlib.compress(b"\x00\xff\x00\x00")[:5],
        zlib.compress(b"\x00\xff\x00"),
        zlib.compress(b"\x00\xff\x00\x00\x00"),
    ],
    ids=["truncated_stream", "short_pixels", "extra_pixels"],
)
def test_tool_image_rejects_truncated_or_mismatched_pixel_data(idat: bytes) -> None:
    malformed = png_with_idat(idat)

    with pytest.raises(ValidationError):
        ToolImage(data=base64.b64encode(malformed).decode("ascii"))


def test_tool_image_rejects_invalid_filter_byte() -> None:
    malformed = png_with_idat(zlib.compress(b"\x05\xff\x00\x00"))

    with pytest.raises(ValidationError):
        ToolImage(data=base64.b64encode(malformed).decode("ascii"))


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (MAX_IMAGE_DIMENSION + 1, 1),
        (5000, 5000),
    ],
)
def test_tool_image_rejects_dimensions_over_decoding_limits(width: int, height: int) -> None:
    oversized = png_with_idat(zlib.compress(b"\x00"), width=width, height=height)

    with pytest.raises(ValidationError):
        ToolImage(data=base64.b64encode(oversized).decode("ascii"))


def test_photo_tool_result_rejects_more_than_agent_image_limit() -> None:
    with pytest.raises(ValidationError):
        PhotoToolResult(
            payload={"title": "График"},
            images=[image() for _ in range(MAX_AGENT_IMAGES + 1)],
        )


def test_agent_result_and_replay_reject_more_than_agent_image_limit() -> None:
    images = [image().model_dump() for _ in range(MAX_AGENT_IMAGES + 1)]

    with pytest.raises(ValidationError):
        AgentResult.model_validate({"text": "Слишком много", "images": images})

    current = AgentResult(text="Два графика", images=[image(), image()])
    assert len(current.images) == MAX_AGENT_IMAGES


def test_tool_image_rejects_unsupported_mime_type() -> None:
    with pytest.raises(ValidationError):
        ToolImage.model_validate({"data": encoded_png(), "mime_type": "image/jpeg"})


def test_agent_result_round_trips_images_and_legacy_records_default_to_empty() -> None:
    current = AgentResult(
        text="График построен.",
        decision=AgentDecision.TOOL,
        tool_name="math_plot",
        images=[image()],
    )
    restored = AgentResult.model_validate_json(current.model_dump_json())
    legacy = AgentResult.model_validate(
        {
            "text": "Старый ответ",
            "decision": "respond",
            "reason": "plain_response",
        }
    )

    assert restored.images == [image()]
    assert legacy.images == []
    assert "images" in json.loads(current.model_dump_json())


@pytest.mark.asyncio
async def test_mcp_read_only_tool_returns_validated_png_separately_from_payload() -> None:
    client = make_mcp_client()
    mock_mcp_call(client, call_result(schedule_payload(), png_content()))

    actual = await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert isinstance(actual, PhotoToolResult)
    assert actual.payload == schedule_payload()
    assert actual.images == [image()]


@pytest.mark.asyncio
async def test_mcp_text_only_result_stays_plain_dictionary() -> None:
    client = make_mcp_client()
    mock_mcp_call(client, call_result(schedule_payload()))

    actual = await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert actual == schedule_payload()


@pytest.mark.asyncio
async def test_mcp_rejects_unsupported_image_mime_type() -> None:
    client = make_mcp_client()
    mock_mcp_call(
        client,
        call_result(schedule_payload(), png_content(mime_type="image/jpeg")),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not base64",
        base64.b64encode(PNG_SIGNATURE).decode("ascii"),
        base64.b64encode(PNG_SIGNATURE + b"\x00" * MAX_IMAGE_BYTES).decode("ascii"),
        base64.b64encode(png_with_idat(b"not a zlib image")).decode("ascii"),
        base64.b64encode(png_with_idat(zlib.compress(b"\x00\xff\x00"))).decode("ascii"),
    ],
    ids=["invalid_base64", "signature_only", "oversized", "malformed_idat", "short_pixels"],
)
async def test_mcp_rejects_malformed_or_oversized_image(payload: str) -> None:
    client = make_mcp_client()
    mock_mcp_call(
        client,
        call_result(schedule_payload(), png_content(data=payload)),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_mcp_stops_extracting_images_when_limit_is_exceeded() -> None:
    client = make_mcp_client()
    mock_mcp_call(
        client,
        call_result(
            schedule_payload(),
            png_content(),
            png_content(),
            png_content(data="not base64"),
        ),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"
    assert "слишком много" in raised.value.message


@pytest.mark.asyncio
async def test_mcp_validates_structured_payload_before_attaching_images() -> None:
    client = make_mcp_client()
    mock_mcp_call(client, call_result({"date": "2026-10-12", "lessons": "broken"}, png_content()))

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_mcp_rejects_images_from_mutating_tool() -> None:
    client = make_mcp_client()
    mock_mcp_call(client, call_result(reminder_payload(), png_content()))

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool(
            "add_reminder",
            {"text": "Проверить", "remind_at": "2026-10-12T11:00:00+03:00"},
            user_id=7,
            confirmation_id="confirmation",
        )

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_fake_llm_receives_only_structured_payload_and_result_carries_photo() -> None:
    expected_image = image()
    service, llm, mcp = make_agent(
        [
            LLMResponse(
                content="",
                tool_calls=[tool_call("get_schedule", {"date": "2026-10-12"}, "call-1")],
            ),
            LLMResponse(content="График построен."),
        ],
        AsyncMock(
            return_value=PhotoToolResult(payload=schedule_payload(), images=[expected_image])
        ),
    )

    result = await service.run(messages(), context())

    assert result.text == "График построен."
    assert result.decision == AgentDecision.TOOL
    assert result.images == [expected_image]
    assert len(llm.requests) == 2
    for request in llm.requests:
        for message in request.messages:
            assert expected_image.data not in message.content
            assert "iVBORw0KGgo" not in message.content

    tool_messages = [
        message for message in llm.requests[1].messages if message.role == MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0].content) == {
        "source": "get_schedule",
        "data": schedule_payload(),
    }
    mcp.call_tool.assert_awaited_once_with("get_schedule", {"date": "2026-10-12"})


@pytest.mark.asyncio
async def test_photo_is_preserved_when_later_batch_call_exceeds_aggregate_budget() -> None:
    first = image()
    second = image(width=2)
    third = image(width=3)
    service, llm, _ = make_agent(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    tool_call("get_schedule", {"date": "2026-10-12"}, "call-1"),
                    tool_call("get_schedule", {"date": "2026-10-13"}, "call-2"),
                ],
            )
        ],
        AsyncMock(
            side_effect=[
                PhotoToolResult(payload=schedule_payload(), images=[first, second]),
                PhotoToolResult(payload=schedule_payload(), images=[third]),
            ]
        ),
    )

    result = await service.run(messages(), context())

    assert result.decision == AgentDecision.ERROR
    assert result.reason == "tool_output_budget"
    assert result.images == [first, second]
    assert len(llm.requests) == 1


@pytest.mark.asyncio
async def test_photo_is_preserved_in_confirmation_result_after_read_call() -> None:
    expected_image = image()
    confirmations = MagicMock(
        prepare=AsyncMock(
            return_value=AgentResult(
                text="Подтвердите напоминание",
                decision=AgentDecision.PREPARE,
                confirmation_id="confirmation",
            )
        )
    )
    service, llm, _ = make_agent(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    tool_call("get_schedule", {"date": "2026-10-12"}, "call-1"),
                    tool_call(
                        "add_reminder",
                        {"text": "Проверить", "remind_at": "2026-10-12T11:00:00+03:00"},
                        "call-2",
                    ),
                ],
            )
        ],
        AsyncMock(
            return_value=PhotoToolResult(payload=schedule_payload(), images=[expected_image])
        ),
        confirmations=confirmations,
    )

    result = await service.run(messages(), context(), timezone="Europe/Moscow")

    assert result.decision == AgentDecision.PREPARE
    assert result.images == [expected_image]
    assert len(llm.requests) == 1


@pytest.mark.asyncio
async def test_text_only_tool_result_leaves_images_empty() -> None:
    service, _, _ = make_agent(
        [
            LLMResponse(
                content="",
                tool_calls=[tool_call("get_schedule", {"date": "2026-10-12"}, "call-1")],
            ),
            LLMResponse(content="Занятий нет."),
        ],
        AsyncMock(return_value=schedule_payload()),
    )

    result = await service.run(messages(), context())

    assert result.text == "Занятий нет."
    assert result.images == []


class _NullContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _RecordingConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self) -> "_RecordingConnection":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def transaction(self) -> _NullContext:
        return _NullContext()

    async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
        return {"state": TurnState.PROCESSING}

    async def execute(self, query: str, *args: object) -> None:
        self.executions.append((query, args))


class _RecordingPool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> _RecordingConnection:
        return self.connection


@pytest.mark.asyncio
async def test_turn_persistence_replays_bounded_images_but_audit_and_history_stay_textual(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected_image = image()
    connection = _RecordingConnection()
    pool = _RecordingPool(connection)
    store = PostgresTurnStore(cast(asyncpg.Pool, pool))
    result = AgentResult(
        text="График построен.",
        decision=AgentDecision.TOOL,
        tool_name="math_plot",
        arguments={"functions": ["1/x"]},
        reason="plain_response",
        images=[expected_image],
    )
    assistant = ChatMessage(role=MessageRole.ASSISTANT, content=reply_text(result))
    audit = AsyncMock()
    monkeypatch.setattr(turn_repo, "record_on_connection", audit)

    await store.complete(
        context(),
        ChatMessage(role=MessageRole.USER, content="Построй график"),
        assistant,
        result,
        datetime(2026, 10, 12, 9, tzinfo=UTC),
        15.0,
    )

    assert expected_image.data not in assistant.content
    assert audit.await_args is not None
    assert expected_image.data not in json.dumps(audit.await_args.kwargs["arguments"])
    assert expected_image.data not in caplog.text
    update_query, update_args = connection.executions[-1]
    assert "UPDATE agent_events" in update_query
    persisted = AgentResult.model_validate_json(cast(str, update_args[2]))
    assert persisted.images == [expected_image]
    legacy_json = dict(json.loads(cast(str, update_args[2])))
    legacy_json.pop("images")
    assert AgentResult.model_validate(legacy_json).images == []


@pytest.mark.asyncio
async def test_dialog_replay_returns_persisted_photos_without_new_llm_or_tool_call() -> None:
    expected_image = image()
    store = MagicMock()
    store.claim = AsyncMock(
        return_value=Replay(
            AgentResult(
                text="График построен.",
                decision=AgentDecision.TOOL,
                tool_name="math_plot",
                images=[expected_image],
            )
        )
    )
    llm = MagicMock()
    service = DialogService(
        cast(asyncpg.Pool, MagicMock()),
        Settings(bot_token="unused", postgres_password="unused"),
        llm,
        store=store,
    )

    result = await service.process_user_message(1, "Построй график", event_id="replay")

    assert isinstance(result, AgentResult)
    assert result.images == [expected_image]
    llm.generate.assert_not_called()
    store.claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmation_replay_keeps_photos_from_persisted_turn_result() -> None:
    expected_image = image()
    store = MagicMock()
    store.claim = AsyncMock(
        return_value=Replay(
            AgentResult(
                text="Подтвердите напоминание",
                decision=AgentDecision.PREPARE,
                confirmation_id="confirmation",
                images=[expected_image],
            )
        )
    )
    confirmations = MagicMock()
    confirmations.existing = AsyncMock(
        return_value=AgentResult(
            text="Подтвердите напоминание",
            decision=AgentDecision.PREPARE,
            confirmation_id="confirmation",
        )
    )
    service = DialogService(
        cast(asyncpg.Pool, MagicMock()),
        Settings(bot_token="unused", postgres_password="unused"),
        MagicMock(),
        store=store,
        confirmations=confirmations,
    )

    result = await service.process_user_message(1, "Напомни", event_id="replay-confirmation")

    assert isinstance(result, AgentResult)
    assert result.confirmation_id == "confirmation"
    assert result.images == [expected_image]
    confirmations.existing.assert_awaited_once()


@pytest.fixture
def handler_message() -> MagicMock:
    message = MagicMock()
    message.bot = None
    message.chat.id = 42
    message.answer = AsyncMock()
    return message


@pytest.fixture
def handler_service() -> MagicMock:
    service = MagicMock()
    service.is_busy = AsyncMock(return_value=False)
    return service


@pytest.mark.asyncio
async def test_handler_sends_formatted_answer_before_photo(
    monkeypatch: pytest.MonkeyPatch,
    handler_message: MagicMock,
    handler_service: MagicMock,
) -> None:
    events: list[str] = []
    expected_image = image()
    result = AgentResult(text="График построен.", images=[expected_image])
    handler_service.process_user_message = AsyncMock(return_value=result)
    send = AsyncMock(side_effect=lambda *args, **kwargs: events.append("text"))
    handler_message.answer_photo = AsyncMock(
        side_effect=lambda *args, **kwargs: events.append("photo")
    )
    monkeypatch.setattr(dialog, "send_formatted_message", send)

    await dialog._execute_dialog(handler_message, handler_service, 42, "Построй график")

    assert events == ["text", "photo"]
    assert send.await_args is not None
    assert send.await_args.args[1] == "График построен."
    assert handler_message.answer_photo.await_args is not None
    uploaded = handler_message.answer_photo.await_args.args[0]
    assert isinstance(uploaded, BufferedInputFile)
    assert uploaded.data == png_bytes()
    assert uploaded.filename == "result-1.png"


@pytest.mark.asyncio
async def test_handler_sends_all_photos_in_result_order(
    monkeypatch: pytest.MonkeyPatch,
    handler_message: MagicMock,
    handler_service: MagicMock,
) -> None:
    events: list[str] = []
    first = image()
    second = image(width=2)
    result = AgentResult(text="Два графика.", images=[first, second])
    handler_service.process_user_message = AsyncMock(return_value=result)
    send = AsyncMock(side_effect=lambda *args, **kwargs: events.append("text"))
    handler_message.answer_photo = AsyncMock(
        side_effect=lambda *args, **kwargs: events.append("photo")
    )
    monkeypatch.setattr(dialog, "send_formatted_message", send)

    await dialog._execute_dialog(handler_message, handler_service, 42, "Построй графики")

    assert events == ["text", "photo", "photo"]
    uploads = [call.args[0] for call in handler_message.answer_photo.await_args_list]
    assert [upload.data for upload in uploads] == [
        base64.b64decode(first.data),
        base64.b64decode(second.data),
    ]
    assert [upload.filename for upload in uploads] == ["result-1.png", "result-2.png"]


@pytest.mark.asyncio
async def test_handler_reports_photo_failure_as_text_without_logging_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    handler_message: MagicMock,
    handler_service: MagicMock,
) -> None:
    events: list[str] = []
    expected_image = image()
    result = AgentResult(text="График построен.", images=[expected_image])
    handler_service.process_user_message = AsyncMock(return_value=result)
    send = AsyncMock(side_effect=lambda *args, **kwargs: events.append("notice"))
    handler_message.answer_photo = AsyncMock(side_effect=RuntimeError(expected_image.data))
    monkeypatch.setattr(dialog, "send_formatted_message", send)

    await dialog._execute_dialog(handler_message, handler_service, 42, "Построй график")

    assert events == ["notice", "notice"]
    assert send.await_args is not None
    assert "фото" in send.await_args_list[1].args[1]
    assert expected_image.data not in caplog.text


@pytest.mark.asyncio
async def test_handler_does_not_send_photo_for_text_only_result(
    monkeypatch: pytest.MonkeyPatch,
    handler_message: MagicMock,
    handler_service: MagicMock,
) -> None:
    handler_service.process_user_message = AsyncMock(
        return_value=AgentResult(text="Текстовый ответ.")
    )
    monkeypatch.setattr(dialog, "send_formatted_message", AsyncMock())

    await dialog._execute_dialog(handler_message, handler_service, 42, "Вопрос")

    handler_message.answer_photo.assert_not_called()
