from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from app.config import Settings
from app.core.exceptions import InvalidTemperatureError, LLMTimeoutError
from app.core.schemas.llm import LLMRequest, LLMResponse
from app.core.schemas.message import MessageRole
from app.handlers.commands import handle_role_callback, handle_start
from app.handlers.dialog import handle_retry_callback, handle_user_text
from app.modes.registry import registry
from app.services.dialog_service import DialogService


class InMemoryPool:
    def __init__(self):
        self.users: dict[int, dict] = {}
        self.messages: list[dict] = []
        self.summaries: dict[int, str] = {}
        self._msg_id_counter = 1

    def acquire(self):
        return _InMemoryConnectionContext(self)


class _InMemoryConnectionContext:
    def __init__(self, pool: InMemoryPool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, *args):
        q = " ".join(query.split())

        if "INSERT INTO user_settings" in q:
            user_id = args[0]
            if user_id not in self.pool.users:
                self.pool.users[user_id] = {
                    "user_id": user_id,
                    "mode": "study",
                    "temperature": 0.7,
                    "created_at": None,
                    "updated_at": None,
                }

            if "mode = EXCLUDED.mode" in q:
                self.pool.users[user_id]["mode"] = args[1]
            elif "temperature = EXCLUDED.temperature" in q:
                self.pool.users[user_id]["temperature"] = args[1]

        elif "INSERT INTO dialog_messages" in q:
            user_id, role, content, char_count, metadata = (
                args[0],
                args[1],
                args[2],
                args[3],
                args[4],
            )
            self.pool.messages.append(
                {
                    "id": self.pool._msg_id_counter,
                    "user_id": user_id,
                    "role": role,
                    "content": content,
                    "char_count": char_count,
                    "metadata": metadata,
                }
            )
            self.pool._msg_id_counter += 1

        elif "DELETE FROM dialog_messages WHERE user_id = $1" in q:
            user_id = args[0]
            self.pool.messages = [m for m in self.pool.messages if m["user_id"] != user_id]

        elif "DELETE FROM dialog_summaries WHERE user_id = $1" in q:
            user_id = args[0]
            self.pool.summaries.pop(user_id, None)

        elif "INSERT INTO dialog_summaries" in q:
            user_id, summary = args[0], args[1]
            self.pool.summaries[user_id] = summary

        elif "DELETE FROM dialog_messages WHERE id = ANY" in q:
            ids_to_del = set(args[0])
            self.pool.messages = [m for m in self.pool.messages if m["id"] not in ids_to_del]

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT user_id, mode, temperature" in q:
            user_id = args[0]
            return self.pool.users.get(user_id)
        if "SELECT summary FROM dialog_summaries" in q:
            user_id = args[0]
            summary = self.pool.summaries.get(user_id)
            return {"summary": summary} if summary else None
        return None

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT role, content, metadata, char_count FROM dialog_messages" in q:
            user_id = args[0]
            limit = args[1]
            matching = [m for m in self.pool.messages if m["user_id"] == user_id]
            matching_sorted = sorted(matching, key=lambda m: m["id"], reverse=True)
            return matching_sorted[:limit]
        if "SELECT id, role, content, metadata, char_count FROM dialog_messages" in q:
            user_id = args[0]
            matching = [m for m in self.pool.messages if m["user_id"] == user_id]
            return sorted(matching, key=lambda m: m["id"], reverse=False)
        return []


@pytest.fixture
def in_memory_pool():
    return InMemoryPool()


@pytest.fixture
def base_settings():
    return Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="test-key",
        llm_model="openai/gpt-4o-mini",
        history_max_messages=10,
        history_max_chars=8000,
    )


async def test_scenario_1_message_sent_with_active_mode_instruction(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value=LLMResponse(content="Генератор — это функция с yield.")
    )
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    response = await service.process_user_message(101, "Что такое генератор?")

    assert response == "Генератор — это функция с yield."
    mock_llm.generate.assert_awaited_once()

    req: LLMRequest = mock_llm.generate.call_args.args[0]
    assert req.messages[0].role == MessageRole.SYSTEM
    assert "преподаватель и ментор" in req.messages[0].content
    assert req.messages[-1].role == MessageRole.USER
    assert req.messages[-1].content == "Что такое генератор?"


async def test_scenario_2_user_history_isolation(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        side_effect=[
            LLMResponse(content="Ответ для пользователя 1"),
            LLMResponse(content="Ответ для пользователя 2"),
            LLMResponse(content="Второй ответ для пользователя 1"),
        ]
    )
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    await service.process_user_message(1, "Вопрос 1 от U1")
    await service.process_user_message(2, "Вопрос 1 от U2")
    await service.process_user_message(1, "Вопрос 2 от U1")

    last_req: LLMRequest = mock_llm.generate.call_args_list[2].args[0]
    contents = [m.content for m in last_req.messages]

    assert "Вопрос 1 от U1" in contents
    assert "Ответ для пользователя 1" in contents
    assert "Вопрос 2 от U1" in contents
    assert "Вопрос 1 от U2" not in contents
    assert "Ответ для пользователя 2" not in contents


