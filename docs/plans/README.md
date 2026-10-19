# История и эволюция планов реализации (Implementation Plans)

В этом каталоге фиксируются все версии архитектурных и технических планов проекта по лабораторным работам. Сохранение истории позволяет отслеживать принятые инженерные решения, контекст изменений и использовать эти данные при оформлении отчётов.

---

## Лабораторная работа № 1

* [01-initial-architecture.md](file:///home/krypton/Dev/itmo/ai-in-sd-bot/docs/plans/lab1/01-initial-architecture.md) — **Ревизия 1 (Черновая)**:
  - Первоначальное проектирование архитектуры бота для Лаб 1.
  - Базовые dataclass-модели без Pydantic.
  - Ограничение схемы БД только ролями `user` и `assistant`.
* [02-pydantic-and-mcp-readiness.md](file:///home/krypton/Dev/itmo/ai-in-sd-bot/docs/plans/lab1/02-pydantic-and-mcp-readiness.md) — **Ревизия 2 (Утверждённая)**:
  - **Корректировка после анализа Лаб 2 и 3**:
    1. Интеграция `Pydantic v2` для строгой валидации DTO и подготовки к схемам инструментов MCP.
    2. Расширение таблицы `dialog_messages` (роли `user`, `assistant`, `system`, `tool` и поле `metadata JSONB` под `tool_calls`).
    3. Выделение сервисного слоя (`DialogService` -> `AgentService`), отделенного от Telegram-хэндлеров, для бесшовного добавления агентного цикла в Лаб 2.
    4. Подготовка сквозного `request_id` (correlation ID) через `contextvars` для трассировки (Лаб 3).

---

## Лабораторная работа № 2

* [01-agent-and-mcp-architecture.md](file:///home/krypton/Dev/itmo/ai-in-sd-bot/docs/plans/lab2/01-agent-and-mcp-architecture.md) (см. также [docs/lab2/plan.md](file:///home/krypton/Dev/itmo/ai-in-sd-bot/docs/lab2/plan.md)) — **Ревизия 1 (Утверждённый план)**:
  - Текущее состояние проекта по итогам Лаб 1 (91 тест, синхронизация с апстримом).
  - FastMCP сервер с гибридным транспортом (SSE для Docker Compose + STDIO для тестов и MCP Inspector).
  - Контракты инструментов (`get_weather`, `get_schedule`, `add_reminder`) и ресурса `schedule://current-week`.
  - Двухфазное подтверждение с Human-in-the-Loop и защита от Confused Deputy.
  - Регламентные документы (`AGENTS.md`, `spec.md`, `plan.md`) и эксперимент по маршрутизации.

---

## Регламент версионирования планов

1. Новые планы создаются в подпапке соответствующей лабораторной: `docs/plans/<lab_name>/<NN>-<name>.md`.
2. При существенной корректировке плана старый файл не перезаписывается, а создаётся следующая ревизия.
3. В шапке каждого плана указываются ключевые изменения относительно предыдущей версии.

