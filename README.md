# Telegram-бот «AI-ассистент студента»

Асинхронный Telegram-бот на базе Python 3.12 и aiogram 3, интегрированный с большими языковыми моделями (LLM) через OpenAI-совместимый API и СУБД PostgreSQL для сохранения контекста диалога, выжимок истории и настроек пользователей.

---

## 1. Стек технологий и инструменты

- **Язык и среда:** Python 3.12
- **Пакетный менеджер и виртуальное окружение:** [uv](https://github.com/astral-sh/uv)
- **Telegram фреймворк:** [aiogram 3](https://docs.aiogram.dev/) (асинхронный long polling, роутеры, фильтры, inline-клавиатуры)
- **СУБД и пул соединений:** PostgreSQL 16 (в Docker Compose), асинхронный драйвер `asyncpg` с пулом подключений
- **Миграции базы данных:** собственный асинхронный runner с `pg_advisory_lock` и контрольными суммами SHA-256 (`app/db/migrations/`)
- **LLM-интеграция:** `httpx` (асинхронный HTTP-клиент к OpenAI-совместимому API: Ollama, OpenRouter, vLLM, LocalAI)
- **Схемы и валидация данных:** `pydantic v2` (модели запросов/ответов LLM, сообщений, валидация настроек пользователей)
- **Служебный HTTP-сервер:** `aiohttp` (эндпоинт `/health` на порту 8080 для Docker и оркестраторов)
- **Контейнеризация:** Docker, Docker Compose
- **Тестирование и линтинг:** `pytest`, `pytest-asyncio`, `ruff` (86 тестов)


---

## 2. Принцип работы бота

### Схема обработки запроса пользователя
1. **Приём апдейта:** Пользователь отправляет сообщение в Telegram. `aiogram` направляет его в роутер диалога (`app/handlers/dialog.py`).
2. **Трассировка и UX:** Генерируется уникальный `request_id` (передаётся через `contextvars`), в чат отправляется статус набора текста (`chat_action="typing"`).
3. **Бизнес-логика (`DialogService`):**
   - Получает или создаёт профиль пользователя в таблице `user_settings` (текущая роль и температура).
   - Загружает конфигурацию роли (`BotMode`) из реестра `ModeRegistry` (системный промпт + few-shot примеры + общие правила разметки `TELEGRAM_FORMATTING_PROMPT`).
   - Автоматически определяет размер контекстного окна модели через API (`/api/show` в Ollama или `/models` в OpenRouter/vLLM) с кэшированием и fallback на 32k токенов (32768).
   - **Компактизация истории:** при накоплении сообщений сверх порога (`COMPACTION_THRESHOLD_MESSAGES=10`) старые сообщения сжимаются через вызов LLM в краткую выжимку (summary) и сохраняются в `dialog_summaries`. Сжатые сообщения удаляются из базы, сохраняя последние `COMPACTION_KEEP_RECENT=3` сообщений дословно.
   - Загружает сохранённую выжимку из `dialog_summaries` и свежие сообщения из `dialog_messages`.
   - Собирает единый контекст: системные инструкции роли + выжимка предыдущего контекста + недавние сообщения + текущий вопрос.
   - Отправляет асинхронный POST-запрос в LLM через `OpenAIClient`.
   - Сохраняет вопрос пользователя и ответ модели в `dialog_messages`.
4. **Рендеринг и чанкинг:**
   - Если ответ превышает 4096 символов, функция `split_text` разбивает его на фрагменты по границам строк и абзацев без разрыва кода.
   - Markdown конвертируется через `render_markdown` в безопасный HTML Telegram. При синтаксических сбоях разметки срабатывает fallback на отправку обычным текстом (`parse_mode=None`).

### Режимы работы (роли)
- `/study` — **«Обучение»:** ментор по программированию. Объясняет концепции, приводит лаконичный пример кода и формулирует выводы.
- `/translate` — **«Переводчик»:** технический IT-перевод текстов, документации и кода с сохранением синтаксиса и терминологии.
- `/review` — **«Код-ревью»:** ревью кода с few-shot примерами. Анализирует найденные проблемы, оценивает алгоритмическую сложность (Big O) и предлагает улучшенный вариант реализации.

### Команды бота
- `/start` — приветственное сообщение и интерактивный выбор роли через inline-кнопки.
- `/help` — список доступных ролей и команд бота.
- `/study`, `/translate`, `/review` — быстрое переключение активной роли.
- `/settings` — интерактивный выбор температуры генерации (`0.0`, `0.3`, `0.7`, `1.0`) через inline-кнопки.
- `/reset` — полная очистка контекста пользователя (удаление сообщений из `dialog_messages` и выжимки из `dialog_summaries`).

### Отказоустойчивость
- **Таймауты LLM:** при превышении времени ожидания (`LLM_TIMEOUT`, по умолчанию 180 с) бот отправляет сообщение об ошибке с inline-кнопкой «🔄 Повторить».
- **Маскирование секретов:** фильтр `SecretFilter` автоматически скрывает токен бота, пароли БД и параметры прокси из логов и трейсбеков.
- **Healthcheck:** HTTP-эндпоинт отдаёт `200 OK` только при одновременной доступности Telegram polling и успешном выполнении `SELECT 1` в PostgreSQL.

---

## 3. Структура проекта и описание компонентов

```text
ai-in-sd-bot/
├── app/
│   ├── core/                          # Ядро приложения и независимые утилиты
│   │   ├── schemas/                   # Pydantic-схемы данных
│   │   │   ├── llm.py                 # Модели LLMRequest, LLMResponse, ToolCall
│   │   │   ├── message.py             # Модели ChatMessage, MessageRole
│   │   │   └── user.py                # Модели UserSettings и валидация температур
│   │   ├── context.py                 # Трассировка запросов через contextvars (request_id)
│   │   ├── exceptions.py              # Иерархия доменных исключений приложения
│   │   └── formatters.py              # Конвертер Markdown -> Telegram HTML и безопасная отправка
│   ├── db/                            # Слой персистентности данных (PostgreSQL)
│   │   ├── connection.py              # Создание пула asyncpg, ping и закрытие
│   │   ├── migrations/                # Механизм миграций БД
│   │   │   ├── runner.py              # Раннер с pg_advisory_lock и контрольными суммами SHA-256
│   │   │   └── sql/                   # SQL-файлы миграций
│   │   │       ├── 0001_initial_schema.sql  # Таблицы user_settings и dialog_messages
│   │   │       └── 0002_dialog_summaries.sql # Таблица dialog_summaries для компактизации
│   │   └── repositories/              # Репозитории доступа к данным
│   │       ├── history_repo.py        # Сохранение/выборка сообщений, саммари и удаление
│   │       └── user_repo.py           # Создание/получение настроек пользователей, смена режима/температуры
│   ├── handlers/                      # Слой представления (Aiogram 3 роутеры)
│   │   ├── commands.py                # Обработка /start, /help, /settings, /reset и callback-кнопок
│   │   ├── dialog.py                  # Обработка текстовых сообщений, повторов, split_text
│   │   ├── echo.py                    # Резервный эхо-обработчик
│   │   └── __init__.py                # Объединяющий корневой роутер хэндлеров
│   ├── llm/                           # Слой интеграции с языковыми моделями
│   │   ├── base.py                    # Протокол BaseLLMClient (generate, get_context_length)
│   │   └── client.py                  # HTTP-клиент httpx с автоопределением контекста (Ollama/OpenRouter)
│   ├── modes/                         # Режимы работы ассистента
│   │   ├── base.py                    # Pydantic-модели BotMode и FewShotExample
│   │   ├── definitions.py             # Системные промпты ролей и общий TELEGRAM_FORMATTING_PROMPT
│   │   └── registry.py                # Реестр доступных ролей ModeRegistry
│   ├── services/                      # Слой бизнес-логики
│   │   └── dialog_service.py          # Диалоговый сервис: компактизация, сборка контекста, вызовы LLM
│   ├── config.py                      # Строгая валидация конфигурации из .env и окружения
│   ├── health.py                      # Встроенный HTTP-сервер состояния системы (/health)
│   ├── healthcheck.py                 # CLI-утилита проверки готовности для Docker HEALTHCHECK
│   ├── logging_setup.py               # Настройка цветного логирования и маскирования секретов
│   ├── telegram.py                    # Сессия Bot с поддержкой прокси и авторегистрация команд
│   ├── __init__.py
│   └── __main__.py                    # Главная точка входа, жизненный цикл и graceful shutdown
├── scripts/                           # Вспомогательные скрипты инфраструктуры
│   ├── bootstrap.py                   # Автоматическая настройка виртуального окружения
│   ├── cli.py                         # Главная консольная точка входа для управления проектом
│   ├── cloud.py                       # Управление развёртыванием в Yandex Cloud
│   ├── common.py                      # Общие системные утилиты скриптов
│   ├── local.py                       # Локальный запуск Docker и бота
│   ├── start.sh / start.ps1           # Кроссплатформенные скрипты быстрого локального запуска
│   └── deploy.sh / deploy.ps1         # Кроссплатформенные скрипты деплоя в облако
├── tests/                             # Набор тестов (92 теста)
│   ├── test_app.py                    # Тесты валидации конфигурации, секретов и healthcheck
│   ├── test_bot_scenarios.py          # Сценарные тесты взаимодействия с ботом и компактизации
│   ├── test_commands_registration.py  # Тесты автоматической регистрации команд в Telegram
│   ├── test_compaction_and_context.py # Тесты автоопределения контекста модели и настроек сжатия
│   ├── test_compose.py                # Тесты конфигурации compose.yaml
│   ├── test_formatters.py             # Тесты конвертации Markdown в Telegram HTML
│   ├── test_integration.py            # Интеграционные тесты с реальной СУБД в Docker
│   ├── test_lifecycle.py              # Тесты жизненного цикла приложения и shutdown
│   ├── test_migrations.py             # Тесты целостности файлов миграций и runner
│   ├── test_modes.py                  # Тесты реестра режимов и системных промптов
│   ├── test_queue.py                  # Тесты менеджера очереди сообщений пользователя
│   ├── test_scripts.py                # Тесты инфраструктурных скриптов проекта
│   └── test_storage.py                # Тесты валидации моделей данных и утилиты split_text
├── compose.yaml                       # Конфигурация контейнеров PostgreSQL и сервиса бота
├── pyproject.toml                     # Метаданные проекта, зависимости, настройки pytest и ruff
└── .env.example                       # Шаблон переменных окружения
```

---

## 4. Конфигурация (`.env`)

Создайте файл `.env` в корне проекта на основе `.env.example`:

```dotenv
# Telegram
BOT_TOKEN=123456789:ABCDEF...
TELEGRAM_PROXY_URL=              # Опционально: http://user:pass@host:port или socks5://host:port

# PostgreSQL
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=bot
POSTGRES_USER=bot
POSTGRES_PASSWORD=secret_password

# LLM (OpenAI-совместимый API / Ollama / OpenRouter / vLLM)
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-v1-...         # Для Ollama можно указать произвольное значение, например 'ollama'
LLM_MODEL=openai/gpt-4o-mini     # Или локальная модель в Ollama: qwen2.5-coder:14b, qwen3:14b
LLM_TIMEOUT=180.0

# Лимиты истории диалога и компактизация
HISTORY_MAX_MESSAGES=10
HISTORY_MAX_CHARS=8000
DEFAULT_CONTEXT_LENGTH=32768     # Базовый размер контекста модели (32k) при сбое автоопределения
COMPACTION_THRESHOLD_MESSAGES=10 # Порог количества сообщений для запуска сжатия истории
COMPACTION_KEEP_RECENT=3         # Сколько последних сообщений сохранять дословно при сжатии

# Сервисные параметры
LOG_LEVEL=INFO
HEALTH_PORT=8080
```

---

## 5. Запуск проекта

### Вариант 1: Запуск через `uv` (рекомендуемый)

1. **Установите `uv`** (если ещё не установлен):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Запустите PostgreSQL в Docker:**
   ```bash
   docker compose up -d db
   ```

3. **Запустите бота:**
   ```bash
   uv run python -m app
   ```

`uv` автоматически развернёт виртуальное окружение `.venv`, установит зависимости из `pyproject.toml`, применит миграции и запустит long polling бота.

---

### Вариант 2: Запуск через скрипты автоматизации

**Linux / macOS:**
```bash
bash scripts/start.sh
```

**Windows (PowerShell):**
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Скрипт проверяет наличие Python 3.12, разворачивает `.venv`, запускает контейнер с PostgreSQL и стартует бота.

---

### Вариант 3: Только подготовка окружения для IDE

Если вы хотите запускать и отлаживать бота напрямую из VS Code или PyCharm:
```bash
bash scripts/start.sh --setup-only
```
После этого выберите интерпретатор `.venv/bin/python` (или `.venv\Scripts\python.exe` на Windows) и запускайте модуль `app`.

---

## 6. Проверка работоспособности

1. **Проверка статуса через CLI:**
   ```bash
   uv run python -m app.healthcheck
   ```

2. **Проверка статуса по HTTP:**
   ```bash
   curl http://127.0.0.1:8080/health
   ```
   Ответ при нормальной работе:
   ```json
   {"status": "ok", "database": "ok", "polling": "running"}
   ```

---

## 7. Тестирование и линтинг

- **Запуск всех тестов:**
  ```bash
  uv run pytest -q
  ```
  *(85 passed, 1 skipped)*

- **Запуск интеграционных тестов с реальной БД:**
  ```bash
  RUN_INTEGRATION=1 uv run pytest -m integration -v
  ```

- **Проверка линтером:**
  ```bash
  uv run ruff check .
  ```

- **Автоформатирование кода:**
  ```bash
  uv run ruff format .
  ```

---

## 8. Деплой в Yandex Cloud

Скрипты `scripts/deploy.sh` (Linux/macOS) и `scripts/deploy.ps1` (Windows) автоматизируют развёртывание бота на виртуальной машине в Yandex Cloud:

```bash
# Первый деплой (передайте ваш публичный IP/CIDR для SSH)
bash scripts/deploy.sh up --ssh-cidr 203.0.113.10/32

# Просмотр статуса контейнеров и логов
bash scripts/deploy.sh status
bash scripts/deploy.sh logs

# Остановка и запуск виртуальной машины
bash scripts/deploy.sh stop
bash scripts/deploy.sh start

# Полное удаление облачных ресурсов проекта
bash scripts/deploy.sh destroy
```