async def test_scenario_3_history_truncation_preserves_order_and_current_message(in_memory_pool):
    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="test-key",
        history_max_messages=3,
        history_max_chars=500,
    )
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        side_effect=lambda req: LLMResponse(content=f"Ans to {req.messages[-1].content}")
    )
    service = DialogService(in_memory_pool, settings, mock_llm, registry)

    for i in range(1, 5):
        await service.process_user_message(10, f"Q{i}")
    await service.process_user_message(10, "Q5")

    last_req: LLMRequest = mock_llm.generate.call_args.args[0]
    non_system_messages = [m for m in last_req.messages if m.role != MessageRole.SYSTEM]
    contents = [m.content for m in non_system_messages]

    assert contents[-1] == "Q5"
    assert "Q1" not in contents
    for idx in range(len(contents) - 1):
        assert contents[idx] != contents[idx + 1]


async def test_scenario_4_mode_switch_clears_history(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value=LLMResponse(content="Ok"))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    await service.process_user_message(20, "Привет в study")
    assert len(in_memory_pool.messages) > 0

    new_mode = await service.switch_mode(20, "translate")
    assert new_mode.id == "translate"

    user, mode = await service.get_user_status(20)
    assert user.mode == "translate"
    assert mode.id == "translate"

    user_msgs = [m for m in in_memory_pool.messages if m["user_id"] == 20]
    assert len(user_msgs) == 0


async def test_scenario_5_reset_clears_only_caller_history(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value=LLMResponse(content="Ok"))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    await service.process_user_message(100, "Сообщение U100")
    await service.process_user_message(200, "Сообщение U200")

    await service.reset_history(100)

    u100_msgs = [m for m in in_memory_pool.messages if m["user_id"] == 100]
    assert len(u100_msgs) == 0

    u200_msgs = [m for m in in_memory_pool.messages if m["user_id"] == 200]
    assert len(u200_msgs) > 0


async def test_scenario_6_settings_accepts_valid_and_rejects_invalid(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    for val in (0.0, 0.3, 0.7, 1.0):
        updated = await service.set_temperature(30, val)
        assert updated == val
        user, _ = await service.get_user_status(30)
        assert user.temperature == val

    for invalid in (0.1, 0.5, 0.9, -0.5, 2.0):
        with pytest.raises(InvalidTemperatureError):
            await service.set_temperature(30, invalid)


async def test_scenario_7_llm_error_converts_to_safe_user_message(in_memory_pool, base_settings):
    bot = Bot(base_settings.bot_token)
    sent_methods = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        if isinstance(method, SendMessage):
            sent_methods.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=LLMTimeoutError("Timeout"))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    message = Message(
        message_id=1,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        text="Сложный запрос",
    )
    message._bot = bot

    await handle_user_text(message, dialog_service=service)

    assert len(sent_methods) == 1
    reply = sent_methods[0].text
    assert "Превышено время ожидания" in reply
    assert "Traceback" not in reply
    assert "api_key" not in reply

    assert sent_methods[0].reply_markup is not None
    assert sent_methods[0].reply_markup.inline_keyboard[0][0].text == "🔄 Повторить"
    assert sent_methods[0].reply_markup.inline_keyboard[0][0].callback_data.startswith("retry:")

    saved_history = [m for m in in_memory_pool.messages if m["user_id"] == 42]
    assert len(saved_history) == 0


async def test_scenario_retry_on_timeout_successful(in_memory_pool, base_settings):
    bot = Bot(base_settings.bot_token)
    sent_methods = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        sent_methods.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=LLMTimeoutError("Timeout"))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    message = Message(
        message_id=1,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        text="Сложный запрос",
    )
    message._bot = bot

    await handle_user_text(message, dialog_service=service)

    send_messages = [m for m in sent_methods if isinstance(m, SendMessage)]
    assert len(send_messages) == 1
    retry_button = send_messages[0].reply_markup.inline_keyboard[0][0]

    mock_llm.generate = AsyncMock(return_value=LLMResponse(content="Успешный ответ после повтора"))
    error_msg = Message(
        message_id=2,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=100, is_bot=True, first_name="Bot"),
        text=send_messages[0].text,
        reply_to_message=message,
    )
    error_msg._bot = bot

    callback = CallbackQuery(
        id="cb_1",
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        chat_instance="ci_1",
        message=error_msg,
        data=retry_button.callback_data,
    )
    callback._bot = bot

    await handle_retry_callback(callback, dialog_service=service)

    send_messages = [m for m in sent_methods if isinstance(m, SendMessage)]
    assert len(send_messages) == 2
    assert "Успешный ответ после повтора" in send_messages[1].text

    saved_history = [m for m in in_memory_pool.messages if m["user_id"] == 42]
    assert len(saved_history) == 2


