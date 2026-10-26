# Отчёт по лабораторной работе № 2

**Тема:** Telegram-агент с MCP-инструментами

**Статус:** реализация и технические критерии приёмки плана 04 выполнены 2026-10-26; сдаваемое состояние отмечено тегом `lab2`.

## Цель и результат

Работа расширяет бота lab1 агентным циклом, который выбирает обычный ответ,
уточнение или ограниченный вызов MCP-инструмента. Сервер публикует три инструмента
(`get_weather`, `get_schedule`, `add_reminder`) и ресурс
`schedule://current-week`. Клиент обнаруживает каталог и JSON-схемы через MCP
discovery, проверяет входы и результаты. Схемы строгие: неизвестные поля
отклоняются. Модель вызывается через OpenAI SDK; обмен MCP выполняется MCP SDK.
Обычный текстовый ответ модели без `tool_calls` поддержан как fallback для
совместимости с lab1.

Для одного исходного сообщения установлены пределы: не более двух MCP-вызовов и
четырёх шагов LLM. `/tools` показывает доступные возможности; `/timezone` задаёт
или показывает часовую зону IANA; `/why` показывает последнее маршрутизационное
решение и безопасную ограниченную сводку аргументов. Пользовательский текст не
попадает в текстовые журналы; БД хранит ограниченную сводку аргументов для аудита
и `/why`.

Создание напоминания требует явного подтверждения владельца. Ожидающее действие
привязано к событию, позиции вызова и неизменяемой нагрузке; срок подтверждения
не превышает 300 секунд (настройка по умолчанию — 300). Уникальные ограничения и
ключ идемпотентности защищают от повторов, а зависший `executing` можно безопасно
сверить и восстановить после перезапуска. Парсер времени использует IANA-зону;
неоднозначное и несуществующее локальное время при DST требует уточнения.

## Транспорты и проверка Inspector

Проверены оба транспорта настоящим MCP Inspector 2.12: stdio и Streamable HTTP.
В обоих случаях проверены discovery трёх инструментов и ресурса, чтение расписания
на дату и недели, а также отказ неавторизованного изменения. На отдельной
изолированной Docker PostgreSQL выполнены подтверждённое изменение и повтор
идемпотентного запроса. Использовалась тестовая база; результат не означает
проверку развёртывания в production.

При первой проверке Inspector отклонял структуру ошибки, потому что outputSchema
объявляла только успешный результат. Схема исправлена на строгий union успешного
результата и типизированной ошибки. После этого Inspector принимает оба исхода.
Codex выполнял интеграцию и проверку, субагенты Luna — ограниченные задачи
по репозиториям, инструментам, командам, документации и тестам. Предложение
ограничить outputSchema только успешным результатом было исправлено по
независимой проверке Inspector; вывод AI сам по себе не считался подтверждением.

Для локального Inspector настройте stdio командой `uv run --locked python -m
app.mcp_server --transport stdio` и передайте PostgreSQL-параметры и общий
`MCP_SHARED_SECRET` длиной не менее 32 символов. Для HTTP укажите
`http://127.0.0.1:8000/mcp/` и `Authorization: Bearer <MCP_SHARED_SECRET>`.
Секрет храните только в локальном окружении; не включайте его в конфигурационные
файлы отчёта, скриншоты или опубликованные примеры. Используйте отдельную
тестовую базу для мутаций.

## Матрица обязательных сценариев

В таблице приведены минимальные проверки и соответствующие автоматические тесты.
Имена тестов приведены вместе с файлами, чтобы результаты можно было воспроизвести.

