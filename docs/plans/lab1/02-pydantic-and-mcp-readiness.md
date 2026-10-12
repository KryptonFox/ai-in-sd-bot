# Архитектурный план: База данных, Pydantic, миграции и задел под Лабораторные № 2 и № 3

## Анализ требований Лабораторных № 2 и № 3

1. **Лабораторная № 2 (AI-агент и MCP-сервер)**:
   - Бот превращается из простого чат-бота в агента с **агентным циклом (agent loop)**: модель выбирает действие (`tool_calls`) -> код вызывает инструмент через протокол **MCP** -> результат инструмента возвращается модели (`role: tool`) -> модель формирует ответ.
   - Требуется **структурированный вывод** и строгая схема вызова инструментов (JSON Schema / Pydantic).
   - В истории диалога появятся сообщения роли `tool` и метаданные вызовов инструментов (`tool_call_id`, аргументы).

2. **Лабораторная № 3 (Маршрутизация, проактивность и трассировка)**:
   - **Маршрутизация (Routing)** между специализированными агентами.
   - **Проактивные сценарии** (доставка напоминаний по расписанию / фоновые задачи без входящего сообщения).
   - **Трассировка (Tracing)**: сквозной `request_id` (correlation ID) для отслеживания цепочки событий от Telegram до LLM и БД.

---

## Архитектурные решения для предотвращения рефакторинга

Чтобы не переписывать проект в Лабораторных № 2 и № 3:

1. **Внедрение Pydantic v2**:
   - Валидация настроек окружения и конфигурации.
   - Строгие модели предметной области (`UserSettings`, `ChatMessage`, `LLMResponse`, `BotMode`).
   - Модели сообщений поддерживают роли `user`, `assistant`, `system`, `tool` и вызовы инструментов (`tool_calls`). В Лабораторной № 2 эти же модели будут использоваться для генерации схем MCP-инструментов.

2. **Сервисный слой (Service Layer / Orchestrator)**:
   - Тонкие Telegram-хэндлеры не вызывают LLM напрямую. Они делегируют работу `DialogService` (в Лаб 2 расширяется до `AgentService` с циклом агента).
   - При появлении вызова инструментов в Лаб 2 логика Telegram-хэндлеров не изменится вообще.

3. **Схема базы данных сразу с поддержкой `tool` и `metadata`**:
   - `dialog_messages` сразу поддерживает `role IN ('user', 'assistant', 'system', 'tool')`.
   - Поле `metadata JSONB` для хранения `tool_call_id`, имени инструмента или аргументов. Для Лаб 1 это поле опционально/пусто, а в Лаб 2 не потребуется ломать схему БД.

4. **Сквозной `request_id` (Трассировка)**:
   - Использование `contextvars` для correlation ID на каждом входящем сообщении Telegram. Логи LLM и БД автоматически связываются с конкретным апдейтом.

---

## Целевая структура проекта

```text
app/
├── core/                         # Ядро приложения
│   ├── config.py                 # Настройки на базе Pydantic
│   ├── exceptions.py             # Иерархия доменных исключений (AppError, LLMError, ConfigError)
│   ├── context.py                # request_id / correlation ID через contextvars
│   └── schemas/                  # Pydantic v2 модели
│       ├── user.py               # UserSettings, UserMode
│       ├── message.py            # MessageRole (user/assistant/system/tool), ChatMessage, HistoryLimit
│       └── llm.py                # LLMRequest, LLMResponse, ToolCall
├── db/                           # Слой работы с PostgreSQL
│   ├── connection.py             # asyncpg пул
│   ├── migrations/               # Production-grade раннер миграций
│   │   ├── runner.py             # Транзакции, SHA-256 чексуммы, pg_advisory_lock
│   │   └── sql/
│   │       └── 0001_initial_schema.sql  # user_settings, dialog_messages (с role 'tool' и metadata JSONB)
│   └── repositories/             # Репозитории данных
│       ├── user_repo.py          # Получение/обновление режима и temperature
│       └── history_repo.py       # Сохранение сообщений, усечение по лимитам (count + chars), сброс
├── llm/                          # Интеграция с языковой моделью
│   ├── base.py                   # Protocol BaseLLMClient
│   └── openai_client.py          # Асинхронный OpenAI-совместимый клиент (принимает/возвращает Pydantic-схемы)
├── modes/                        # Реестр режимов и промпты (в Лаб 2 станет реестром агентов)
│   ├── base.py                   # Pydantic-модель BotMode (name, command, description, prompt, few-shots)
│   ├── registry.py               # ModeRegistry (register, get, list_all, default)
│   └── definitions.py            # /study, /translate, /review (с few-shot примерами)
├── services/                     # Сервисный слой (оркестрация)
│   └── dialog_service.py         # Координация: получение истории -> сборка промпта -> вызов LLM -> сохранение ответа
├── handlers/                     # Тонкие aiogram-хэндлеры
│   ├── commands.py               # /start, /settings, /reset, переключение режимов через ModeRegistry
│   └── dialog.py                 # Текстовые сообщения, typing статус, разбиение сообщений > 4096 символов
├── health.py                     # HTTP Healthcheck сервер
├── logging_setup.py              # Логирование со сквозным request_id и фильтрацией секретов
└── telegram.py                   # Фабрика aiogram Bot
```

