# Telegram-бот «AI-ассистент студента»

Асинхронный Telegram-бот на базе Python 3.12 и aiogram 3, интегрированный с большими языковыми моделями (LLM) через OpenAI-совместимый API и СУБД PostgreSQL для сохранения контекста диалога, выжимок истории и настроек пользователей.

---

## 1. Стек технологий и инструменты

- **Язык и среда:** Python 3.12
- **Пакетный менеджер и виртуальное окружение:** [uv](https://github.com/astral-sh/uv)
- **Telegram фреймворк:** [aiogram 3](https://docs.aiogram.dev/) (асинхронный long polling, роутеры, фильтры, inline-клавиатуры)
- **СУБД и пул соединений:** PostgreSQL 16 (в Docker Compose), асинхронный драйвер `asyncpg` с пулом подключений
- **Миграции базы данных:** собственный асинхронный runner с `pg_advisory_lock` и контрольными суммами SHA-256 (`app/db/migrations/`)
- **LLM-интеграция:** `openai.AsyncOpenAI` (Chat Completions через HTTPX2; настраиваемый OpenAI-совместимый endpoint)
- **Схемы и валидация данных:** `pydantic v2` (модели запросов/ответов LLM, сообщений, валидация настроек пользователей)
- **Форматирование Telegram:** `telegramify-markdown==1.4.1` (текст + entities, готовое разбиение)
- **Служебный HTTP-сервер:** `aiohttp` (эндпоинт `/health` на порту 8080 для Docker и оркестраторов)
- **Контейнеризация:** Docker, Docker Compose
- **Тестирование и линтинг:** `pytest`, `pytest-asyncio`, `ruff`


---

## 2. Принцип работы бота

### Схема обработки запроса пользователя
1. **Приём апдейта:** Пользователь отправляет сообщение в Telegram. `aiogram` направляет его в роутер диалога (`app/handlers/dialog.py`).
2. **Трассировка и UX:** Генерируется уникальный `request_id` (передаётся через `contextvars`), в чат отправляется статус набора текста (`chat_action="typing"`).
3. **Бизнес-логика (`DialogService`):**
   - Получает или создаёт профиль пользователя в таблице `user_settings` (текущая роль и температура).
   - Собирает один RCTF system (Role → Context → Task → Format) с общей политикой, аудиторией ИТМО и правилами Telegram; добавляет максимум один релевантный few-shot пример.
   - Определяет контекст отдельно от генерации: в режиме auto запрашивает metadata только у распознанного Ollama/OpenRouter, кэширует по endpoint и модели; иначе использует `DEFAULT_CONTEXT_LENGTH` (32768). Для нестандартного провайдера задаётся `LLM_CONTEXT_PROVIDER`.
   - **Компактизация истории:** при накоплении сообщений сверх порога (`COMPACTION_THRESHOLD_MESSAGES=10`) старые сообщения сжимаются через вызов LLM в краткую выжимку (summary) и сохраняются в `dialog_summaries`. Сжатые сообщения удаляются из базы, сохраняя последние `COMPACTION_KEEP_RECENT=3` сообщений дословно.
   - Загружает сохранённую выжимку из `dialog_summaries` и свежие сообщения из `dialog_messages`.
   - Собирает контекст: доверенный system + примеры + summary как недоверенное сообщение user + история + вопрос. Старые system/tool-записи истории не становятся системными инструкциями.
   - Вызывает `chat.completions.create` через SDK-адаптер `OpenAIClient`: один клиент на запуск, явные настройки, `max_retries=0`, общий deadline `LLM_TIMEOUT`, закрытие при остановке/ошибке запуска. Скрытый reasoning не используется как ответ.
   - Сохраняет вопрос пользователя и ответ модели в `dialog_messages`.
4. **Рендеринг и чанкинг:**
   - Полный Markdown один раз проходит `telegramify_markdown.convert(latex_escape=False)` с изолированной конфигурацией без emoji у заголовков/ссылок и с маркером списка `-`. Затем `split_entities(..., max_utf16_len=4096)` делит текст и entities; отправка через aiogram всегда использует `parse_mode=None`.
   - Обёртка проверяет UTF-16 offsets, допускает bold/italic/code/pre/text_link и ссылки HTTP(S). Неизвестные entities удаляются с сохранением видимого текста. Собственных парсера, HTML-рендера и splitter нет.
   - При сбое конвертации отправляется исходный текст без entities; при отказе Telegram из-за entities — видимый текст только отклонённой части. Оба plain-text пути также используют `split_entities`; сетевые и остальные API-ошибки не повторяются. Reply-to ставится на первый фрагмент, inline-кнопки — на последний. Пустой результат не отправляется.

### Режимы работы (роли)
- `/study` — **«Обучение»:** ментор студента ИТМО по математике, алгоритмам и ПО. Обосновывает решение, учитывает уровень и нужный язык; код приводит по задаче, по просьбе о подсказке не раскрывает полное решение.
- `/translate` — **«Переводчик»:** математический и технический перевод текстов, документации и кода с сохранением синтаксиса и терминологии.
- `/review` — **«Код-ревью»:** ревью кода с few-shot примерами. Анализирует найденные проблемы, оценивает алгоритмическую сложность (Big O) и предлагает улучшенный вариант реализации.

### Команды бота
- `/start` — приветственное сообщение и интерактивный выбор роли через inline-кнопки.
- `/help` — список доступных ролей и команд бота.
- `/study`, `/translate`, `/review` — быстрое переключение активной роли.
- `/settings` — интерактивный выбор температуры генерации (`0.0`, `0.3`, `0.7`, `1.0`) через inline-кнопки.
- `/reset` — полная очистка контекста пользователя (удаление сообщений из `dialog_messages` и выжимки из `dialog_summaries`).

### Отказоустойчивость
- **Таймауты LLM:** при превышении времени ожидания (`LLM_TIMEOUT`, по умолчанию 180 с) бот отправляет сообщение об ошибке с inline-кнопкой «🔄 Повторить».
- **Маскирование секретов:** фильтр `SecretFilter` автоматически скрывает токен бота, ключ LLM, пароли БД и параметры прокси из логов и трейсбеков.
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
│   │   └── formatters.py              # telegramify-markdown: текст + entities и безопасная отправка
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
│   │   ├── dialog.py                  # Обработка текстовых сообщений, повторов, typing
│   │   ├── echo.py                    # Резервный эхо-обработчик
│   │   └── __init__.py                # Объединяющий корневой роутер хэндлеров
│   ├── llm/                           # Слой интеграции с языковыми моделями
│   │   ├── base.py                    # Протокол BaseLLMClient (generate, get_context_length, aclose)
│   │   ├── client.py                  # SDK-адаптер Chat Completions и tools DTO
│   │   └── context_length.py          # Отдельное определение metadata и fallback
│   ├── modes/                         # Режимы работы ассистента
│   │   ├── base.py                    # Pydantic-модели BotMode и FewShotExample
│   │   ├── definitions.py             # Типизированные RCTF-части режимов и few-shot
│   │   └── registry.py                # Реестр доступных ролей ModeRegistry
│   ├── prompts/                       # Builder, политика, Telegram-контракт и компактизация
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
│   ├── bootstrap.py                   # Подготовка окружения через uv
│   ├── cli.py                         # Главная консольная точка входа для управления проектом
│   ├── cloud.py                       # Управление развёртыванием в Yandex Cloud
│   ├── common.py                      # Общие системные утилиты скриптов
│   ├── local.py                       # Локальный запуск Docker и бота
│   ├── start.sh / start.ps1           # Кроссплатформенные скрипты быстрого локального запуска
│   └── deploy.sh / deploy.ps1         # Кроссплатформенные скрипты деплоя в облако
├── tests/                             # Автотесты и интеграционная проверка PostgreSQL
│   ├── test_app.py                    # Тесты валидации конфигурации, секретов и healthcheck
│   ├── test_bot_scenarios.py          # Сценарные тесты взаимодействия с ботом и компактизации
│   ├── test_commands_registration.py  # Тесты автоматической регистрации команд в Telegram
│   ├── test_compaction_and_context.py # Тесты автоопределения контекста модели и настроек сжатия
│   ├── test_compose.py                # Тесты конфигурации compose.yaml
│   ├── test_formatters.py             # Конвертация, UTF-16 entities, разбиение и fallback
│   ├── test_integration.py            # Интеграционные тесты с реальной СУБД в Docker
│   ├── test_lifecycle.py              # Тесты жизненного цикла приложения и shutdown
│   ├── test_llm_sdk.py                # SDK/HTTPX2, ошибки, tools и отмена
│   ├── test_migrations.py             # Тесты целостности файлов миграций и runner
│   ├── test_modes.py                  # Тесты реестра режимов и системных промптов
│   ├── test_queue.py                  # Тесты менеджера очереди сообщений пользователя
│   ├── test_scripts.py                # Тесты инфраструктурных скриптов проекта
│   └── test_storage.py                # Тесты валидации моделей данных
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
LLM_CONTEXT_PROVIDER=auto       # auto | none | ollama | models

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

2. **Подготовьте окружение:**
   ```bash
   uv sync --locked
   ```
   Python 3.12 выбирается из `.python-version`; при необходимости uv загрузит его.

3. **Запустите PostgreSQL в Docker:**
   ```bash
   docker compose -f compose.yaml -f compose.local.yaml up -d db
   ```

4. **Запустите бота:**
   ```bash
   uv run --locked python -m app
   ```

`uv` создаёт `.venv` и устанавливает зависимости по `uv.lock`. Миграции PostgreSQL
применяет само приложение при запуске перед началом long polling.

Зависимости объявлены в `pyproject.toml`, точные версии зафиксированы в `uv.lock`.
Добавление пакетов: `uv add <package>`, инструментов разработки: `uv add --dev <package>`.
После изменения зависимостей коммитьте оба файла. В CI и Docker используется
`--locked`: несоответствие конфигурации lock-файлу приводит к ошибке.

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

Для обёрток нужны uv в `PATH` и Docker Compose. Скрипт подготавливает окружение
через `uv sync --locked`, запускает PostgreSQL и бота. Системная установка Python
для обёрток не требуется: интерпретатором управляет uv. Запуск возможен из любого каталога.

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
   uv run --locked python -m app.healthcheck
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
  uv run --locked pytest -q
  ```

- **Запуск интеграционных тестов с реальной БД:**
  ```bash
  RUN_INTEGRATION=1 uv run --locked pytest -m integration -v
  ```

- **Проверка линтером:**
  ```bash
  uv run --locked ruff check .
  ```

- **Автоформатирование кода:**
  ```bash
  uv run --locked ruff format .
  ```

---

## 8. Деплой в Yandex Cloud

Docker-образ собирается на Python 3.12 с uv 0.13.1. Runtime-зависимости устанавливаются
из `uv.lock` при сборке без группы `dev`; контейнер запускается от `student` и использует
`/app/.venv/bin/python` для приложения и healthcheck. При старте пакеты не скачиваются.
Для локальной проверки образа: `docker build -t ai-in-sd-bot:uv .`.
Архив развёртывания включает конфигурацию uv, исходники, SQL-миграции и JSON из
`app/data`, исключая `.env`, локальные окружения и ключи.

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


## Подготовка lab2: SDK, промпты и Telegram

Контракты, ограничения и результаты приёмки подготовки описаны в
[плане 05](docs/plans/lab2/05-llm-sdk-prompts-and-telegram.md).
Корневая спецификация lab2 будет подготовлена на этапе 0 плана 04.
Адаптер сериализует tools и результаты, но выполнение инструментов и MCP
остаются задачей плана 04.

Сводка автоматических проверок приведена в плане 05.

Ограничения выбранной библиотеки: один завершающий перенос fenced-блока убирается,
пустые/пробельные части не отправляются; при жёстком разбиении очень длинной строки
составной emoji может попасть в разные сообщения. UTF-16 surrogate pairs и порядок
оставшихся символов сохраняются. Формулы следует писать текстом или code: флаг
`latex_escape=False` отключает преобразование скобочной LaTeX-записи, но собственный
синтаксис библиотеки `$...$` всё ещё обрабатывается как математическая разметка.