| № | Сценарий методички | Автоматическая проверка |
| --- | --- | --- |
| 1 | Погода существующего города | `tests/test_tools_lab2.py::test_weather_success_filters_country_and_reads_current_conditions` |
| 2 | Город не найден; прогноз не запрашивается | `tests/test_tools_lab2.py::test_weather_not_found_does_not_request_forecast` |
| 3 | Тайм-аут и некорректный погодный ответ | `tests/test_tools_lab2.py::test_weather_timeout_retries_once_then_raises_safe_failure`; `tests/test_tools_lab2.py::test_weather_invalid_api_responses_raise_safe_failure` |
| 4 | Расписание: все поля найденного занятия | `tests/test_tools_lab2.py::test_schedule_reads_empty_day_and_week_across_year_boundary` |
| 5 | Расписание: пустой день | `tests/test_tools_lab2.py::test_schedule_reads_empty_day_and_week_across_year_boundary` |
| 6 | Неверная или несуществующая дата | `tests/test_tools_lab2.py::test_schedule_rejects_invalid_date_before_reading_file` |
| 7 | Создание будущего напоминания после подтверждения | `tests/test_mcp_client_lab2.py::test_stdio_client_discovers_reads_and_confirms_reminder_with_database` |
| 8 | Прошлое время и пустой текст отклоняются | `tests/test_tools_lab2.py::test_normalize_reminder_rejects_past_missing_timezone_and_offset_mismatch`; `tests/test_tools_lab2.py::test_tool_argument_dtos_strip_and_reject_invalid_values` |
| 9 | Идемпотентный повтор создаёт одну запись | `tests/test_lab2_db.py::test_confirmation_prepare_claim_and_reminder_idempotency` |
| 10 | Повтор события и конкурентное подтверждение | `tests/test_lab2_db.py::test_confirmation_prepare_claim_and_reminder_idempotency`; `tests/test_lab2_db.py::test_event_retry_preserves_call_budget_and_caches_result` |
| 11 | Чужие чтение/подтверждение/изменение отклоняются | `tests/test_lab2_db.py::test_confirmation_prepare_claim_and_reminder_idempotency`; `tests/test_mcp_client_lab2.py::test_stdio_client_discovers_reads_and_confirms_reminder_with_database` |
| 12 | Неделя на границе года | `tests/test_tools_lab2.py::test_schedule_reads_empty_day_and_week_across_year_boundary` |
| 13 | Неизвестная IANA-зона и отсутствие сохранённой зоны | `tests/test_tools_lab2.py::test_unknown_iana_zone_is_rejected`; `tests/test_agent_service_lab2.py::test_reminder_without_timezone_returns_clarification_without_mcp` |
| 14 | Неизвестный инструмент и лишние поля | `tests/test_mcp_client_lab2.py::test_call_tool_rejects_unknown_tool_and_extra_input_fields` |

## Негативные сценарии

| Категория | Безопасный ожидаемый результат | Проверка |
| --- | --- | --- |
| Несуществующий инструмент или попытка обойти контракт | Запрос отклонён до MCP; смешанное решение tool и finish также отвергнуто | `tests/test_agent_service_lab2.py::test_unknown_tool_is_rejected_without_mcp_call`; `tests/test_agent_service_lab2.py::test_mixed_finish_decision_and_tool_calls_are_rejected` |
| Инструкция внутри данных инструмента | Данные остаются в роли tool с тем же call ID, не попадают в системную инструкцию | `tests/test_agent_service_lab2.py::test_tool_result_data_is_available_as_tool_input_to_next_llm_step` |
| Прошедшее время или DST-неоднозначность | Напоминание не исполняется до коррекции или уточнения локального времени | `tests/test_tools_lab2.py::test_normalize_reminder_rejects_past_missing_timezone_and_offset_mismatch`; `tests/test_tools_lab2.py::test_normalize_reminder_requests_clarification_for_dst_overlap`; `tests/test_tools_lab2.py::test_normalize_reminder_requests_clarification_for_dst_gap` |
| Недоступный MCP / попытка HTTP-мутации без доверенного контекста | Обычный ответ сохраняется; мутация отклоняется без SQL-изменения | `tests/test_agent_service_lab2.py::test_mcp_unavailable_still_allows_plain_response_and_blocks_tool`; `tests/test_mcp_transports.py::test_streamable_http_transport_auth_and_mcp_contract` |

## Автоматические проверки

Полный набор с реальным PostgreSQL прошёл 246 тестов без пропусков. Ruff check,
проверка формата и diff прошли. Тесты можно запустить командами:

```bash
uv run --locked pytest -q
RUN_INTEGRATION=1 uv run --locked pytest -m integration -v
uv run --locked pytest -q tests/test_mcp_transports.py
```

