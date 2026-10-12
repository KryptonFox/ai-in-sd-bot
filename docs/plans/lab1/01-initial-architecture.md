# Архитектурный план: База данных, миграции и модульная структура (Лабораторная работа № 1)

Разработка расширяемой архитектуры AI-ассистента в Telegram на базе OpenAI-совместимого API с сохранением контекста в PostgreSQL, production-grade системой миграций и реестром режимов работы (`/study`, `/translate`, `/review`).

---

## User Review Required

> [!IMPORTANT]
> **Production-grade система миграций на SQL + `asyncpg`**:
> Мы реализуем механизм миграций с транзакционным выполнением, вычислением контрольных сумм (SHA-256) и защитой от параллельного запуска через консультативные блокировки PostgreSQL (`pg_advisory_lock`). Это позволяет избежать тяжелых ORM-зависимостей (вроде SQLAlchemy/Alembic), обеспечивает 100% асинхронность и надёжность при перезапусках приложения.

> [!IMPORTANT]
> **Реестр режимов (Registry Pattern)**:
> Каждый режим описывается декларативным объектом `BotMode` (команда, системный промпт, few-shot примеры, описание). Добавление нового режима сводится к добавлению одной декларации в реестр без изменения хэндлеров, логики маршрутизации, команды `/start` или схемы БД.

---

## Предлагаемая архитектура проекта

```text
app/
├── config.py                 # Валидация настроек (BOT_TOKEN, POSTGRES_*, LLM_*, HISTORY_*)
├── db.py                     # Пул asyncpg и автоматический запуск миграций при старте
├── migrations/               # Механизм миграций
│   ├── __init__.py
│   ├── runner.py             # Транзакционный раннер (pg_advisory_lock, SHA-256 checksums)
│   └── sql/
│       └── 0001_initial_schema.sql  # Таблицы user_settings и dialog_messages
├── storage/                  # Репозитории для работы с БД
│   ├── __init__.py
│   ├── models.py             # Dataclass-модели (UserSettings, HistoryMessage)
│   ├── user_repo.py          # CRUD настроек пользователя (режим, temperature)
│   └── history_repo.py       # Сохранение, выборка с ограничением (count + tokens/chars), сброс
├── modes/                    # Реестр и системные инструкции
│   ├── __init__.py
│   ├── base.py               # BotMode, FewShotExample
│   ├── registry.py           # ModeRegistry (register, get, list_all, default)
│   └── definitions.py        # Режимы /study, /translate, /review (с few-shot)
├── llm/                      # Абстракция работы с языковой моделью
│   ├── __init__.py
│   ├── client.py             # OpenAI-совместимый асинхронный клиент
│   └── exceptions.py         # Безопасные исключения (LLMTimeoutError, LLMServiceError)
├── handlers/                 # Telegram-обработчики (aiogram 3)
│   ├── __init__.py
│   ├── commands.py           # /start, /settings, /reset, смена режимов через реестр
│   └── dialog.py             # Текстовые сообщения, typing статус, чанкинг > 4096 символов
├── health.py                 # Проверка состояния (существующий healthcheck)
├── logging_setup.py          # Безопасное логирование
└── telegram.py               # Инициализация aiogram Bot
```

---

## Proposed Changes

### Этап 1: Конфигурация и переменные окружения

#### [MODIFY] [app/config.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py)
* Добавление параметров LLM:
  - `llm_base_url: str` (по умолчанию `https://api.openai.com/v1`)
  - `llm_api_key: str` (скрыт из repr)
  - `llm_model: str` (например, `gpt-4o-mini` или `deepseek-chat`)
  - `llm_timeout: float` (по умолчанию 30.0 сек)
* Добавление лимитов истории диалога:
  - `history_max_messages: int` (по умолчанию 10)
  - `history_max_chars: int` (по умолчанию 8000 символов)
