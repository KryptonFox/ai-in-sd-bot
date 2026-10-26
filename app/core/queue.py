import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from app.core.exceptions import QueueFullError

logger = logging.getLogger("app.core.queue")


class UserQueueManager:
    """Менеджер очередей сообщений пользователей.

    Обеспечивает последовательную обработку сообщений одного пользователя (FIFO),
    предотвращая состояния гонки (race conditions) при одновременной отправке
    нескольких сообщений подряд, при этом запросы разных пользователей
    обрабатываются параллельно.
    """

    def __init__(self, max_queue_size: int = 3) -> None:
        self.max_queue_size = max_queue_size
        self._locks: dict[int, asyncio.Lock] = {}
        self._active_counts: dict[int, int] = {}
        self._global_lock = asyncio.Lock()

    async def is_user_busy(self, user_id: int) -> bool:
        """Проверяет, обрабатывается ли сейчас запрос данного пользователя."""
        async with self._global_lock:
            return self._active_counts.get(user_id, 0) > 0

    async def get_queue_size(self, user_id: int) -> int:
        """Возвращает текущее количество активных и ожидающих запросов пользователя."""
        async with self._global_lock:
            return self._active_counts.get(user_id, 0)

    @asynccontextmanager
    async def acquire(self, user_id: int, ignore_limits: bool = False) -> AsyncGenerator[int]:
        """Захватывает очередь для пользователя.

        Yields:
            int: Позиция в очереди (0 если выполняется сразу, > 0 если ожидало).

        Raises:
            QueueFullError: Если количество запросов пользователя превышает лимит.
        """
        async with self._global_lock:
            current_count = self._active_counts.get(user_id, 0)
            if not ignore_limits and current_count >= self.max_queue_size:
                raise QueueFullError(
                    f"Очередь сообщений пользователя {user_id} переполнена "
                    f"(максимум {self.max_queue_size})"
                )

            self._active_counts[user_id] = current_count + 1
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            user_lock = self._locks[user_id]
            queue_position = current_count

        await user_lock.acquire()
        try:
            yield queue_position
        finally:
            user_lock.release()
            async with self._global_lock:
                self._active_counts[user_id] -= 1
                if self._active_counts[user_id] <= 0:
                    self._active_counts.pop(user_id, None)
                    self._locks.pop(user_id, None)