Unit-тесты подменяют LLM, внешние HTTP-вызовы и PostgreSQL; интеграционный набор
подключается отдельно при `RUN_INTEGRATION=1`. Подтверждённые Inspector-сценарии
использовали отдельную Docker PostgreSQL.

Общий Docker-образ собран для `bot` и `mcp-server`. В отдельном временном
Compose-проекте проверены healthcheck, отсутствие опубликованного MCP-порта
и Telegram/LLM-ключей в сервере, оба транспорта, discovery, чтение ресурса,
создание и повтор подтверждения. Измеренный повторный запуск занял около 0,2 с
для HTTP и 5,1 с для stdio; при холодном запуске под нагрузкой короткий timeout
может привести к временной недоступности, поэтому `MCP_TIMEOUT` настраивается.

На реальной настроенной модели дополнительно пройден полный цикл
LLM → MCP-расписание → LLM, а также подготовка напоминания без записи, явное
подтверждение, создание через MCP и повтор с одной записью в тестовой БД.
[Результат сквозной проверки](evaluation/results/lab2/live-agent-integration-20261026.json).
Проверка восстановления с поздним коммитом использует настоящие блокировки
PostgreSQL: исчерпанный бюджет не превращает уже созданное напоминание в отказ.

## Эксперимент маршрутизации

Модель: `nvidia/nemotron-3-ultra-550b-a55b:free`, temperature=0.0. Время и зона
задаются `evaluation_context` исходного набора, каждый запрос независим.
Ожидания модели не передаются. Создание напоминаний оценивается как подготовка;
запись без подтверждения не выполняется. В JSON сохранены revision, признак dirty,
SHA-256 исходников приложения, модель, настройки и нормализованные аргументы.

| Версия | Routing | Аргументы | Наблюдение |
| --- | --- | --- | --- |
| [Baseline настроенного API](evaluation/results/lab2/lab2-routing-baseline-configured-20261026T091359198804Z.md) | 12/12 | 7/9 | Город переведён, страна Тбилиси потеряна |
| [Первое описание: ошибочные случаи](evaluation/results/lab2/lab2-routing-weather-description-failed-cases-20261026T091442222795Z.md) | 2/2 | 1/2 | Страна Тбилиси сохранена; Петербургу добавлена неназванная страна |
| [Первое описание: весь набор](evaluation/results/lab2/lab2-routing-weather-description-final-20261026T091558304211Z.md) | 11/12 | 6/9 | Лишняя страна в двух запросах, ненужное уточнение напоминания |
| [Точное описание: весь набор](evaluation/results/lab2/lab2-routing-exact-city-description-final-20261026T091905232892Z.md) | **12/12** | **9/9** | Пороги ≥10/12 и ≥8/9 достигнуты |

Между итерациями менялся один элемент маршрутизации — описание `get_weather`.
Первое требование сохранить язык и страну оказалось недостаточно точным:
модель добавляла страну от себя. Итоговое описание требует передавать город
на исходном языке и включать страну/регион только из самого запроса.
Оценщик и исходный набор не менялись, правила сравнения не ослаблялись.
Последующее усиление атомарности восстановления в БД не затрагивает dry-run
маршрутизацию; отдельная сквозная проверка выполнена после него.

[Первый недоступный API](evaluation/results/lab2/lab2-routing-baseline-20261025T215105307193Z.md)
до изменения `.env` не вернул решений; этот результат сохранён как диагностика
доступности и не участвует в сравнении качества другой модели. Все промежуточные
запуски сохранены. Даже при temperature=0 наблюдается вариативность ответов:
результат фиксированного набора не гарантирует правильность произвольного запроса.

Повторный запуск:

```bash
uv run --locked python -m scripts.run_routing_eval --live --label baseline
uv run --locked python -m scripts.run_routing_eval --live --label after-change --cases weather_1 weather_3
```

## Граница lab3

Lab2 создаёт и хранит напоминания с подтверждением. Доставка уведомления в
заданное время, фоновые задачи и проактивная обработка напоминаний относятся к
lab3.