---

## План по этапам реализации

### Этап 1. Зависимости и ядро (`app/core/`, Pydantic)
1. Добавить `pydantic==2.*` в `pyproject.toml` (через `uv add pydantic`).
2. Создать Pydantic-модели:
   - `ChatMessage` (с поддержкой ролей `user`, `assistant`, `system`, `tool`).
   - `UserSettings` (режим, температура с валидацией допустимых значений `0.0, 0.3, 0.7, 1.0`).
   - `LLMResponse` (текст, токены, задел под `tool_calls`).
   - `BotMode` (конфигурация режима, системная инструкция, few-shot примеры).
3. Обновить `app/config.py` с использованием Pydantic для валидации параметров LLM (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT`) и лимитов истории.

### Этап 2. База данных и production-grade миграции (`app/db/`)
1. Реализовать транзакционный раннер миграций (`app/db/migrations/runner.py`):
   - Таблица `schema_migrations` (версия, имя, sha256 хеш, дата применения).
   - Консультативная блокировка `pg_advisory_lock` (защита от гонок при рестарте).
   - Валидация неизменности ранее примененных миграций.
2. Создать файл первой миграции `0001_initial_schema.sql`:
   - Таблица `user_settings` (user_id, mode, temperature, timestamps).
   - Таблица `dialog_messages` (id, user_id, role, content, char_count, metadata JSONB, created_at) + индекс по `(user_id, created_at ASC)`.
3. Реализовать репозитории `user_repo.py` и `history_repo.py`.
4. Интегрировать автоматический запуск миграций при старте приложения в `app/db/connection.py`.

### Этап 3. Реестр режимов (`app/modes/`)
1. Реализовать `ModeRegistry` с методами динамической регистрации и поиска.
2. Определить три режима:
   - `/study` — интерактивный разбор концепций программирования.
   - `/translate` — технический перевод кода и документации.
   - `/review` — код-ревью с поиском ошибок и рекомендациями по качеству (с **few-shot примерами**).

### Этап 4. LLM-клиент и Сервисный слой (`app/llm/`, `app/services/`)
1. Реализовать `OpenAIClient` на базе `httpx` или `openai`:
   - Принимает список `ChatMessage` и параметры генерации (`temperature`, `model`).
   - Безопасная обработка таймаутов и кодов ошибок с маскированием ключей.
2. Реализовать `DialogService`:
   - Загрузка контекста с учетом лимитов (сообщения + объем).
   - Формирование диалога: системный промпт режима -> few-shot примеры -> история -> текущий запрос.
   - Вызов LLM.
   - Сохранение успешного ответа (ошибочные не сохраняются).

### Этап 5. Telegram-хэндлеры (`app/handlers/`)
1. `commands.py`:
   - `/start` — динамический текст на основе зарегистрированных в `ModeRegistry` режимов.
   - `/settings` — просмотр и выбор `temperature` (0.0, 0.3, 0.7, 1.0) с инлайн-кнопками или аргументами.
   - `/reset` — очистка истории текущего пользователя.
   - Универсальный хэндлер смены режима: динамически обрабатывает `/study`, `/translate`, `/review` и любые будущие режимы.
2. `dialog.py`:
   - Фильтр личных сообщений (`F.chat.type == "private"`).
   - Отправка `ChatAction.TYPING`.
   - Вызов `DialogService`.
   - Разбиение длинных ответов (> 4096 символов) без потери разметки и порядка.

### Этап 6. Тестирование и верификация
1. Unit-тесты для Pydantic-моделей и валидации.
2. Тесты мигратора и репозиториев БД (с моками asyncpg).
3. 8 обязательных сценариев из методички с моками Telegram и LLM.
4. Проверка `ruff check .` и `ruff format --check .`.

---

## План верификации

### Автоматические тесты
```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
```

### Ручная проверка
1. Прогон миграций на локальном PostgreSQL в Docker.
2. Запуск бота через `uv run python -m app` и проверка сценариев:
   - Проверка `/start` и списка режимов.
   - Переключение на `/review`, отправка сниппета кода.
   - Проверка сохранения контекста и перезапуска бота.
   - Переключение температуры в `/settings`.
   - Сброс контекста через `/reset`.
