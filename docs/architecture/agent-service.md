# Маршрут сообщения в AgentService

`DialogService` передаёт историю, настройки и доверенный `EventContext` в `run`.
`AgentService` возвращает `AgentResult`; отправка сообщения в Telegram выполняется обработчиком.

Состояние одного вызова хранится в `_RunState`. Оно не разделяется между пользователями.
`_Outcome` связывает результат с причиной для аудита и признаком сохранения.
Общие правила модели вынесены в `app/prompts/agent.py`; интерфейсы инструментов
`ToolExecutor` и `ToolClient` находятся в `app/core/ports.py`.

```mermaid
flowchart TD
    Start["run(messages, context)"] --> Existing{"Есть сохранённое подтверждение?"}
    Existing -->|Да| Return["Вернуть AgentResult в DialogService"]
    Existing -->|Нет| Claim["_claim_event"]
    Claim --> New{"Событие принято в обработку?"}
    New -->|Нет| Replay["_replay_event: результат или статус обработки"]
    Replay --> Return
    New -->|Да| State["_create_state: контекст, правила, схемы, бюджет"]
    State --> Guard["_run_with_errors"]
    Guard --> Loop["_run_steps: ограниченный цикл"]
    Loop --> Generate["llm.generate"]
    Generate --> Response["_handle_response"]
    Response --> Kind{"Что вернула модель?"}
    Kind -->|Текст| Plain["_answer"]
    Kind -->|finish_response| Finish["_finish_response: respond или clarify"]
    Kind -->|Инструменты| Calls["_execute_calls: пакет вызовов"]
    Calls --> Validate["_validate_call: имя, аргументы, бюджет"]
    Validate --> Operation{"Какое действие?"}
    Operation -->|Чтение| Read["_execute_read → _reserve_call → mcp.call_tool"]
    Read --> Location{"Нужно уточнить город?"}
    Location -->|Нет| Next["Добавить tool-результат в контекст"]
    Next --> Remaining{"Остались вызовы в пакете?"}
    Remaining -->|Да| Validate
    Remaining -->|Нет| Loop
    Location -->|Да| Clarify["_location_clarification"]
    Operation -->|Напоминание| Prepare["_prepare_reminder → ConfirmationService.prepare"]
    Prepare --> Outcome["_Outcome: предложение с кнопками"]
    Plain --> Record["_record: аудит и сохранённый результат события"]
    Finish --> Record
    Clarify --> Record
    Outcome --> Record
    Record --> Return
    Calls -.->|dry_run: только проверка и предложение| Return
    Calls -.->|Ошибка аргументов или лимита| SafeError["_run_with_errors: безопасный результат"]
    Loop -.->|Лимит шагов| SafeError
    SafeError --> Record
    Generate -.->|LLMError| ModelError["_record_model_failure"]
    ModelError --> Raise["Передать исключение вызывающему коду"]
```

После чтения успешный промежуточный вызов записывается в аудит с причиной
`read_succeeded`. Обработанное событие завершается только после итогового ответа,
уточнения, предложения подтверждения или контролируемой ошибки.

Создание напоминания после нажатия кнопки проходит отдельным маршрутом:
`handler → DialogService.confirm_action → ConfirmationService.confirm → MCP`.
Вызов `run` только готовит предложение; он не создаёт напоминание до подтверждения.
В режиме `dry_run` запись и подготовка подтверждения не выполняются.