* Валидация и безопасная обработка ошибок ([ConfigError](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py#L13-L15)).

#### [MODIFY] [.env.example](file:///home/krypton/Dev/itmo/ai-in-sd-bot/.env.example)
* Добавление шаблонов новых переменных с фиктивными значениями.

---

### Этап 2: Production-Grade механизм миграций и схема БД

#### [NEW] [app/migrations/runner.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/migrations/runner.py)
* Таблица версионирования `schema_migrations` (`version INT PRIMARY KEY`, `name TEXT`, `checksum TEXT`, `applied_at TIMESTAMPTZ`).
* Консультативная блокировка `pg_advisory_lock` для защиты от race conditions при рестарте контейнеров.
* Каждая миграция выполняется строго в отдельной транзакции.
* Проверка контрольных сумм SHA-256 ранее применённых миграций для предотвращения незамеченных изменений старых файлов.

#### [NEW] [app/migrations/sql/0001_initial_schema.sql](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/migrations/sql/0001_initial_schema.sql)
1. **`user_settings`**:
   - `user_id BIGINT PRIMARY KEY` (Telegram ID).
   - `mode VARCHAR(32) NOT NULL DEFAULT 'study'` (текущий режим).
   - `temperature REAL NOT NULL DEFAULT 0.7` (значения `0.0`, `0.3`, `0.7`, `1.0`).
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`.
   - `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`.
2. **`dialog_messages`**:
   - `id BIGSERIAL PRIMARY KEY`.
   - `user_id BIGINT NOT NULL REFERENCES user_settings(user_id) ON DELETE CASCADE`.
   - `role VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant'))`.
   - `content TEXT NOT NULL`.
   - `char_count INT NOT NULL DEFAULT 0`.
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`.
   - Индекс `idx_dialog_messages_user_created` по `(user_id, created_at ASC)`.

#### [MODIFY] [app/db.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/db.py)
* Вызов `await run_migrations(pool)` сразу после проверки `SELECT 1`.

---

### Этап 3: Слой хранения данных (Repository Pattern)

#### [NEW] [app/storage/models.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/storage/models.py)
* Датаклассы `UserSettings` и `HistoryMessage`.

#### [NEW] [app/storage/user_repo.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/storage/user_repo.py)
* `get_or_create(user_id: int) -> UserSettings`
* `set_mode(user_id: int, mode: str) -> None`
* `set_temperature(user_id: int, temperature: float) -> None`

#### [NEW] [app/storage/history_repo.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/storage/history_repo.py)
* `add_message(user_id: int, role: str, content: str) -> None`
* `get_history(user_id: int, max_messages: int, max_chars: int) -> list[HistoryMessage]`:
  - Выборка последних $N$ сообщений с обрезкой по суммарному объёму, сохраняя хронологический порядок (`ASC`).
* `clear_history(user_id: int) -> None`: сброс истории при смене режима или команде `/reset`.

---

### Этап 4: Реестр режимов (Extensible Mode Registry)

#### [NEW] [app/modes/base.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/modes/base.py)
* Датакласс `BotMode`:
  - `command: str` (например, `"study"`, `"translate"`, `"review"`)
  - `name: str` (например, `"Обучение"`)
  - `description: str` (описание для `/start` и подсказок)
  - `system_prompt: str` (системная инструкция по структуре R.C.T.F.)
  - `few_shot_examples: list[tuple[str, str]]` (пары вход-выход)

#### [NEW] [app/modes/registry.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/modes/registry.py)
* Класс `ModeRegistry`: регистрация, получение режима по команде/id, список всех доступных команд, получение режима по умолчанию (`study`).

#### [NEW] [app/modes/definitions.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/modes/definitions.py)
* Реализация трёх режимов:
  1. `/study` — структурированное объяснение учебных концепций программирования.
  2. `/translate` — перевод текстов и кода на указанный язык.
  3. `/review` — подробный код-ревью (поиск багов, анализ сложности, рекомендации по стилю) с **few-shot примерами**.

---

### Этап 5: LLM-клиент и Telegram-обработчики

#### [NEW] [app/llm/client.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py)
* Асинхронный клиент к OpenAI-совместимому API (через `httpx` или официальный `openai.AsyncOpenAI`).
* Обработка таймаутов, ошибок авторизации, ошибок сервера и парсинг ответа.
* Тестируемость: возможность подмены клиента моком.

#### [NEW] [app/handlers/commands.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/commands.py)
* `/start` — динамически формирует справку на основе зарегистрированных в `ModeRegistry` режимов.
* `/settings` — просмотр настроек и выбор `temperature` (с клавиатурой или аргументами: 0.0, 0.3, 0.7, 1.0).
* `/reset` — очистка контекста текущего пользователя.
* Обработчик переключения режимов — динамически привязан к зарегистрированным в `ModeRegistry` командам: переключает режим в БД и очищает историю.

#### [NEW] [app/handlers/dialog.py](file:///home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py)
* Обработка пользовательских сообщений (только в личных чатах `F.chat.type == "private"`).
* Вызов `send_chat_action("typing")`.
* Загрузка контекста, сборка промпта (system + few-shot + history + current message).
* Вызов LLM, сохранение ответа в БД при успехе.
* Чанкинг ответов длиннее 4096 символов.

---

## Verification Plan

### Automated Tests
1. **Тесты миграций и репозиториев БД**:
   - `pytest tests/test_migrations.py` — проверка наката миграций, проверки контрольной суммы, изоляции транзакций.
   - `pytest tests/test_storage.py` — изоляция пользователей, усечение истории по лимитам, очистка истории.
2. **Тесты реестра режимов**:
   - `pytest tests/test_modes.py` — регистрация новых режимов, получение режима по умолчанию, форматирование промптов.
3. **Обязательные тесты из методички (8 сценариев)**:
   - `pytest tests/test_bot_scenarios.py`:
     - Обычное сообщение с инструкцией режима.
     - Изоляция историй двух пользователей.
     - Применение стратегии сокращения истории.
     - Смена режима очищает историю и сохраняет настройку.
     - `/reset` очищает только историю вызвавшего пользователя.
     - `/settings` принимает допустимые и отклоняет недопустимые температуры.
     - Ошибка LLM преобразуется в безопасное пользовательское сообщение.
     - Ответ > 4096 символов корректно разбивается без потерь.
4. **Статические проверки**:
   - `uv run ruff check .`
   - `uv run ruff format --check .`

### Manual Verification
1. Запуск PostgreSQL через `docker compose -f compose.local.yaml up -d`.
2. Запуск бота через `uv run python -m app` и проверка в Telegram:
   - `/start`
   - Переключение на `/review`, отправка сниппета кода.
   - `/settings` и переключение `temperature`.
   - `/reset`.
