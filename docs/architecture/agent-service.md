# Маршрут сообщения в AgentService

`DialogService` владеет полным пользовательским запросом: очередью, claim/replay,
общим deadline и атомарной записью результата с вопросом и ответом.
`ContextBuilder` собирает историю, выжимку и статус последнего действия.
`AgentService.run()` выполняет обычный цикл LLM tool calling. SQL и Telegram SDK
внутри этого цикла отсутствуют.

При `LLM_STREAMING=true` SSE остаётся внутри OpenAI-совместимого адаптера: SDK
собирает текст и аргументы tool calls, а адаптер проверяет законченный ответ.
Tool calls не исполняются по частичным фрагментам. Telegram получает только
завершённый публичный ответ.

## Ресурс как локальная функция tool calling

В один запрос модели входят обнаруженные MCP tools и локальные функции приложения:
`read_resource(uri)` и `clarification(question)`. Схема `read_resource` создаётся
по текущему каталогу `list_resources`: параметр `uri` имеет enum из обнаруженных
URI. Поэтому модель выбирает ресурс в том же обычном сообщении, где выбирает
погоду, расписание tool или уточнение.

`AgentService` формирует определения двух локальных функций рядом с
`clarification(question)`. Определение `read_resource(uri)` строится из
`ReadResourceArguments` и обнаруженных URI. Вызов всё равно проходит через
общую валидацию всего tool-call batch в `ToolExecutionService`: URI должен
входить в discovery-каталог, а весь batch проверяется до любого RPC.

`read_resource` — host-only функция, а не MCP tool. Сервер публикует ресурс
через `resources/read`, но не включает его в `tools/list`. После валидации
`ToolExecutionService` резервирует вызов и вызывает `MCPClient.read_resource(uri)`.
Клиент использует FastMCP Client resource API; он не отправляет этот запрос как
MCP `tools/call`.

Успешное содержимое добавляется в conversation как сообщение роли `tool` с
оригинальным `tool_call_id`. Содержимое находится в JSON-обёртке с `source`, URI,
MIME-типом и текстом ресурса. И обёртка, и содержимое считаются недоверенными
данными: вложенные инструкции не исполняются. Успешные URI собираются отдельно
в `RunState.resource_uris` и возвращаются в `AgentResult.resource_uris`, в том
числе при ответе, чтении других tools или подготовке напоминания.

Ошибка чтения ресурса преобразуется в tool error content, привязанный к
исходному `tool_call_id`. Следующий обычный шаг LLM решает, уточнить запрос или
ответить по доступным данным. Хост не повторяет ошибочный RPC автоматически;
уже зарезервированный вызов остаётся в бюджете. Ошибки валидации URI и проверки
бюджета возникают до RPC и завершают обработку как обычные ошибки инструмента.
Ошибки нативных MCP tools также остаются терминальными `ToolFailure`; продолжение
после resource RPC не меняет их поведение. Каждый MCP tool read, resource read и
подтверждённое создание напоминания расходует один из двух вызовов события. Всё
проходит через общий предел в четыре шага LLM.

## Основной цикл