async def test_scenario_retry_not_found_alerts_user(in_memory_pool, base_settings):
    bot = Bot(base_settings.bot_token)
    sent_methods = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        sent_methods.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    service = DialogService(in_memory_pool, base_settings, MagicMock(), registry)

    error_msg = Message(
        message_id=2,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=100, is_bot=True, first_name="Bot"),
        text="Ошибка",
    )
    error_msg._bot = bot

    callback = CallbackQuery(
        id="cb_2",
        from_user=User(id=999, is_bot=False, first_name="Другой"),
        chat_instance="ci_2",
        message=error_msg,
        data="retry:unknown_key",
    )
    callback._bot = bot

    await handle_retry_callback(callback, dialog_service=service)

    answers = [m for m in sent_methods if isinstance(m, AnswerCallbackQuery)]
    assert len(answers) == 1
    assert answers[0].show_alert is True
    assert "Не удалось найти исходный запрос" in answers[0].text


async def test_scenario_8_long_response_chunking_without_loss(in_memory_pool, base_settings):
    bot = Bot(base_settings.bot_token)
    sent_chunks = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        if isinstance(method, SendMessage):
            sent_chunks.append(method.text)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    long_answer = "Часть первая.\n" + "X" * 3500 + "\n\nЧасть вторая.\n" + "Y" * 3200
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value=LLMResponse(content=long_answer))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    message = Message(
        message_id=1,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        text="Дай длинный ответ",
    )
    message._bot = bot

    await handle_user_text(message, dialog_service=service)

    assert len(sent_chunks) >= 2
    for chunk in sent_chunks:
        assert len(chunk) <= 4096

    combined_received = "\n\n".join(sent_chunks)
    assert "Часть первая." in combined_received
    assert "Часть вторая." in combined_received
    assert len(combined_received) >= len(long_answer)


async def test_scenario_main_menu_role_buttons_and_callback(in_memory_pool, base_settings):
    bot = Bot(base_settings.bot_token)
    sent_methods = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        sent_methods.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    message = Message(
        message_id=1,
        date=MagicMock(),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        text="/start",
    )
    message._bot = bot

    await handle_start(message)

    send_messages = [m for m in sent_methods if isinstance(m, SendMessage)]
    assert len(send_messages) == 1
    markup = send_messages[0].reply_markup
    assert markup is not None
    assert len(markup.inline_keyboard) == 3
    assert markup.inline_keyboard[0][0].callback_data == "set_role:study"
    assert markup.inline_keyboard[1][0].callback_data == "set_role:translate"
    assert markup.inline_keyboard[2][0].callback_data == "set_role:review"

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value=LLMResponse(content="Привет!"))
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    await service.process_user_message(42, "Привет")
    user_msgs = [m for m in in_memory_pool.messages if m["user_id"] == 42]
    assert len(user_msgs) > 0

    callback = CallbackQuery(
        id="cb_mode",
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        chat_instance="ci_mode",
        message=message,
        data="set_role:translate",
    )
    callback._bot = bot

    await handle_role_callback(callback, dialog_service=service)

    answers = [m for m in sent_methods if isinstance(m, AnswerCallbackQuery)]
    assert len(answers) == 1
    assert "Переводчик" in answers[0].text

    user, mode = await service.get_user_status(42)
    assert user.mode == "translate"
    assert mode.id == "translate"

    user_msgs_after = [m for m in in_memory_pool.messages if m["user_id"] == 42]
    assert len(user_msgs_after) == 0


async def test_compaction_triggered_when_threshold_reached(in_memory_pool, base_settings):
    summary_text = "Саммари: студент изучает алгоритмы сортировки на Python."
    answer_text = "Timsort имеет сложность O(N log N)."

    llm_responses = [
        LLMResponse(content=summary_text),
        LLMResponse(content=answer_text),
    ]
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=llm_responses)
    mock_llm.get_context_length = AsyncMock(return_value=32768)

    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    for i in range(10):
        in_memory_pool.messages.append(
            {
                "id": in_memory_pool._msg_id_counter,
                "user_id": 99,
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Сообщение {i}",
                "char_count": len(f"Сообщение {i}"),
                "metadata": {},
            }
        )
        in_memory_pool._msg_id_counter += 1

    assert len([m for m in in_memory_pool.messages if m["user_id"] == 99]) == 10

    response = await service.process_user_message(99, "Расскажи про Timsort")

    assert response == answer_text
    assert in_memory_pool.summaries.get(99) == summary_text

    kept_after_compaction = [m for m in in_memory_pool.messages if m["user_id"] == 99]
    assert len(kept_after_compaction) == 3 + 2

    last_call = mock_llm.generate.call_args_list[-1]
    last_req = last_call.args[0]
    system_contents = [m.content for m in last_req.messages if m.role == MessageRole.SYSTEM]
    assert any(
        "Контекст предыдущей части диалога" in c and summary_text in c for c in system_contents
    )


async def test_reset_history_clears_messages_and_summary(in_memory_pool, base_settings):
    mock_llm = MagicMock()
    service = DialogService(in_memory_pool, base_settings, mock_llm, registry)

    in_memory_pool.messages.append(
        {
            "id": 1,
            "user_id": 77,
            "role": "user",
            "content": "Вопрос",
            "char_count": 6,
            "metadata": {},
        }
    )
    in_memory_pool.summaries[77] = "Старое саммари"

    await service.reset_history(77)

    assert len([m for m in in_memory_pool.messages if m["user_id"] == 77]) == 0
    assert 77 not in in_memory_pool.summaries
