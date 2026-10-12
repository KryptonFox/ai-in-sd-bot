import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from app.config import Settings
from app.core.exceptions import QueueFullError
from app.core.queue import UserQueueManager
from app.core.schemas.llm import LLMResponse
from app.handlers.dialog import handle_user_text
from app.modes.registry import registry
from app.services.dialog_service import DialogService
from tests.test_bot_scenarios import InMemoryPool


@pytest.fixture
def queue_manager():
    return UserQueueManager(max_queue_size=3)


@pytest.mark.asyncio
async def test_queue_serializes_same_user(queue_manager):
    execution_order = []

    async def worker(task_id: int, delay: float):
        async with queue_manager.acquire(user_id=1):
            execution_order.append(f"start_{task_id}")
            await asyncio.sleep(delay)
            execution_order.append(f"end_{task_id}")

    task1 = asyncio.create_task(worker(1, 0.05))
    task2 = asyncio.create_task(worker(2, 0.01))

    await asyncio.gather(task1, task2)

    assert execution_order == [
        "start_1",
        "end_1",
        "start_2",
        "end_2",
    ]


@pytest.mark.asyncio
async def test_queue_allows_different_users_concurrently(queue_manager):
    events = []

    async def worker(user_id: int):
        async with queue_manager.acquire(user_id=user_id):
            events.append(f"start_{user_id}")
            await asyncio.sleep(0.05)
            events.append(f"end_{user_id}")

    task1 = asyncio.create_task(worker(user_id=10))
    task2 = asyncio.create_task(worker(user_id=20))

    await asyncio.gather(task1, task2)

    assert "start_10" in events
    assert "start_20" in events
    # Both start before either ends because they are different users
    assert events.index("start_10") < events.index("end_10")
    assert events.index("start_20") < events.index("end_20")


@pytest.mark.asyncio
async def test_queue_full_raises_error(queue_manager):
    async with queue_manager.acquire(user_id=1):
        assert await queue_manager.get_queue_size(user_id=1) == 1

        async def subtask():
            async with queue_manager.acquire(user_id=1):
                pass

        t1 = asyncio.create_task(subtask())
        t2 = asyncio.create_task(subtask())
        await asyncio.sleep(0.01)

        # 3 tasks active or queued, 4th must raise QueueFullError
        with pytest.raises(QueueFullError):
            async with queue_manager.acquire(user_id=1):
                pass

        t1.cancel()
        t2.cancel()


@pytest.mark.asyncio
async def test_queue_ignore_limits(queue_manager):
    async with queue_manager.acquire(user_id=1):
        # Even if at capacity, ignore_limits allows acquire to wait instead of raising
        entered = False

        async def admin_task():
            nonlocal entered
            async with queue_manager.acquire(user_id=1, ignore_limits=True):
                entered = True

        task = asyncio.create_task(admin_task())
        await asyncio.sleep(0.01)
        assert not entered

    await task
    assert entered


@pytest.mark.asyncio
async def test_queue_cleans_up_state_after_completion(queue_manager):
    async with queue_manager.acquire(user_id=99):
        assert await queue_manager.is_user_busy(99) is True
        assert await queue_manager.get_queue_size(99) == 1

    assert await queue_manager.is_user_busy(99) is False
    assert await queue_manager.get_queue_size(99) == 0
    assert 99 not in queue_manager._locks
    assert 99 not in queue_manager._active_counts


@pytest.mark.asyncio
async def test_sequential_messages_in_dialog_handler():
    pool = InMemoryPool()
    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="test-key",
        llm_model="openai/gpt-4o-mini",
        user_queue_max_size=2,
    )
    mock_llm = MagicMock()

    call_count = 0

    async def fake_generate(request):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return LLMResponse(content=f"Ответ {call_count}")

    mock_llm.generate = AsyncMock(side_effect=fake_generate)
    service = DialogService(pool, settings, mock_llm, registry)

    bot = Bot(settings.bot_token)
    sent_methods = []

    async def fake_session(bot_inst, method, *args, **kwargs):
        sent_methods.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)

    msg1 = Message(
        message_id=1,
        date=MagicMock(),
        chat=Chat(id=77, type="private"),
        from_user=User(id=77, is_bot=False, first_name="User"),
        text="Первое сообщение",
    )
    msg1._bot = bot

    msg2 = Message(
        message_id=2,
        date=MagicMock(),
        chat=Chat(id=77, type="private"),
        from_user=User(id=77, is_bot=False, first_name="User"),
        text="Второе сообщение",
    )
    msg2._bot = bot

    # Fire both simultaneously
    task1 = asyncio.create_task(handle_user_text(msg1, dialog_service=service))
    task2 = asyncio.create_task(handle_user_text(msg2, dialog_service=service))

    await asyncio.gather(task1, task2)

    # Check answers sent in order
    send_messages = [m for m in sent_methods if isinstance(m, SendMessage)]
    # Filter only response messages (exclude queue notifications if any)
    answers = [m.text for m in send_messages if "Ответ" in m.text]
    assert answers == ["Ответ 1", "Ответ 2"]

    # History in pool should have user 1, assistant 1, user 2, assistant 2 in order
    user_msgs = [m["content"] for m in pool.messages if m["user_id"] == 77]
    assert user_msgs == [
        "Первое сообщение",
        "Ответ 1",
        "Второе сообщение",
        "Ответ 2",
    ]