```mermaid
flowchart TD
    Handler[Telegram handler] --> Dialog[DialogService.process_user_message]
    Dialog --> Claim[TurnStore.claim]
    Claim --> Replay{Результат claim}
    Replay -->|Replay| Cached[Сохранённый результат]
    Replay -->|InProgress| Busy[Статус обработки]
    Replay -->|Acquired| Context[ContextBuilder.build]
    Context --> Run[AgentService.run и RunState]
    Run --> Request[LLM request: MCP tools + локальные functions]
    Request --> Model[Обычный шаг llm.generate]
    Model --> Handle{Ответ модели}
    Handle -->|Обычный текст| Result[AgentResult]
    Handle -->|clarification(question)| Clarify[Локальный AgentResult: CLARIFY]
    Handle -->|Любой tool call, кроме clarification| Execute[ToolExecutionService.execute]
    Execute --> BatchValidate[Проверить весь batch до RPC]
    BatchValidate -->|read_resource(uri)| ValidateURI[Проверить discovery URI и резерв бюджета]
    BatchValidate -->|Ошибка аргументов, URI или бюджета| ErrorResult
    ValidateURI --> MCPRead[MCPClient.read_resource]
    MCPRead -->|Успех или RPC-ошибка| ResourceTool[Ответ tool с исходным tool_call_id]
    BatchValidate -->|Нативный MCP tool| NativeRead[MCPClient.call_tool]
    NativeRead -->|Успех| ToolResult[Ответ tool с исходным tool_call_id]
    NativeRead -->|ToolFailure| ErrorResult[Терминальная ошибка AgentResult]
    ToolResult --> Continue[StepAction.CONTINUE]
    ResourceTool --> Continue
    ErrorResult --> Result
    Continue --> Request
    Execute --> Prepare[Подготовить add_reminder]
    Prepare --> Result
    Result --> Commit[TurnStore.complete: реплики, результат и аудит]
    Clarify --> Commit
    Commit --> Render[Presentation и отправка]
    Cached --> Render
    Busy --> Render
```

Локальная функция `clarification(question)` превращается в
`AgentResult(CLARIFY)` без сетевого вызова. Уточнение не публикуется MCP-серверу
и не может идти в одном ответе модели с другим вызовом.

После успешного нативного tool result или успешного/ошибочного чтения ресурса
`ToolExecutionService` возвращает `StepAction.CONTINUE`, и агент делает следующий
обычный LLM-шаг. Ошибки нативных MCP tools, валидации и проверки бюджета
завершают цикл через `ToolFailure`. Обычный текст сам завершает цикл.

## Границы ответственности

`ToolExecutionService` проверяет аргументы MCP tools по discovery-схемам через
`MCPClient`, использует `readOnlyHint` и учитывает общий MCP-бюджет. Успешные
read-only tools и локальный `read_resource` обрабатываются в одном потоке
tool calls. Политика приложения разрешает изменение только через
`add_reminder`; неизвестные изменяющие действия блокируются. Напоминание
подготавливается через `ConfirmationService`.

`ToolExecutionService` не принимает `Clock` и не нормализует время. Это делает
`ConfirmationService` по часам приложения и зоне пользователя. Позиция сохранения
предложения равна `0`, так как приложение разрешает одну мутацию на событие.
Существующие записи подтверждений с прежними позициями остаются читаемыми.

Подключение MCP выделено из цикла агента. `create_student_tools_client(settings)`
в `app/mcp/connection.py` настраивает stdio или HTTP FastMCP Client, а `MCPClient`
получает готовый SDK-клиент. В `app/__main__.py` фоновая `_recover_services()`
повторяет discovery каждые 15 секунд только если каталог помечен недоступным.
Каждый RPC использует SDK context manager; его reentrant references управляют
общими сессиями. `available` отражает успешное discovery каталога, а не постоянно
открытый сокет. Timeout отдельного RPC не сбрасывает этот признак и не прерывает
другие запросы; отмена освобождает только контекст текущей операции. Ошибочный
RPC не отправляется повторно.

После подтверждения путь проходит напрямую через
`DialogService.confirm_action()` и `ConfirmationService.confirm()`, а затем через
`MCPClient.call_tool("add_reminder", ...)`. `DialogService` получает каталог
MCP tools через порт `ToolClient`; зависимости от `ToolExecutionService` у него
нет. Клиент вызывает FastMCP tool один раз и не повторяет мутацию при сетевой
ошибке. Неопределённый исход подтверждения сверяется по idempotency key.

Итоговый `AgentResult` содержит успешные `resource_uris`; хранилище replay
сохраняет их вместе с итогом. В `decision_audits.arguments` список URI добавляется
под зарезервированным ключом, отдельно от ограниченных параметров инструмента.
`/why` выводит его как список ресурсов, а не tools. Полный ресурсный текст,
исходный запрос и рассуждения модели в аудит не копируются; ограниченный текст
напоминания хранится как аргумент действия.
