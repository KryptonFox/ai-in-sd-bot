# План 08. Полный построчный отчёт по изменениям `app`

Документ создан как дополнение к `docs/reports/plan08-mcp-structure.md`.
Он покрывает **каждую** добавленную и удалённую строку `git diff HEAD -- app`
и **каждую** строку нового неотслеживаемого `app/mcp/composite.py`.

## Методика и идентификаторы

- Базовая фиксация: `cbbf8e09ef4c2163fdeee8832da87852aae7d133`.
- Источник изменений: `git diff --unified=0 HEAD -- app`; новый файл —
  `git ls-files --others --exclude-standard app` (`app/mcp/composite.py`).
- Отслеживаемый diff: **465 добавлений / 25 удалений** в 12 файлах.
- Новый файл `app/mcp/composite.py`: **137 добавлений**.
- Итог отчёта: **602 добавления / 25 удалений = 627 строк**.
- Идентификатор `path:+N` означает добавленную строку с текущим номером `N`.
- Идентификатор `path:-N` означает удалённую строку со старым номером `N`.
- В таблицах колонки «Старая» и «Новая» показывают номер в соответствующей
  версии файла, а прочерк — отсутствие строки в этой версии.
- Пустая ячейка кода `<code></code>` означает именно пустую строку; её позиция
  однозначно задана идентификатором.
- Приведён только изменённый текст. Неизменённые соседние строки контекста в
  таблицы не включены и служат только ориентиром в описаниях.
- Миграция БД не требуется: изображения сохраняются в уже существующем JSON
  результата события, а старые записи читаются с пустым списком `images`.
- Исходные правки `docs/plans/README.md`, `plan.md` и
  `docs/plans/lab2/08-math-mcp.md` существовали до этой задачи, не входят в
  `git diff HEAD -- app` и не приписываются реализации.

## Инварианты, которые сохраняет diff

- **Discovery-схемы не копируются.** `ToolCatalog` по-прежнему компилирует
  `input_schema`/`output_schema` из ответа MCP; composite только делегирует
  `validate_arguments` владельцу. Новые строки не содержат ручных JSON-схем
  инструментов.
- **Старые текстовые результаты не меняются.** Если у результата нет
  `ImageContent`, `MCPClient.call_tool` возвращает прежний `dict`; только при
  наличии изображений появляется `PhotoToolResult`.
- **Подтверждения остаются в student_tools.** `__main__` передаёт
  `ConfirmationService` именно `clients.student`, а composite не используется
  для подготовки напоминания.
- **Миграция БД не нужна.** Изображения добавляются в существующий JSONB
  `agent_events.result`; старые записи без поля читаются с `images=[]`.
- **Лимиты изображений действуют на трёх уровнях.** MCP-клиент проверяет
  отдельный PNG и MIME, `ToolExecutionService` — количество/суммарный объём,
  `AgentResult`/`PhotoToolResult` — максимум два элемента.
- **OpenCode-флаг не меняет провайдера.** Добавляются только два заголовка;
  payload, модель и отсутствие thinking/reasoning не меняются.

## Сводка покрытия

| Файл | Добавлено | Удалено | Всего строк отчёта |
| --- | ---: | ---: | ---: |
| `app/__main__.py` | 14 | 10 | 24 |
| `app/config.py` | 15 | 4 | 19 |
| `app/core/ports.py` | 2 | 1 | 3 |
| `app/core/schemas/agent.py` | 3 | 0 | 3 |
| `app/core/schemas/execution.py` | 3 | 0 | 3 |
| `app/core/schemas/tools.py` | 190 | 0 | 190 |
| `app/handlers/dialog.py` | 40 | 2 | 42 |
| `app/llm/client.py` | 22 | 1 | 23 |
| `app/mcp/client.py` | 42 | 4 | 46 |
| `app/mcp/connection.py` | 91 | 0 | 91 |
| `app/services/dialog_service.py` | 3 | 1 | 4 |
| `app/services/tool_execution.py` | 40 | 2 | 42 |
| `app/mcp/composite.py` | 137 | 0 | 137 |
| **Итого** | **602** | **25** | **627** |

## [app/__main__.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py)

Точка сборки приложения. Изменения переключают жизненный цикл с одного студенческого клиента на набор `ToolClients` и составной клиент, а также разделяют восстановление подтверждений и MCP. Строки `-126`, `-131`–`-134`, `-151` относятся к старой сигнатуре/логике; `+127`, `+133`–`+138`, `+155` — к текущей.

Строк в отчёте: **24** (+14 / -10).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/__main__.py:-17` | 17 | — | `from app.mcp.client import MCPClient` | Удалён импорт конкретного MCPClient: в точке сборки приложения теперь используется составной клиент, а не один транспортный клиент. |
| `app/__main__.py:-18` | 18 | — | `from app.mcp.connection import create_student_tools_client` | Удалён импорт старой фабрики create_student_tools_client; её заменила create_tool_clients, возвращающая оба клиента и composite. |
| `app/__main__.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:17) | `from app.mcp.composite import CompositeToolClient` | Импортирует CompositeToolClient — тип, который хранится в приложении и передаётся в ToolExecutionService/DialogService/HealthState. |
| `app/__main__.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:18) | `from app.mcp.connection import create_tool_clients` | Импортирует create_tool_clients — единую фабрику студенческого, математического и составного клиентов. |
| `app/__main__.py:-33` | 33 | — | `    mcp_client = None` | Удалена неаннотированная переменная mcp_client; новая переменная явно ограничена типом composite или None. |
| `app/__main__.py:+33` | — | [33](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:33) | `    mcp_client: CompositeToolClient \| None = None` | Задаёт точный тип mcp_client: CompositeToolClient \| None, чтобы статическая проверка видела контракт жизненного цикла. |
| `app/__main__.py:-43` | 43 | — | `        mcp_client = create_student_tools_client(settings)` | Удалено создание единственного студенческого клиента; теперь фабрика возвращает набор клиентов. |
| `app/__main__.py:+43` | — | [43](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:43) | `        clients = create_tool_clients(settings)` | Создаёт ToolClients: student, необязательный math и composite, сохраняя единую точку конфигурации транспортов. |
| `app/__main__.py:+44` | — | [44](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:44) | `        mcp_client = clients.composite` | Берёт composite из набора; именно он передаётся в общий ToolExecutionService и DialogService. |
| `app/__main__.py:-49` | 49 | — | `            mcp_client,` | Раньше ConfirmationService получал общий mcp-клиент; для подтверждений это больше не нужно. |
| `app/__main__.py:+50` | — | [50](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:50) | `            clients.student,` | Передаёт ConfirmationService непосредственно clients.student, чтобы подтверждённое напоминание всегда уходило в student_tools. |
| `app/__main__.py:-126` | 126 | — | `    mcp: MCPClient,` | Удалена аннотация MCPClient у параметра фонового восстановления; функция теперь работает с composite. |
| `app/__main__.py:+127` | — | [127](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:127) | `    mcp: CompositeToolClient,` | Аннотирует параметр как CompositeToolClient, у которого есть независимый recover(). |
| `app/__main__.py:-131` | 131 | — | `            if not mcp.available:` | Удалена проверка available у одного клиента: composite сам решает, какие серверы требуют повторного discovery. |
| `app/__main__.py:-132` | 132 | — | `                await mcp.start()` | Удалён прямой вызов mcp.start() из цикла восстановления; его заменяет mcp.recover(). |
| `app/__main__.py:-134` | 134 | — | `            logger.warning("agent_recovery_unavailable")` | Старое сообщение агрегировало сбой и подтверждений, и MCP; теперь причины логируются раздельно. |
| `app/__main__.py:+133` | — | [133](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:133) | `            logger.warning("confirmation_recovery_unavailable")` | Новое имя лога относится только к восстановлению подтверждений и не смешивается с ошибками MCP. |
| `app/__main__.py:+134` | — | [134](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:134) | <code></code> | Пустая строка разделяет независимые try-блоки восстановления подтверждений и MCP. |
| `app/__main__.py:+135` | — | [135](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:135) | `        try:` | Открывает отдельный try для восстановления MCP, чтобы сбой одного сервера не скрывал сбой другого. |
| `app/__main__.py:+136` | — | [136](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:136) | `            await mcp.recover()` | Вызывает composite.recover(): повторный discovery выполняется только для недоступных клиентов. |
| `app/__main__.py:+137` | — | [137](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:137) | `        except Exception:` | Перехватывает любые ошибки восстановления MCP, не завершая фоновый цикл. |
| `app/__main__.py:+138` | — | [138](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:138) | `            logger.warning("mcp_recovery_unavailable")` | Логирует факт недоступности восстановления MCP без текста пользователя и секретов. |
| `app/__main__.py:-151` | 151 | — | `    mcp: MCPClient \| None,` | Удалена аннотация MCPClient \| None у параметра shutdown. |
| `app/__main__.py:+155` | — | [155](/home/krypton/Dev/itmo/ai-in-sd-bot/app/__main__.py:155) | `    mcp: CompositeToolClient \| None,` | Shutdown принимает CompositeToolClient \| None; его aclose() закрывает все дочерние клиенты. |

## [app/config.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py)

Настройки. Добавлены общий разбор булевых значений, флаг OpenCode и URL второго MCP-сервера. Удалённые строки `-144`–`-146` и `-234` заменены вызовом `_boolean` и передачей уже готового `bool`.

Строк в отчёте: **19** (+15 / -4).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/config.py:+51` | — | [51](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:51) | `def _boolean(key: str, raw_value: str) -> bool:` | Вводит общий разбор булевых переменных окружения, чтобы LLM_STREAMING и новый флаг OpenCode использовали одну валидацию. |
| `app/config.py:+52` | — | [52](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:52) | `    normalized = raw_value.strip().lower()` | Нормализует значение: обрезает пробелы и приводит к нижнему регистру, сохраняя прежнюю терпимость к TRUE/FALSE. |
| `app/config.py:+53` | — | [53](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:53) | `    if normalized not in {"true", "false", "1", "0"}:` | Проверяет допустимый закрытый набор true/false/1/0; любое другое значение считается ошибкой конфигурации. |
| `app/config.py:+54` | — | [54](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:54) | `        raise ConfigError(f"{key}: укажите true или false.")` | Сообщает имя конкретной переменной в ConfigError, не раскрывая значение и секреты. |
| `app/config.py:+55` | — | [55](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:55) | <code></code> | Пустая строка отделяет проверку от возврата вычисленного значения. |
| `app/config.py:+56` | — | [56](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:56) | `    return normalized in {"true", "1"}` | Возвращает True для true/1 и False для false/0, повторяя прежнюю семантику streaming. |
| `app/config.py:+57` | — | [57](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:57) | <code></code> | Пустая строка завершает helper перед декоратором Settings. |
| `app/config.py:+58` | — | [58](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:58) | <code></code> | Вторая пустая строка обеспечивает PEP 8-разделение двух определений верхнего уровня. |
| `app/config.py:+75` | — | [75](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:75) | `    llm_opencode_session: bool = False` | Добавляет поле llm_opencode_session со значением по умолчанию False: дополнительный заголовок не включается без явного согласия. |
| `app/config.py:+89` | — | [89](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:89) | `    math_mcp_server_url: str = ""` | Добавляет math_mcp_server_url со значением по умолчанию пустой строки; пустое значение сохраняет режим одного MCP-сервера. |
| `app/config.py:-144` | 144 | — | `        streaming = value("LLM_STREAMING", "true").strip().lower()` | Удалён ручной strip/lower для LLM_STREAMING: нормализация перенесена в _boolean. |
| `app/config.py:-145` | 145 | — | `        if streaming not in {"true", "false", "1", "0"}:` | Удалена локальная проверка набора true/false/1/0; её выполняет общий _boolean. |
| `app/config.py:-146` | 146 | — | `            raise ConfigError("LLM_STREAMING: укажите true или false.")` | Удалено отдельное сообщение об ошибке LLM_STREAMING; helper формирует сообщение с именем ключа. |
| `app/config.py:+154` | — | [154](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:154) | `        streaming = _boolean("LLM_STREAMING", value("LLM_STREAMING", "true"))` | Разбирает LLM_STREAMING через _boolean и сразу получает bool вместо строки, которую позже сравнивали с набором. |
| `app/config.py:+155` | — | [155](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:155) | `        opencode_session = _boolean("LLM_OPENCODE_SESSION", value("LLM_OPENCODE_SESSION", "false"))` | Разбирает LLM_OPENCODE_SESSION с тем же набором допустимых значений и значением по умолчанию false. |
| `app/config.py:-234` | 234 | — | `            llm_streaming=streaming in {"true", "1"},` | Удалено повторное преобразование строки streaming в bool при создании Settings. |
| `app/config.py:+243` | — | [243](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:243) | `            llm_streaming=streaming,` | Передаёт уже нормализованный bool llm_streaming без повторного сравнения строк. |
| `app/config.py:+244` | — | [244](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:244) | `            llm_opencode_session=opencode_session,` | Передаёт разобранный флаг llm_opencode_session в Settings. |
| `app/config.py:+263` | — | [263](/home/krypton/Dev/itmo/ai-in-sd-bot/app/config.py:263) | `            math_mcp_server_url=value("MATH_MCP_SERVER_URL").strip(),` | Читает MATH_MCP_SERVER_URL и обрезает пробелы; пустой URL означает отсутствие второго HTTP-клиента. |

## [app/core/ports.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/ports.py)

Порт `ToolExecutor`. Возвращаемый тип расширен, чтобы слой выполнения мог получить структурированный payload и изображения отдельно.

Строк в отчёте: **3** (+2 / -1).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/core/ports.py:+9` | — | [9](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/ports.py:9) | `from app.core.schemas.tools import PhotoToolResult` | Импортирует PhotoToolResult, чтобы порт ToolExecutor мог описать результат с отдельными изображениями. |
| `app/core/ports.py:-19` | 19 | — | `    ) -> dict[str, object]: ...` | Старый контракт call_tool возвращал только dict и не мог представить PNG отдельно от структурированного payload. |
| `app/core/ports.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/ports.py:20) | `    ) -> dict[str, object] \| PhotoToolResult: ...` | Расширяет возвращаемый тип: обычный dict для текстовых инструментов или PhotoToolResult для инструмента с изображениями. |

## [app/core/schemas/agent.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/agent.py)

Схема результата агента. Добавлено ограниченное поле `images`; за счёт значения по умолчанию старые JSON-записи без поля остаются совместимыми.

Строк в отчёте: **3** (+3 / -0).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/core/schemas/agent.py:+6` | — | [6](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/agent.py:6) | `from app.core.schemas.tools import MAX_AGENT_IMAGES, ToolImage` | Импортирует предел изображений и модель ToolImage для поля результата агента. |
| `app/core/schemas/agent.py:+7` | — | [7](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/agent.py:7) | <code></code> | Пустая строка отделяет сторонний импорт от локального импорта схем. |
| `app/core/schemas/agent.py:+38` | — | [38](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/agent.py:38) | `    images: list[ToolImage] = Field(default_factory=list, max_length=MAX_AGENT_IMAGES)` | Добавляет images в AgentResult с пустым списком по умолчанию и жёстким max_length=2; старые записи без поля читаются как []. |

## [app/core/schemas/execution.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/execution.py)

Состояние одного агентного запуска. Изображения накапливаются в `RunState` и переносятся в `AgentResult` при построении результата.

Строк в отчёте: **3** (+3 / -0).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/core/schemas/execution.py:+6` | — | [6](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/execution.py:6) | `from app.core.schemas.tools import ToolImage` | Импортирует ToolImage, чтобы RunState мог накапливать изображения в пределах одного запуска. |
| `app/core/schemas/execution.py:+21` | — | [21](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/execution.py:21) | `    images: list[ToolImage] = field(default_factory=list)` | Добавляет изменяемый список images в RunState; он живёт только в памяти агентного цикла. |
| `app/core/schemas/execution.py:+44` | — | [44](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/execution.py:44) | `            images=list(self.images),` | Копирует накопленные изображения в AgentResult при построении финального результата. |

## [app/core/schemas/tools.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py)

Новый слой проверки PNG и модели изображения результата. Все 190 строк — добавления; удалённых строк в файле нет. Блоки: импорты и лимиты, расчёт размера пиксельного потока, проверка zlib/Pillow, структурный разбор чанков, строгий base64 и модели `ToolImage`/`PhotoToolResult`.

Строк в отчёте: **190** (+190 / -0).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/core/schemas/tools.py:+1` | — | [1](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:1) | `import base64` | Импортирует base64 для строгого декодирования и канонической пере-кодировки PNG data URL. |
| `app/core/schemas/tools.py:+2` | — | [2](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:2) | `import binascii` | Импортирует binascii, чтобы отличать ошибки base64 от прочих ValueError. |
| `app/core/schemas/tools.py:+3` | — | [3](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:3) | `import io` | Импортирует io для передачи байтов PNG в Pillow без временных файлов. |
| `app/core/schemas/tools.py:+4` | — | [4](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:4) | `import warnings` | Импортирует warnings, чтобы подавить только предупреждение Pillow о decompression bomb после собственных проверок размеров. |
| `app/core/schemas/tools.py:+5` | — | [5](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:5) | `import zlib` | Импортирует zlib для проверки CRC чанков и точного размера распакованных пиксельных данных. |
| `app/core/schemas/tools.py:+10` | — | [10](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:10) | `from PIL import Image` | Импортирует Pillow Image для финальной проверки, что поток действительно декодируется как PNG. |
| `app/core/schemas/tools.py:+14` | — | [14](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:14) | `PNG_MIME_TYPE = "image/png"` | Фиксирует единственный разрешённый MIME-тип изображения результата. |
| `app/core/schemas/tools.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:15) | `PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"` | Хранит восьмибайтовую подпись PNG для быстрой проверки начала файла. |
| `app/core/schemas/tools.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:16) | `MAX_IMAGE_BYTES = 256 * 1024` | Ограничивает одно изображение 256 КиБ — тем же лимитом, что использует math_plot. |
| `app/core/schemas/tools.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:17) | `MAX_IMAGE_PIXELS = 8 * 1024 * 1024` | Ограничивает изображение 8 388 608 пикселями, защищая от маленьких по байтам, но огромных полотен. |
| `app/core/schemas/tools.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:18) | `MAX_IMAGE_DIMENSION = 8192` | Ограничивает каждую сторону 8192 пикселями до вызова Pillow. |
| `app/core/schemas/tools.py:+19` | — | [19](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:19) | `MAX_DECODED_IMAGE_BYTES = 32 * 1024 * 1024` | Ограничивает распакованный пиксельный поток 32 МиБ, чтобы zlib-бомба не съела память. |
| `app/core/schemas/tools.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:20) | `MAX_AGENT_IMAGES = 2` | Разрешает не более двух изображений в одном результате/ходе агента. |
| `app/core/schemas/tools.py:+21` | — | [21](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:21) | `MAX_AGENT_IMAGE_BYTES = MAX_IMAGE_BYTES * MAX_AGENT_IMAGES` | Задаёт суммарный лимит 512 КиБ для двух изображений агентного результата. |
| `app/core/schemas/tools.py:+22` | — | [22](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:22) | `MAX_BASE64_LENGTH = ((MAX_IMAGE_BYTES + 2) // 3) * 4` | Вычисляет максимальную длину canonical base64 для 256 КиБ, отсекая гигантские строки до декодирования. |
| `app/core/schemas/tools.py:+23` | — | [23](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:23) | `_PNG_COLOR_DEPTHS: dict[int, frozenset[int]] = {` | Начинает таблицу допустимых глубин цвета для каждого PNG color type. |
| `app/core/schemas/tools.py:+24` | — | [24](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:24) | `    0: frozenset({1, 2, 4, 8, 16}),` | Для grayscale (type 0) разрешает 1/2/4/8/16 бит на канал. |
| `app/core/schemas/tools.py:+25` | — | [25](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:25) | `    2: frozenset({8, 16}),` | Для truecolor (type 2) разрешает 8 и 16 бит на канал. |
| `app/core/schemas/tools.py:+26` | — | [26](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:26) | `    3: frozenset({1, 2, 4, 8}),` | Для indexed color (type 3) разрешает 1/2/4/8 бит на индекс. |
| `app/core/schemas/tools.py:+27` | — | [27](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:27) | `    4: frozenset({8, 16}),` | Для grayscale+alpha (type 4) разрешает 8 и 16 бит. |
| `app/core/schemas/tools.py:+28` | — | [28](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:28) | `    6: frozenset({8, 16}),` | Для RGBA (type 6) разрешает 8 и 16 бит. |
| `app/core/schemas/tools.py:+29` | — | [29](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:29) | `}` | Закрывает таблицу допустимых цветовых глубин. |
| `app/core/schemas/tools.py:+30` | — | [30](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:30) | `_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}` | Сопоставляет PNG color type числу каналов, чтобы вычислить размер строки. |
| `app/core/schemas/tools.py:+31` | — | [31](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:31) | `_ADAM7_PASSES = (` | Начинает таблицу семи проходов Adam7 с их смещениями и шагами. |
| `app/core/schemas/tools.py:+32` | — | [32](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:32) | `    (0, 0, 8, 8),` | Первый проход Adam7: старт (0,0), шаг 8x8. |
| `app/core/schemas/tools.py:+33` | — | [33](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:33) | `    (4, 0, 8, 8),` | Второй проход Adam7: старт (4,0), шаг 8x8. |
| `app/core/schemas/tools.py:+34` | — | [34](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:34) | `    (0, 4, 4, 8),` | Третий проход Adam7: старт (0,4), шаг 4x8. |
| `app/core/schemas/tools.py:+35` | — | [35](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:35) | `    (2, 0, 4, 4),` | Четвёртый проход Adam7: старт (2,0), шаг 4x4. |
| `app/core/schemas/tools.py:+36` | — | [36](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:36) | `    (0, 2, 2, 4),` | Пятый проход Adam7: старт (0,2), шаг 2x4. |
| `app/core/schemas/tools.py:+37` | — | [37](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:37) | `    (1, 0, 2, 2),` | Шестой проход Adam7: старт (1,0), шаг 2x2. |
| `app/core/schemas/tools.py:+38` | — | [38](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:38) | `    (0, 1, 1, 2),` | Седьмой проход Adam7: старт (0,1), шаг 1x2. |
| `app/core/schemas/tools.py:+39` | — | [39](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:39) | `)` | Закрывает таблицу проходов Adam7. |
| `app/core/schemas/tools.py:+84` | — | [84](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:84) | `def _png_pixel_data_size(` | Начинает расчёт точного размера распакованного пиксельного потока PNG по IHDR. |
| `app/core/schemas/tools.py:+85` | — | [85](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:85) | `    width: int,` | Параметр width — ширина изображения из IHDR. |
| `app/core/schemas/tools.py:+86` | — | [86](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:86) | `    height: int,` | Параметр height — высота изображения из IHDR. |
| `app/core/schemas/tools.py:+87` | — | [87](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:87) | `    bit_depth: int,` | Параметр bit_depth — бит на канал/индекс из IHDR. |
| `app/core/schemas/tools.py:+88` | — | [88](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:88) | `    color_type: int,` | Параметр color_type — PNG color type из IHDR. |
| `app/core/schemas/tools.py:+89` | — | [89](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:89) | `    interlace_method: int,` | Параметр interlace_method — 0 (none) или 1 (Adam7). |
| `app/core/schemas/tools.py:+90` | — | [90](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:90) | `) -> int:` | Закрывает сигнатуру и объявляет возврат целого размера в байтах. |
| `app/core/schemas/tools.py:+91` | — | [91](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:91) | `    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth` | Считает бит на пиксель как число каналов, умноженное на глубину. |
| `app/core/schemas/tools.py:+92` | — | [92](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:92) | <code></code> | Пустая строка отделяет вычисление bits_per_pixel от ветвления по interlace. |
| `app/core/schemas/tools.py:+93` | — | [93](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:93) | `    if interlace_method == 0:` | Выбирает простой расчёт для неизочередного PNG. |
| `app/core/schemas/tools.py:+94` | — | [94](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:94) | `        row_bytes = (width * bits_per_pixel + 7) // 8` | Округляет биты строки вверх до целых байт. |
| `app/core/schemas/tools.py:+95` | — | [95](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:95) | `        return height * (1 + row_bytes)` | Умножает строку на высоту и добавляет по одному filter-байту на строку. |
| `app/core/schemas/tools.py:+96` | — | [96](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:96) | <code></code> | Пустая строка отделяет non-interlaced return от ветки Adam7. |
| `app/core/schemas/tools.py:+97` | — | [97](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:97) | `    total = 0` | Инициализирует суммарный размер для всех проходов Adam7. |
| `app/core/schemas/tools.py:+98` | — | [98](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:98) | `    for x_start, y_start, x_step, y_step in _ADAM7_PASSES:` | Перебирает семь проходов Adam7 с их координатами и шагами. |
| `app/core/schemas/tools.py:+99` | — | [99](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:99) | `        pass_width = (width - x_start + x_step - 1) // x_step if width > x_start else 0` | Вычисляет ширину прохода с округлением вверх; вне изображения даёт 0. |
| `app/core/schemas/tools.py:+100` | — | [100](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:100) | `        pass_height = (height - y_start + y_step - 1) // y_step if height > y_start else 0` | Вычисляет высоту прохода с округлением вверх; вне изображения даёт 0. |
| `app/core/schemas/tools.py:+101` | — | [101](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:101) | `        if pass_width == 0 or pass_height == 0:` | Пропускает проходы, не покрывающие ни одного пикселя. |
| `app/core/schemas/tools.py:+102` | — | [102](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:102) | `            continue` | Переходит к следующему проходу Adam7. |
| `app/core/schemas/tools.py:+103` | — | [103](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:103) | <code></code> | Пустая строка отделяет проверку пустого прохода от расчёта его строки. |
| `app/core/schemas/tools.py:+104` | — | [104](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:104) | `        row_bytes = (pass_width * bits_per_pixel + 7) // 8` | Округляет ширину прохода в битах до целых байт. |
| `app/core/schemas/tools.py:+105` | — | [105](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:105) | `        total += pass_height * (1 + row_bytes)` | Добавляет к сумме строки прохода с filter-байтом на каждую строку. |
| `app/core/schemas/tools.py:+106` | — | [106](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:106) | <code></code> | Пустая строка отделяет цикл от возврата итогового размера. |
| `app/core/schemas/tools.py:+107` | — | [107](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:107) | `    return total` | Возвращает ожидаемый размер распакованных данных для проверки zlib-потока. |
| `app/core/schemas/tools.py:+108` | — | [108](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:108) | <code></code> | Пустая строка завершает функцию расчёта размера. |
| `app/core/schemas/tools.py:+109` | — | [109](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:109) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/core/schemas/tools.py:+110` | — | [110](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:110) | `def _validate_png_stream(data: bytes, expected_size: int) -> None:` | Начинает проверку, что IDAT распаковывается ровно в ожидаемое число байт. |
| `app/core/schemas/tools.py:+111` | — | [111](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:111) | `    decompressor = zlib.decompressobj()` | Создаёт потоковый декомпрессор zlib с контролем границ. |
| `app/core/schemas/tools.py:+112` | — | [112](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:112) | <code></code> | Пустая строка отделяет создание декомпрессора от try-блока. |
| `app/core/schemas/tools.py:+113` | — | [113](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:113) | `    try:` | Открывает обработку ошибок сжатого потока. |
| `app/core/schemas/tools.py:+114` | — | [114](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:114) | `        decoded = decompressor.decompress(data, expected_size + 1)` | Распаковывает не более expected_size+1 байта, чтобы обнаружить лишние данные без роста памяти. |
| `app/core/schemas/tools.py:+115` | — | [115](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:115) | `    except zlib.error:` | Перехватывает zlib.error при повреждённом deflate-потоке. |
| `app/core/schemas/tools.py:+116` | — | [116](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:116) | `        raise ValueError("invalid PNG pixel data") from None` | Преобразует ошибку zlib в единое ValueError без утечки внутренних деталей. |
| `app/core/schemas/tools.py:+117` | — | [117](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:117) | <code></code> | Пустая строка отделяет try/except от проверки результата. |
| `app/core/schemas/tools.py:+118` | — | [118](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:118) | `    if len(decoded) != expected_size or decompressor.unused_data or not decompressor.eof:` | Требует точный размер, отсутствие хвоста и корректный конец zlib-потока. |
| `app/core/schemas/tools.py:+119` | — | [119](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:119) | `        raise ValueError("invalid PNG pixel data")` | Отклоняет поток с неверным размером, лишними данными или без eof. |
| `app/core/schemas/tools.py:+120` | — | [120](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:120) | <code></code> | Пустая строка завершает проверку потока. |
| `app/core/schemas/tools.py:+121` | — | [121](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:121) | <code></code> | Вторая пустая строка разделяет функции проверки PNG. |
| `app/core/schemas/tools.py:+122` | — | [122](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:122) | `def _validate_png_image(raw: bytes) -> None:` | Начинает финальную проверку PNG средствами Pillow после структурных проверок. |
| `app/core/schemas/tools.py:+123` | — | [123](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:123) | `    try:` | Открывает try, чтобы любые ошибки Pillow превратить в единое ValueError. |
| `app/core/schemas/tools.py:+124` | — | [124](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:124) | `        with warnings.catch_warnings():` | Локально подавляет предупреждения Pillow, не меняя глобальные настройки процесса. |
| `app/core/schemas/tools.py:+125` | — | [125](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:125) | `            warnings.simplefilter("ignore", Image.DecompressionBombWarning)` | Игнорирует только DecompressionBombWarning: размеры уже ограничены вручную. |
| `app/core/schemas/tools.py:+126` | — | [126](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:126) | `            with Image.open(io.BytesIO(raw)) as image:` | Открывает PNG из памяти через io.BytesIO. |
| `app/core/schemas/tools.py:+127` | — | [127](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:127) | `                if image.format != "PNG":` | Проверяет, что Pillow определил формат именно как PNG. |
| `app/core/schemas/tools.py:+128` | — | [128](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:128) | `                    raise ValueError("image data must be a PNG file")` | Отклоняет поток, который Pillow распознал как другой формат. |
| `app/core/schemas/tools.py:+129` | — | [129](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:129) | `                image.load()` | Принудительно декодирует пиксели, выявляя повреждённые IDAT/фильтры. |
| `app/core/schemas/tools.py:+130` | — | [130](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:130) | `    except Exception as error:` | Перехватывает любые исключения Pillow или декодирования. |
| `app/core/schemas/tools.py:+131` | — | [131](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:131) | `        raise ValueError("image data must be a valid PNG file") from error` | Нормализует ошибку в ValueError с безопасным текстом без деталей изображения. |
| `app/core/schemas/tools.py:+132` | — | [132](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:132) | <code></code> | Пустая строка завершает проверку Pillow. |
| `app/core/schemas/tools.py:+133` | — | [133](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:133) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/core/schemas/tools.py:+134` | — | [134](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:134) | `def _validate_png(raw: bytes) -> None:` | Начинает структурный разбор PNG по чанкам до передачи в Pillow. |
| `app/core/schemas/tools.py:+135` | — | [135](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:135) | `    if not raw.startswith(PNG_SIGNATURE):` | Проверяет восьмибайтовую подпись PNG в начале файла. |
| `app/core/schemas/tools.py:+136` | — | [136](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:136) | `        raise ValueError("image data must be a PNG file")` | Отклоняет поток без корректной подписи PNG. |
| `app/core/schemas/tools.py:+137` | — | [137](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:137) | <code></code> | Пустая строка отделяет проверку подписи от разбора чанков. |
| `app/core/schemas/tools.py:+138` | — | [138](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:138) | `    offset = len(PNG_SIGNATURE)` | Устанавливает смещение сразу после подписи. |
| `app/core/schemas/tools.py:+139` | — | [139](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:139) | `    header: tuple[int, int, int, int, int] \| None = None` | Хранит разобранный IHDR как (width, height, bit_depth, color_type, interlace) или None. |
| `app/core/schemas/tools.py:+140` | — | [140](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:140) | `    image_data: list[bytes] = []` | Накапливает payload всех IDAT-чанков для совместной распаковки. |
| `app/core/schemas/tools.py:+141` | — | [141](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:141) | `    seen_end = False` | Флаг обязательного IEND; без него файл считается неполным. |
| `app/core/schemas/tools.py:+142` | — | [142](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:142) | <code></code> | Пустая строка отделяет состояние разбора от цикла по чанкам. |
| `app/core/schemas/tools.py:+143` | — | [143](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:143) | `    while offset < len(raw):` | Идёт по чанкам, пока смещение не достигнет конца файла. |
| `app/core/schemas/tools.py:+144` | — | [144](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:144) | `        if offset + 12 > len(raw):` | Проверяет, что в файле есть минимум 12 байт заголовка чанка (length+type+crc). |
| `app/core/schemas/tools.py:+145` | — | [145](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:145) | `            raise ValueError("truncated PNG chunk")` | Отклоняет обрезанный заголовок чанка. |
| `app/core/schemas/tools.py:+146` | — | [146](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:146) | `        length = int.from_bytes(raw[offset : offset + 4], "big")` | Читает big-endian длину payload чанка. |
| `app/core/schemas/tools.py:+147` | — | [147](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:147) | `        chunk_type = raw[offset + 4 : offset + 8]` | Читает четырёхбайтовый тип чанка. |
| `app/core/schemas/tools.py:+148` | — | [148](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:148) | `        chunk_end = offset + 12 + length` | Вычисляет конец чанка: длина, тип, payload и CRC. |
| `app/core/schemas/tools.py:+149` | — | [149](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:149) | `        if chunk_end > len(raw):` | Проверяет, что заявленный чанк целиком помещается в файл. |
| `app/core/schemas/tools.py:+150` | — | [150](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:150) | `            raise ValueError("truncated PNG chunk")` | Отклоняет чанк, выходящий за границы файла. |
| `app/core/schemas/tools.py:+151` | — | [151](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:151) | `        if not chunk_type.isalpha():` | Требует, чтобы тип чанка состоял только из ASCII-букв. |
| `app/core/schemas/tools.py:+152` | — | [152](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:152) | `            raise ValueError("invalid PNG chunk type")` | Отклоняет небуквенный тип чанка. |
| `app/core/schemas/tools.py:+153` | — | [153](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:153) | <code></code> | Пустая строка отделяет заголовок чанка от его payload. |
| `app/core/schemas/tools.py:+154` | — | [154](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:154) | `        payload = raw[offset + 8 : offset + 8 + length]` | Извлекает payload чанка без заголовка и CRC. |
| `app/core/schemas/tools.py:+155` | — | [155](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:155) | `        expected_crc = int.from_bytes(raw[offset + 8 + length : chunk_end], "big")` | Читает ожидаемый CRC как big-endian uint32. |
| `app/core/schemas/tools.py:+156` | — | [156](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:156) | `        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:` | Сверяет zlib.crc32(type+payload), приведённый к uint32, с CRC файла. |
| `app/core/schemas/tools.py:+157` | — | [157](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:157) | `            raise ValueError("PNG chunk checksum mismatch")` | Отклоняет PNG с повреждённой контрольной суммой любого чанка. |
| `app/core/schemas/tools.py:+158` | — | [158](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:158) | <code></code> | Пустая строка отделяет CRC от диспетчеризации по типу чанка. |
| `app/core/schemas/tools.py:+159` | — | [159](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:159) | `        if header is None:` | Обрабатывает первый чанк, который обязан быть IHDR. |
| `app/core/schemas/tools.py:+160` | — | [160](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:160) | `            if chunk_type != b"IHDR" or length != 13:` | Требует, чтобы первый чанк был IHDR ровно из 13 байт. |
| `app/core/schemas/tools.py:+161` | — | [161](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:161) | `                raise ValueError("PNG must start with an IHDR chunk")` | Отклоняет файл, начинающийся не с IHDR нужной длины. |
| `app/core/schemas/tools.py:+162` | — | [162](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:162) | `            width = int.from_bytes(payload[0:4], "big")` | Читает ширину из первых четырёх байт IHDR. |
| `app/core/schemas/tools.py:+163` | — | [163](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:163) | `            height = int.from_bytes(payload[4:8], "big")` | Читает высоту из следующих четырёх байт IHDR. |
| `app/core/schemas/tools.py:+164` | — | [164](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:164) | `            bit_depth = payload[8]` | Читает глубину цвета из IHDR. |
| `app/core/schemas/tools.py:+165` | — | [165](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:165) | `            color_type = payload[9]` | Читает PNG color type из IHDR. |
| `app/core/schemas/tools.py:+166` | — | [166](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:166) | `            compression_method = payload[10]` | Читает метод сжатия (должен быть 0). |
| `app/core/schemas/tools.py:+167` | — | [167](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:167) | `            filter_method = payload[11]` | Читает метод фильтрации (должен быть 0). |
| `app/core/schemas/tools.py:+168` | — | [168](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:168) | `            interlace_method = payload[12]` | Читает метод чересстрочности (0 или 1). |
| `app/core/schemas/tools.py:+169` | — | [169](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:169) | `            if width == 0 or height == 0:` | Запрещает нулевые ширину и высоту. |
| `app/core/schemas/tools.py:+170` | — | [170](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:170) | `                raise ValueError("PNG dimensions must be positive")` | Отклоняет изображение с нулевым измерением. |
| `app/core/schemas/tools.py:+171` | — | [171](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:171) | `            if bit_depth not in _PNG_COLOR_DEPTHS.get(color_type, frozenset()):` | Проверяет глубину цвета по таблице допустимых значений для данного color type. |
| `app/core/schemas/tools.py:+172` | — | [172](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:172) | `                raise ValueError("unsupported PNG color depth")` | Отклоняет неизвестный color type или недопустимую для него глубину. |
| `app/core/schemas/tools.py:+173` | — | [173](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:173) | `            if compression_method != 0 or filter_method != 0 or interlace_method not in {0, 1}:` | Требует стандартные compression/filter методы и interlace только 0 или 1. |
| `app/core/schemas/tools.py:+174` | — | [174](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:174) | `                raise ValueError("unsupported PNG encoding")` | Отклоняет PNG с неподдерживаемым способом кодирования. |
| `app/core/schemas/tools.py:+175` | — | [175](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:175) | `            header = (width, height, bit_depth, color_type, interlace_method)` | Сохраняет проверенный IHDR для последующих расчётов. |
| `app/core/schemas/tools.py:+176` | — | [176](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:176) | `        elif chunk_type == b"IHDR":` | Обнаруживает повторный IHDR после первого. |
| `app/core/schemas/tools.py:+177` | — | [177](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:177) | `            raise ValueError("duplicate PNG header")` | Отклоняет PNG с дублирующимся заголовком. |
| `app/core/schemas/tools.py:+178` | — | [178](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:178) | `        elif chunk_type == b"IDAT":` | Обрабатывает чанк IDAT с пиксельными данными. |
| `app/core/schemas/tools.py:+179` | — | [179](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:179) | `            image_data.append(payload)` | Добавляет payload IDAT в список для склейки и распаковки. |
| `app/core/schemas/tools.py:+180` | — | [180](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:180) | `        elif chunk_type == b"IEND":` | Обрабатывает финальный чанк IEND. |
| `app/core/schemas/tools.py:+181` | — | [181](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:181) | `            if length != 0:` | Требует нулевую длину payload у IEND. |
| `app/core/schemas/tools.py:+182` | — | [182](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:182) | `                raise ValueError("invalid PNG end chunk")` | Отклоняет IEND с непустым payload. |
| `app/core/schemas/tools.py:+183` | — | [183](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:183) | `            if chunk_end != len(raw):` | Требует, чтобы IEND заканчивался ровно на конце файла. |
| `app/core/schemas/tools.py:+184` | — | [184](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:184) | `                raise ValueError("unexpected data after PNG end")` | Отклоняет данные после IEND. |
| `app/core/schemas/tools.py:+185` | — | [185](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:185) | `            seen_end = True` | Отмечает, что обязательный IEND найден. |
| `app/core/schemas/tools.py:+186` | — | [186](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:186) | `            break` | Прерывает цикл после корректного IEND. |
| `app/core/schemas/tools.py:+187` | — | [187](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:187) | <code></code> | Пустая строка отделяет обработку IEND от перехода к следующему чанку. |
| `app/core/schemas/tools.py:+188` | — | [188](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:188) | `        offset = chunk_end` | Сдвигает смещение на конец текущего чанка. |
| `app/core/schemas/tools.py:+189` | — | [189](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:189) | <code></code> | Пустая строка завершает цикл по чанкам. |
| `app/core/schemas/tools.py:+190` | — | [190](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:190) | `    if header is None or not image_data or not seen_end:` | Проверяет наличие IHDR, хотя бы одного IDAT и IEND. |
| `app/core/schemas/tools.py:+191` | — | [191](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:191) | `        raise ValueError("incomplete PNG file")` | Отклоняет неполный PNG без обязательных чанков. |
| `app/core/schemas/tools.py:+192` | — | [192](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:192) | <code></code> | Пустая строка отделяет проверку полноты от ограничений размеров. |
| `app/core/schemas/tools.py:+193` | — | [193](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:193) | `    width, height, bit_depth, color_type, interlace_method = header` | Распаковывает сохранённый кортеж IHDR в локальные переменные. |
| `app/core/schemas/tools.py:+194` | — | [194](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:194) | `    if (` | Открывает многстрочную проверку предельных размеров. |
| `app/core/schemas/tools.py:+195` | — | [195](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:195) | `        width > MAX_IMAGE_DIMENSION` | Проверяет ширину против 8192 пикселей. |
| `app/core/schemas/tools.py:+196` | — | [196](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:196) | `        or height > MAX_IMAGE_DIMENSION` | Проверяет высоту против 8192 пикселей. |
| `app/core/schemas/tools.py:+197` | — | [197](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:197) | `        or width * height > MAX_IMAGE_PIXELS` | Проверяет общее число пикселей против 8 МиП. |
| `app/core/schemas/tools.py:+198` | — | [198](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:198) | `    ):` | Закрывает условие размеров. |
| `app/core/schemas/tools.py:+199` | — | [199](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:199) | `        raise ValueError("PNG dimensions exceed the allowed limit")` | Отклоняет PNG, превышающий лимиты размеров. |
| `app/core/schemas/tools.py:+200` | — | [200](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:200) | <code></code> | Пустая строка отделяет проверку размеров от расчёта распакованного объёма. |
| `app/core/schemas/tools.py:+201` | — | [201](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:201) | `    expected_size = _png_pixel_data_size(width, height, bit_depth, color_type, interlace_method)` | Вычисляет ожидаемый размер пиксельного потока с учётом interlace. |
| `app/core/schemas/tools.py:+202` | — | [202](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:202) | `    if expected_size > MAX_DECODED_IMAGE_BYTES:` | Сравнивает ожидаемый распакованный объём с лимитом 32 МиБ. |
| `app/core/schemas/tools.py:+203` | — | [203](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:203) | `        raise ValueError("PNG pixel data exceeds the allowed limit")` | Отклоняет PNG, который распаковывается больше допустимого. |
| `app/core/schemas/tools.py:+204` | — | [204](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:204) | <code></code> | Пустая строка отделяет расчёт от фактической распаковки. |
| `app/core/schemas/tools.py:+205` | — | [205](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:205) | `    _validate_png_stream(b"".join(image_data), expected_size)` | Проверяет zlib-поток IDAT на точный размер и завершённость. |
| `app/core/schemas/tools.py:+206` | — | [206](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:206) | `    _validate_png_image(raw)` | Передаёт PNG в Pillow для финального декодирования пикселей. |
| `app/core/schemas/tools.py:+207` | — | [207](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:207) | <code></code> | Пустая строка завершает структурную проверку PNG. |
| `app/core/schemas/tools.py:+208` | — | [208](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:208) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/core/schemas/tools.py:+209` | — | [209](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:209) | `def _decode_png_base64(value: str) -> bytes:` | Начинает строгое декодирование base64 и проверку PNG. |
| `app/core/schemas/tools.py:+210` | — | [210](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:210) | `    if not isinstance(value, str) or not value:` | Требует непустую строку правильного типа. |
| `app/core/schemas/tools.py:+211` | — | [211](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:211) | `        raise ValueError("image data must be a non-empty base64 string")` | Отклоняет пустое или нестроковое значение data. |
| `app/core/schemas/tools.py:+212` | — | [212](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:212) | `    if len(value) > MAX_BASE64_LENGTH:` | Проверяет верхнюю границу длины base64 до декодирования. |
| `app/core/schemas/tools.py:+213` | — | [213](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:213) | `        raise ValueError("image exceeds the size limit")` | Отклоняет заведомо слишком длинную base64-строку. |
| `app/core/schemas/tools.py:+214` | — | [214](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:214) | `    try:` | Открывает try для строгого base64-декодирования. |
| `app/core/schemas/tools.py:+215` | — | [215](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:215) | `        raw = base64.b64decode(value, validate=True)` | Декодирует с validate=True, запрещая пробелы и неалфавитные символы. |
| `app/core/schemas/tools.py:+216` | — | [216](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:216) | `    except (binascii.Error, ValueError):` | Перехватывает binascii.Error и ValueError от base64. |
| `app/core/schemas/tools.py:+217` | — | [217](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:217) | `        raise ValueError("image data must be valid base64") from None` | Преобразует ошибку base64 в безопасное ValueError. |
| `app/core/schemas/tools.py:+218` | — | [218](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:218) | `    if len(raw) > MAX_IMAGE_BYTES:` | Проверяет фактический размер декодированных байтов. |
| `app/core/schemas/tools.py:+219` | — | [219](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:219) | `        raise ValueError("image exceeds the size limit")` | Отклоняет изображение больше 256 КиБ. |
| `app/core/schemas/tools.py:+220` | — | [220](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:220) | `    if base64.b64encode(raw).decode("ascii") != value:` | Пере-кодирует байты и сравнивает с исходной строкой. |
| `app/core/schemas/tools.py:+221` | — | [221](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:221) | `        raise ValueError("image data must use canonical base64")` | Отклоняет неканонический base64 с другим выравниванием/хвостом. |
| `app/core/schemas/tools.py:+222` | — | [222](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:222) | `    _validate_png(raw)` | Запускает структурную и Pillow-проверку PNG. |
| `app/core/schemas/tools.py:+223` | — | [223](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:223) | `    return raw` | Возвращает проверенные байты PNG. |
| `app/core/schemas/tools.py:+224` | — | [224](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:224) | <code></code> | Пустая строка завершает helper декодирования. |
| `app/core/schemas/tools.py:+225` | — | [225](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:225) | <code></code> | Вторая пустая строка разделяет helper и модели. |
| `app/core/schemas/tools.py:+226` | — | [226](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:226) | `class ToolImage(ToolDTO):` | Объявляет неизменяемую Pydantic-модель изображения результата. |
| `app/core/schemas/tools.py:+227` | — | [227](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:227) | `    data: str` | Хранит PNG как canonical base64-строку. |
| `app/core/schemas/tools.py:+228` | — | [228](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:228) | `    mime_type: Literal["image/png"] = PNG_MIME_TYPE` | Ограничивает MIME-тип литералом image/png со значением по умолчанию. |
| `app/core/schemas/tools.py:+229` | — | [229](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:229) | <code></code> | Пустая строка отделяет поля от валидатора. |
| `app/core/schemas/tools.py:+230` | — | [230](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:230) | `    @field_validator("data")` | Регистрирует валидатор поля data. |
| `app/core/schemas/tools.py:+231` | — | [231](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:231) | `    @classmethod` | Делает валидатор классовым методом, как требует Pydantic. |
| `app/core/schemas/tools.py:+232` | — | [232](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:232) | `    def validate_png_data(cls, value: str) -> str:` | Объявляет валидатор с возвратом строки. |
| `app/core/schemas/tools.py:+233` | — | [233](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:233) | `        _decode_png_base64(value)` | Прогоняет data через полную проверку base64/размера/PNG. |
| `app/core/schemas/tools.py:+234` | — | [234](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:234) | `        return value` | Возвращает исходную строку после успешной проверки. |
| `app/core/schemas/tools.py:+235` | — | [235](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:235) | <code></code> | Пустая строка завершает ToolImage. |
| `app/core/schemas/tools.py:+236` | — | [236](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:236) | <code></code> | Вторая пустая строка разделяет модели. |
| `app/core/schemas/tools.py:+237` | — | [237](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:237) | `class PhotoToolResult(ToolDTO):` | Объявляет результат инструмента с отдельными текстовым payload и изображениями. |
| `app/core/schemas/tools.py:+238` | — | [238](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:238) | `    payload: dict[str, object]` | Хранит структурированный payload, который увидит модель. |
| `app/core/schemas/tools.py:+239` | — | [239](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:239) | `    images: list[ToolImage] = Field(default_factory=list, max_length=MAX_AGENT_IMAGES)` | Ограничивает список изображений двумя элементами и задаёт пустой список по умолчанию. |
| `app/core/schemas/tools.py:+240` | — | [240](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:240) | <code></code> | Пустая строка завершает PhotoToolResult. |
| `app/core/schemas/tools.py:+241` | — | [241](/home/krypton/Dev/itmo/ai-in-sd-bot/app/core/schemas/tools.py:241) | <code></code> | Вторая пустая строка отделяет новые модели от прежних схем инструментов. |

## [app/handlers/dialog.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py)

Telegram-представление. После форматированного текста обработчик отправляет фото отдельными сообщениями. Удалённые строки `-5` и `-10` — прежние однострочные импорты, заменённые расширенными.

Строк в отчёте: **42** (+40 / -2).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/handlers/dialog.py:+2` | — | [2](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:2) | `import base64` | Импортирует base64 для декодирования проверенного PNG перед отправкой в Telegram. |
| `app/handlers/dialog.py:-5` | 5 | — | `from collections.abc import AsyncGenerator` | Удалён однострочный импорт AsyncGenerator: он расширен до AsyncGenerator и Sequence. |
| `app/handlers/dialog.py:+6` | — | [6](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:6) | `from collections.abc import AsyncGenerator, Sequence` | Добавляет Sequence для аннотации списка изображений в обработчике отправки. |
| `app/handlers/dialog.py:-10` | 10 | — | `from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message` | Удалён однострочный импорт aiogram.types; его заменил многострочный импорт с BufferedInputFile. |
| `app/handlers/dialog.py:+11` | — | [11](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:11) | `from aiogram.types import (` | Начинает многострочный импорт типов aiogram. |
| `app/handlers/dialog.py:+12` | — | [12](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:12) | `    BufferedInputFile,` | Импортирует BufferedInputFile для отправки PNG из памяти без временного файла. |
| `app/handlers/dialog.py:+13` | — | [13](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:13) | `    CallbackQuery,` | Импортирует CallbackQuery, используемый другими обработчиками модуля. |
| `app/handlers/dialog.py:+14` | — | [14](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:14) | `    InlineKeyboardButton,` | Импортирует InlineKeyboardButton для клавиатур подтверждения/повтора. |
| `app/handlers/dialog.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:15) | `    InlineKeyboardMarkup,` | Импортирует InlineKeyboardMarkup для клавиатур. |
| `app/handlers/dialog.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:16) | `    Message,` | Импортирует Message — цель ответа и источник чата. |
| `app/handlers/dialog.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:17) | `)` | Закрывает многострочный импорт aiogram. |
| `app/handlers/dialog.py:+30` | — | [30](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:30) | `from app.core.schemas.tools import ToolImage` | Импортирует ToolImage для типизации изображений в результате агента. |
| `app/handlers/dialog.py:+87` | — | [87](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:87) | `async def _send_result_photos(` | Начинает отдельный обработчик отправки фотографий после текстового ответа. |
| `app/handlers/dialog.py:+88` | — | [88](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:88) | `    target_message: Message,` | Принимает сообщение, в ответ на которое отправляются фото. |
| `app/handlers/dialog.py:+89` | — | [89](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:89) | `    images: Sequence[ToolImage],` | Принимает последовательность уже провалидированных ToolImage. |
| `app/handlers/dialog.py:+90` | — | [90](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:90) | `    request_id: str,` | Принимает request_id для безопасного лога без payload. |
| `app/handlers/dialog.py:+91` | — | [91](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:91) | `) -> None:` | Объявляет, что обработчик ничего не возвращает. |
| `app/handlers/dialog.py:+92` | — | [92](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:92) | `    failed = 0` | Счётчик неудачных отправок для одного итогового уведомления. |
| `app/handlers/dialog.py:+93` | — | [93](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:93) | <code></code> | Пустая строка отделяет инициализацию от цикла. |
| `app/handlers/dialog.py:+94` | — | [94](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:94) | `    for index, image in enumerate(images, start=1):` | Перебирает изображения в порядке результата, нумеруя файлы с единицы. |
| `app/handlers/dialog.py:+95` | — | [95](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:95) | `        try:` | Открывает try на одну отправку, чтобы сбой не прерывал остальные. |
| `app/handlers/dialog.py:+96` | — | [96](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:96) | `            await target_message.answer_photo(` | Отправляет фото ответом на исходное сообщение. |
| `app/handlers/dialog.py:+97` | — | [97](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:97) | `                BufferedInputFile(` | Оборачивает байты в BufferedInputFile для aiogram. |
| `app/handlers/dialog.py:+98` | — | [98](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:98) | `                    base64.b64decode(image.data, validate=True),` | Декодирует canonical base64 в байты; данные уже проверены ToolImage. |
| `app/handlers/dialog.py:+99` | — | [99](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:99) | `                    filename=f"result-{index}.png",` | Задаёт стабильное имя result-N.png в порядке отправки. |
| `app/handlers/dialog.py:+100` | — | [100](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:100) | `                )` | Закрывает BufferedInputFile. |
| `app/handlers/dialog.py:+101` | — | [101](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:101) | `            )` | Закрывает вызов answer_photo. |
| `app/handlers/dialog.py:+102` | — | [102](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:102) | `        except Exception:` | Перехватывает любой сбой Telegram/aiogram для одного изображения. |
| `app/handlers/dialog.py:+103` | — | [103](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:103) | `            failed += 1` | Увеличивает счётчик неудачных отправок. |
| `app/handlers/dialog.py:+104` | — | [104](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:104) | <code></code> | Пустая строка отделяет цикл от проверки результата. |
| `app/handlers/dialog.py:+105` | — | [105](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:105) | `    if not failed:` | Если все фото отправлены, дополнительное уведомление не нужно. |
| `app/handlers/dialog.py:+106` | — | [106](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:106) | `        return` | Выходит без текстового сообщения об ошибке. |
| `app/handlers/dialog.py:+107` | — | [107](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:107) | <code></code> | Пустая строка отделяет успешный выход от ветки ошибок. |
| `app/handlers/dialog.py:+108` | — | [108](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:108) | `    logger.warning("[%s] photo_send_failed count=%d", request_id, failed)` | Логирует только количество неудачных отправок и request_id, без данных изображения. |
| `app/handlers/dialog.py:+109` | — | [109](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:109) | `    try:` | Открывает try для необязательного уведомления об ошибке. |
| `app/handlers/dialog.py:+110` | — | [110](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:110) | `        await send_formatted_message(target_message, "📷 Не удалось отправить фото результата.")` | Сообщает пользователю текстом, что фото результата отправить не удалось. |
| `app/handlers/dialog.py:+111` | — | [111](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:111) | `    except Exception:` | Перехватывает сбой отправки самого уведомления. |
| `app/handlers/dialog.py:+112` | — | [112](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:112) | `        logger.warning("[%s] photo_failure_notice_unavailable", request_id)` | Логирует недоступность уведомления, не раскрывая содержимое. |
| `app/handlers/dialog.py:+113` | — | [113](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:113) | <code></code> | Пустая строка завершает обработчик фото. |
| `app/handlers/dialog.py:+114` | — | [114](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:114) | <code></code> | Вторая пустая строка отделяет обработчик от _execute_dialog. |
| `app/handlers/dialog.py:+154` | — | [154](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:154) | `        if isinstance(result, AgentResult) and result.images:` | После текстового ответа проверяет, что результат является AgentResult и содержит изображения. |
| `app/handlers/dialog.py:+155` | — | [155](/home/krypton/Dev/itmo/ai-in-sd-bot/app/handlers/dialog.py:155) | `            await _send_result_photos(target_message, result.images, req_id)` | Отправляет фото отдельно и после текста, передавая request_id для лога. |

## [app/llm/client.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py)

LLM-клиент. Добавлен необязательный режим OpenCode: стабильный на время жизни клиента заголовок сессии и User-Agent. Удалённая строка `-167` — прежний однострочный вызов `create`, заменённый многострочным с `_request_options()`.

Строк в отчёте: **23** (+22 / -1).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/llm/client.py:+5` | — | [5](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:5) | `from uuid import uuid4` | Импортирует uuid4 для генерации непрозрачного идентификатора сессии на клиент. |
| `app/llm/client.py:+44` | — | [44](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:44) | `OPENCODE_USER_AGENT = "itmo-study-bot/0.1"` | Фиксирует User-Agent, который отправляется только при включённом флаге OpenCode. |
| `app/llm/client.py:+45` | — | [45](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:45) | `OPENCODE_SESSION_HEADER = "x-opencode-session"` | Фиксирует имя дополнительного заголовка x-opencode-session. |
| `app/llm/client.py:+46` | — | [46](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:46) | <code></code> | Пустая строка отделяет константы от таблицы HTTP-кодов. |
| `app/llm/client.py:+69` | — | [69](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:69) | `        self._opencode_session = f"ses_{uuid4()}" if settings.llm_opencode_session else ""` | Создаёт стабильный для жизни клиента идентификатор ses_<uuid4> только при llm_opencode_session=True; иначе пустая строка. |
| `app/llm/client.py:+106` | — | [106](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:106) | `    def _request_options(self) -> dict[str, Any]:` | Начинает сборку необязательных параметров запроса к OpenAI SDK. |
| `app/llm/client.py:+107` | — | [107](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:107) | `        if not self._opencode_session:` | При выключенном флаге не добавляет никаких заголовков. |
| `app/llm/client.py:+108` | — | [108](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:108) | `            return {}` | Возвращает пустой dict, сохраняя прежнее поведение запросов. |
| `app/llm/client.py:+109` | — | [109](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:109) | <code></code> | Пустая строка отделяет выключенную ветку от включённой. |
| `app/llm/client.py:+110` | — | [110](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:110) | `        return {` | Возвращает kwargs с extra_headers для передачи в create/stream. |
| `app/llm/client.py:+111` | — | [111](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:111) | `            "extra_headers": {` | Открывает вложенный extra_headers. |
| `app/llm/client.py:+112` | — | [112](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:112) | `                "User-Agent": OPENCODE_USER_AGENT,` | Добавляет User-Agent itmo-study-bot/0.1. |
| `app/llm/client.py:+113` | — | [113](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:113) | `                OPENCODE_SESSION_HEADER: self._opencode_session,` | Добавляет стабильный x-opencode-session текущего клиента. |
| `app/llm/client.py:+114` | — | [114](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:114) | `            }` | Закрывает extra_headers. |
| `app/llm/client.py:+115` | — | [115](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:115) | `        }` | Закрывает dict параметров. |
| `app/llm/client.py:+116` | — | [116](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:116) | <code></code> | Пустая строка отделяет helper от generate. |
| `app/llm/client.py:-167` | 167 | — | `                data = await self._client.chat.completions.create(model=model, **payload)` | Удалён однострочный вызов chat.completions.create без дополнительных заголовков. |
| `app/llm/client.py:+183` | — | [183](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:183) | `                data = await self._client.chat.completions.create(` | Начинает многострочный вызов create, чтобы добавить необязательные kwargs. |
| `app/llm/client.py:+184` | — | [184](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:184) | `                    model=model,` | Передаёт выбранную модель. |
| `app/llm/client.py:+185` | — | [185](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:185) | `                    **payload,` | Передаёт прежний payload запроса без изменений. |
| `app/llm/client.py:+186` | — | [186](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:186) | `                    **self._request_options(),` | Добавляет extra_headers только если OpenCode-флаг включён. |
| `app/llm/client.py:+187` | — | [187](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:187) | `                )` | Закрывает вызов create. |
| `app/llm/client.py:+235` | — | [235](/home/krypton/Dev/itmo/ai-in-sd-bot/app/llm/client.py:235) | `            **self._request_options(),` | Добавляет те же необязательные заголовки в потоковый запрос stream. |

## [app/mcp/client.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py)

Транспортный MCP-клиент. После discovery-валидации payload он извлекает изображения из `content`, проверяет их и возвращает `PhotoToolResult`. Удалённые строки `-13`, `-88`, `-91`, `-110` заменены расширенным импортом, новым типом возврата и явным `read_only`.

Строк в отчёте: **46** (+42 / -4).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/mcp/client.py:+10` | — | [10](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:10) | `from pydantic import ValidationError` | Импортирует ValidationError, чтобы отличить некорректный PNG от прочих ошибок вызова. |
| `app/mcp/client.py:-13` | 13 | — | `from app.core.schemas.tools import ToolFailure` | Удалён узкий импорт ToolFailure; его заменил импорт набора схем изображения. |
| `app/mcp/client.py:+14` | — | [14](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:14) | `from app.core.schemas.tools import (` | Начинает многострочный импорт схем результата с изображениями. |
| `app/mcp/client.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:15) | `    MAX_AGENT_IMAGES,` | Импортирует MAX_AGENT_IMAGES для раннего ограничения числа ImageContent. |
| `app/mcp/client.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:16) | `    PNG_MIME_TYPE,` | Импортирует PNG_MIME_TYPE — единственный поддерживаемый MIME. |
| `app/mcp/client.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:17) | `    PhotoToolResult,` | Импортирует PhotoToolResult для отделения payload от изображений. |
| `app/mcp/client.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:18) | `    ToolFailure,` | Импортирует ToolFailure для единых кодов ошибок MCP-слоя. |
| `app/mcp/client.py:+19` | — | [19](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:19) | `    ToolImage,` | Импортирует ToolImage, который валидирует base64 и PNG. |
| `app/mcp/client.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:20) | `)` | Закрывает многострочный импорт. |
| `app/mcp/client.py:-88` | 88 | — | `    ) -> dict[str, object]:` | Старый call_tool возвращал только dict и не мог сообщить о наличии изображений. |
| `app/mcp/client.py:+95` | — | [95](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:95) | `    ) -> dict[str, object] \| PhotoToolResult:` | Новый контракт возвращает dict для текстового результата или PhotoToolResult для изображений. |
| `app/mcp/client.py:+97` | — | [97](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:97) | `        read_only = self.catalog.is_read_only(name)` | Один раз читает readOnlyHint из каталога и использует его и для подтверждения, и для проверки изображений. |
| `app/mcp/client.py:-91` | 91 | — | `        if not self.catalog.is_read_only(name):` | Удалён повторный вызов is_read_only внутри условия подтверждения. |
| `app/mcp/client.py:+99` | — | [99](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:99) | `        if not read_only:` | Использует сохранённый read_only, не меняя семантику подтверждения. |
| `app/mcp/client.py:-110` | 110 | — | `        return self.catalog.validate_output(name, result.structured_content)` | Удалён прямой возврат validate_output, который терял ImageContent. |
| `app/mcp/client.py:+118` | — | [118](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:118) | `        payload = self.catalog.validate_output(name, result.structured_content)` | Сначала валидирует structured payload по discovery-схеме, как и раньше. |
| `app/mcp/client.py:+119` | — | [119](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:119) | `        images = self._images(result)` | Извлекает и валидирует изображения из content после успешного payload. |
| `app/mcp/client.py:+120` | — | [120](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:120) | `        if images and not read_only:` | Запрещает изображения от инструментов изменения (readOnlyHint не true). |
| `app/mcp/client.py:+121` | — | [121](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:121) | `            raise ToolFailure(` | Открывает ToolFailure для мутирующего инструмента с изображением. |
| `app/mcp/client.py:+122` | — | [122](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:122) | `                "invalid_output", "Инструмент изменения не может возвращать изображения."` | Код invalid_output и понятное сообщение без данных изображения. |
| `app/mcp/client.py:+123` | — | [123](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:123) | `            )` | Закрывает вызов ToolFailure. |
| `app/mcp/client.py:+124` | — | [124](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:124) | `        if images:` | Если изображения есть, возвращает отдельный PhotoToolResult. |
| `app/mcp/client.py:+125` | — | [125](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:125) | `            return PhotoToolResult(payload=payload, images=images)` | Сохраняет проверенный payload и список ToolImage раздельно. |
| `app/mcp/client.py:+126` | — | [126](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:126) | `        return payload` | Для текстового результата сохраняет прежний возврат dict без изменений. |
| `app/mcp/client.py:+127` | — | [127](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:127) | <code></code> | Пустая строка отделяет call_tool от helper извлечения изображений. |
| `app/mcp/client.py:+128` | — | [128](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:128) | `    @staticmethod` | Объявляет статический helper, не зависящий от состояния клиента. |
| `app/mcp/client.py:+129` | — | [129](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:129) | `    def _images(result: CallToolResult) -> list[ToolImage]:` | Принимает CallToolResult и возвращает список проверенных ToolImage. |
| `app/mcp/client.py:+130` | — | [130](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:130) | `        images: list[ToolImage] = []` | Начинает пустой список изображений. |
| `app/mcp/client.py:+131` | — | [131](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:131) | <code></code> | Пустая строка отделяет инициализацию от обхода content. |
| `app/mcp/client.py:+132` | — | [132](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:132) | `        for content in result.content:` | Перебирает все блоки content ответа MCP. |
| `app/mcp/client.py:+133` | — | [133](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:133) | `            if not isinstance(content, types.ImageContent):` | Пропускает TextContent и другие не-изображения. |
| `app/mcp/client.py:+134` | — | [134](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:134) | `                continue` | Переходит к следующему блоку для не-ImageContent. |
| `app/mcp/client.py:+135` | — | [135](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:135) | `            if len(images) >= MAX_AGENT_IMAGES:` | Проверяет лимит двух изображений до добавления третьего. |
| `app/mcp/client.py:+136` | — | [136](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:136) | `                raise ToolFailure("invalid_output", "Инструмент вернул слишком много изображений.")` | Отклоняет результат с более чем двумя изображениями. |
| `app/mcp/client.py:+137` | — | [137](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:137) | `            if content.mime_type != PNG_MIME_TYPE:` | Требует MIME image/png. |
| `app/mcp/client.py:+138` | — | [138](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:138) | `                raise ToolFailure(` | Открывает ошибку неподдерживаемого формата. |
| `app/mcp/client.py:+139` | — | [139](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:139) | `                    "invalid_output", "Инструмент вернул изображение неподдерживаемого формата."` | Код invalid_output и сообщение о формате. |
| `app/mcp/client.py:+140` | — | [140](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:140) | `                )` | Закрывает вызов ToolFailure. |
| `app/mcp/client.py:+141` | — | [141](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:141) | `            try:` | Открывает try валидации ToolImage. |
| `app/mcp/client.py:+142` | — | [142](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:142) | `                images.append(ToolImage(data=content.data))` | Создаёт ToolImage, проверяя base64, размер и структуру PNG. |
| `app/mcp/client.py:+143` | — | [143](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:143) | `            except ValidationError:` | Перехватывает ValidationError Pydantic от ToolImage. |
| `app/mcp/client.py:+144` | — | [144](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:144) | `                raise ToolFailure(` | Открывает ToolFailure для некорректного изображения. |
| `app/mcp/client.py:+145` | — | [145](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:145) | `                    "invalid_output", "Инструмент вернул некорректное изображение."` | Код invalid_output и безопасное сообщение. |
| `app/mcp/client.py:+146` | — | [146](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:146) | `                ) from None` | Закрывает вызов ToolFailure без цепочки исходного исключения. |
| `app/mcp/client.py:+147` | — | [147](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:147) | <code></code> | Пустая строка отделяет цикл от возврата. |
| `app/mcp/client.py:+148` | — | [148](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/client.py:148) | `        return images` | Возвращает все проверенные изображения в порядке content. |

## [app/mcp/connection.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py)

Фабрики MCP-клиентов. Добавлены общий `ToolClients`, единый секрет для stdio-пары, отдельная math-фабрика и белый список окружения. Удалённых строк нет: старые фабрики сохранены, новые добавлены рядом.

Строк в отчёте: **91** (+91 / -0).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/mcp/connection.py:+4` | — | [4](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:4) | `from dataclasses import dataclass, replace` | Импортирует dataclass для набора клиентов и replace для подмены общего секрета в настройках. |
| `app/mcp/connection.py:+11` | — | [11](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:11) | `from app.mcp.composite import CompositeToolClient` | Импортирует CompositeToolClient, который собирается в этой фабрике. |
| `app/mcp/connection.py:+12` | — | [12](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:12) | <code></code> | Пустая строка отделяет локальные импорты от константы окружения. |
| `app/mcp/connection.py:+13` | — | [13](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:13) | `MATH_STDIO_ENVIRONMENT_KEYS = frozenset(` | Начинает белый список переменных окружения, передаваемых math-серверу в stdio. |
| `app/mcp/connection.py:+14` | — | [14](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:14) | `    {` | Открывает множество разрешённых ключей. |
| `app/mcp/connection.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:15) | `        "COMSPEC",` | COMSPEC нужен для запуска дочерних процессов в Windows. |
| `app/mcp/connection.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:16) | `        "HOME",` | HOME нужен Matplotlib/SymPy для пользовательских кэшей. |
| `app/mcp/connection.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:17) | `        "LANG",` | LANG сохраняет локаль для корректного текста. |
| `app/mcp/connection.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:18) | `        "LC_ALL",` | LC_ALL сохраняет локаль. |
| `app/mcp/connection.py:+19` | — | [19](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:19) | `        "LC_CTYPE",` | LC_CTYPE сохраняет кодировку/локаль. |
| `app/mcp/connection.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:20) | `        "LOCALAPPDATA",` | LOCALAPPDATA нужен Windows-процессам. |
| `app/mcp/connection.py:+21` | — | [21](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:21) | `        "MPLCONFIGDIR",` | MPLCONFIGDIR позволяет задать кэш Matplotlib. |
| `app/mcp/connection.py:+22` | — | [22](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:22) | `        "PATH",` | PATH нужен для поиска системных библиотек/процессов. |
| `app/mcp/connection.py:+23` | — | [23](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:23) | `        "PATHEXT",` | PATHEXT нужен Windows для расширений исполняемых файлов. |
| `app/mcp/connection.py:+24` | — | [24](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:24) | `        "PYTHONIOENCODING",` | PYTHONIOENCODING фиксирует кодировку stdio. |
| `app/mcp/connection.py:+25` | — | [25](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:25) | `        "SYSTEMROOT",` | SYSTEMROOT нужен Windows-процессам. |
| `app/mcp/connection.py:+26` | — | [26](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:26) | `        "TEMP",` | TEMP нужен временным файлам. |
| `app/mcp/connection.py:+27` | — | [27](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:27) | `        "TMP",` | TMP нужен временным файлам. |
| `app/mcp/connection.py:+28` | — | [28](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:28) | `        "TMPDIR",` | TMPDIR нужен временным файлам в Unix. |
| `app/mcp/connection.py:+29` | — | [29](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:29) | `        "USERPROFILE",` | USERPROFILE нужен Windows-процессам. |
| `app/mcp/connection.py:+30` | — | [30](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:30) | `        "WINDIR",` | WINDIR нужен Windows-процессам. |
| `app/mcp/connection.py:+31` | — | [31](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:31) | `    }` | Закрывает множество ключей. |
| `app/mcp/connection.py:+32` | — | [32](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:32) | `)` | Закрывает frozenset белого списка. |
| `app/mcp/connection.py:+33` | — | [33](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:33) | <code></code> | Пустая строка отделяет константу от dataclass. |
| `app/mcp/connection.py:+34` | — | [34](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:34) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/mcp/connection.py:+35` | — | [35](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:35) | `@dataclass(frozen=True)` | Делает ToolClients неизменяемым снимком созданных клиентов. |
| `app/mcp/connection.py:+36` | — | [36](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:36) | `class ToolClients:` | Объявляет контейнер клиентов приложения. |
| `app/mcp/connection.py:+37` | — | [37](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:37) | `    student: MCPClient` | Хранит студенческий клиент, который также получает ConfirmationService. |
| `app/mcp/connection.py:+38` | — | [38](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:38) | `    math: MCPClient \| None` | Хранит необязательный математический клиент: None в HTTP-режиме без URL. |
| `app/mcp/connection.py:+39` | — | [39](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:39) | `    composite: CompositeToolClient` | Хранит composite, объединяющий discovery доступных клиентов. |
| `app/mcp/connection.py:+40` | — | [40](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:40) | <code></code> | Пустая строка отделяет dataclass от фабрики. |
| `app/mcp/connection.py:+41` | — | [41](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:41) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/mcp/connection.py:+42` | — | [42](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:42) | `def create_tool_clients(settings: Settings) -> ToolClients:` | Начинает единую фабрику клиентов для приложения. |
| `app/mcp/connection.py:+43` | — | [43](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:43) | `    secret = settings.mcp_shared_secret` | Берёт настроенный общий секрет MCP. |
| `app/mcp/connection.py:+44` | — | [44](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:44) | `    if settings.mcp_transport == MCPTransport.STDIO and not secret:` | Для stdio без секрета создаёт временный общий секрет. |
| `app/mcp/connection.py:+45` | — | [45](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:45) | `        secret = secrets.token_urlsafe(32)` | Генерирует криптостойкий token_urlsafe(32), как раньше делал студенческий клиент. |
| `app/mcp/connection.py:+46` | — | [46](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:46) | <code></code> | Пустая строка отделяет подготовку секрета от подмены настроек. |
| `app/mcp/connection.py:+47` | — | [47](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:47) | `    configured = replace(settings, mcp_shared_secret=secret)` | Создаёт копию Settings с одним общим секретом для обоих stdio-процессов. |
| `app/mcp/connection.py:+48` | — | [48](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:48) | `    student = create_student_tools_client(configured)` | Создаёт студенческий MCPClient штатной фабрикой. |
| `app/mcp/connection.py:+49` | — | [49](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:49) | `    math = create_math_tools_client(configured)` | Создаёт математический MCPClient или None, если HTTP-URL не задан. |
| `app/mcp/connection.py:+50` | — | [50](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:50) | <code></code> | Пустая строка отделяет создание клиентов от сборки списка. |
| `app/mcp/connection.py:+51` | — | [51](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:51) | `    clients = [student]` | Начинает список клиентов с обязательного студенческого. |
| `app/mcp/connection.py:+52` | — | [52](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:52) | `    if math is not None:` | Проверяет, что математический клиент сконфигурирован. |
| `app/mcp/connection.py:+53` | — | [53](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:53) | `        clients.append(math)` | Добавляет math-клиент в общий composite. |
| `app/mcp/connection.py:+54` | — | [54](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:54) | <code></code> | Пустая строка отделяет список от возврата. |
| `app/mcp/connection.py:+55` | — | [55](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:55) | `    return ToolClients(` | Создаёт ToolClients с отдельными ссылками student/math/composite. |
| `app/mcp/connection.py:+56` | — | [56](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:56) | `        student=student,` | Сохраняет студенческий клиент для подтверждений. |
| `app/mcp/connection.py:+57` | — | [57](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:57) | `        math=math,` | Сохраняет math-клиент или None. |
| `app/mcp/connection.py:+58` | — | [58](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:58) | `        composite=CompositeToolClient(clients),` | Собирает CompositeToolClient из доступных клиентов. |
| `app/mcp/connection.py:+59` | — | [59](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:59) | `    )` | Закрывает конструктор ToolClients. |
| `app/mcp/connection.py:+113` | — | [113](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:113) | <code></code> | Пустая строка отделяет студенческую фабрику от математической. |
| `app/mcp/connection.py:+114` | — | [114](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:114) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/mcp/connection.py:+115` | — | [115](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:115) | `def create_math_tools_client(settings: Settings) -> MCPClient \| None:` | Начинает фабрику math-клиента; None означает режим одного сервера. |
| `app/mcp/connection.py:+116` | — | [116](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:116) | `    if settings.mcp_transport == MCPTransport.STREAMABLE_HTTP:` | Выбирает HTTP-ветку для streamable-http. |
| `app/mcp/connection.py:+117` | — | [117](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:117) | `        if not settings.math_mcp_server_url:` | Проверяет, задан ли MATH_MCP_SERVER_URL. |
| `app/mcp/connection.py:+118` | — | [118](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:118) | `            return None` | При пустом URL не создаёт второй клиент, сохраняя прежний режим. |
| `app/mcp/connection.py:+119` | — | [119](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:119) | `        if not settings.mcp_shared_secret:` | Для HTTP требует общий Bearer-секрет. |
| `app/mcp/connection.py:+120` | — | [120](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:120) | `            raise ValueError("MCP secret is required")` | Явно сообщает об отсутствии секрета до создания транспорта. |
| `app/mcp/connection.py:+121` | — | [121](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:121) | <code></code> | Пустая строка отделяет проверки от создания HTTP-транспорта. |
| `app/mcp/connection.py:+122` | — | [122](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:122) | `        transport: StdioTransport \| StreamableHttpTransport = StreamableHttpTransport(` | Создаёт StreamableHttpTransport с явной аннотацией объединённого типа. |
| `app/mcp/connection.py:+123` | — | [123](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:123) | `            settings.math_mcp_server_url,` | Использует отдельный URL математического сервера. |
| `app/mcp/connection.py:+124` | — | [124](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:124) | `            headers={"Authorization": f"Bearer {settings.mcp_shared_secret}"},` | Добавляет Authorization: Bearer с общим секретом. |
| `app/mcp/connection.py:+125` | — | [125](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:125) | `        )` | Закрывает конструктор HTTP-транспорта. |
| `app/mcp/connection.py:+126` | — | [126](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:126) | `        secret = settings.mcp_shared_secret` | Сохраняет секрет для meta подтверждений/вызовов. |
| `app/mcp/connection.py:+127` | — | [127](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:127) | `    else:` | Переходит к stdio-ветке. |
| `app/mcp/connection.py:+128` | — | [128](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:128) | `        secret = settings.mcp_shared_secret or secrets.token_urlsafe(32)` | Берёт общий секрет или генерирует временный. |
| `app/mcp/connection.py:+129` | — | [129](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:129) | `        env = {` | Начинает сборку минимального окружения math-процесса. |
| `app/mcp/connection.py:+130` | — | [130](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:130) | `            key: value for key, value in os.environ.items() if key in MATH_STDIO_ENVIRONMENT_KEYS` | Оставляет только ключи из MATH_STDIO_ENVIRONMENT_KEYS. |
| `app/mcp/connection.py:+131` | — | [131](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:131) | `        }` | Закрывает comprehension окружения. |
| `app/mcp/connection.py:+132` | — | [132](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:132) | `        env["MCP_SHARED_SECRET"] = secret` | Добавляет общий MCP_SHARED_SECRET для вызовов. |
| `app/mcp/connection.py:+133` | — | [133](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:133) | `        transport = StdioTransport(` | Создаёт stdio-транспорт для math_tools. |
| `app/mcp/connection.py:+134` | — | [134](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:134) | `            command=sys.executable,` | Запускает текущий Python-интерпретатор. |
| `app/mcp/connection.py:+135` | — | [135](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:135) | `            args=[` | Открывает список аргументов модуля. |
| `app/mcp/connection.py:+136` | — | [136](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:136) | `                "-m",` | Передаёт -m для запуска пакета. |
| `app/mcp/connection.py:+137` | — | [137](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:137) | `                "mcp_servers.math_tools",` | Указывает независимый пакет mcp_servers.math_tools. |
| `app/mcp/connection.py:+138` | — | [138](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:138) | `                "--transport",` | Выбирает транспорт. |
| `app/mcp/connection.py:+139` | — | [139](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:139) | `                "stdio",` | Передаёт stdio. |
| `app/mcp/connection.py:+140` | — | [140](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:140) | `                "--env-file",` | Отключает чтение пользовательского .env. |
| `app/mcp/connection.py:+141` | — | [141](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:141) | `                os.devnull,` | Использует os.devnull как пустой env-файл. |
| `app/mcp/connection.py:+142` | — | [142](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:142) | `            ],` | Закрывает список аргументов. |
| `app/mcp/connection.py:+143` | — | [143](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:143) | `            env=env,` | Передаёт отфильтрованное окружение. |
| `app/mcp/connection.py:+144` | — | [144](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:144) | `            keep_alive=True,` | Держит дочерний процесс живым между вызовами. |
| `app/mcp/connection.py:+145` | — | [145](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:145) | `        )` | Закрывает StdioTransport. |
| `app/mcp/connection.py:+146` | — | [146](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:146) | <code></code> | Пустая строка отделяет транспорт от SDK-клиента. |
| `app/mcp/connection.py:+147` | — | [147](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:147) | `    sdk = Client(` | Создаёт FastMCP Client поверх выбранного транспорта. |
| `app/mcp/connection.py:+148` | — | [148](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:148) | `        transport,` | Передаёт транспорт. |
| `app/mcp/connection.py:+149` | — | [149](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:149) | `        name="study-bot",` | Задаёт имя клиента study-bot. |
| `app/mcp/connection.py:+150` | — | [150](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:150) | `        timeout=settings.mcp_timeout,` | Использует общий таймаут операций. |
| `app/mcp/connection.py:+151` | — | [151](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:151) | `        init_timeout=settings.mcp_timeout,` | Использует тот же таймаут на инициализацию. |
| `app/mcp/connection.py:+152` | — | [152](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:152) | `    )` | Закрывает Client. |
| `app/mcp/connection.py:+153` | — | [153](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/connection.py:153) | `    return MCPClient(sdk, secret=secret)` | Оборачивает SDK-клиент в MCPClient с тем же секретом. |

## [app/services/dialog_service.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/dialog_service.py)

Восстановление подтверждения. Удалённая строка `-115` возвращала результат без изображений; новая ветка копирует `images` из сохранённого хода.

Строк в отчёте: **4** (+3 / -1).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/services/dialog_service.py:-115` | 115 | — | `                                return current` | Удалён прямой возврат current при повторном запросе подтверждения: он терял изображения, уже сохранённые в результате хода. |
| `app/services/dialog_service.py:+115` | — | [115](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/dialog_service.py:115) | `                                return current.model_copy(` | Начинает копирование актуального результата подтверждения через model_copy. |
| `app/services/dialog_service.py:+116` | — | [116](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/dialog_service.py:116) | `                                    update={"images": list(claim.result.images)}` | Переносит images из сохранённого claim.result в актуальный результат, не меняя остальные поля. |
| `app/services/dialog_service.py:+117` | — | [117](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/dialog_service.py:117) | `                                )` | Закрывает model_copy и возвращает результат с восстановленными фото. |

## [app/services/tool_execution.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py)

Выполнение инструментов. Добавлены извлечение изображений из `PhotoToolResult`, перенос в `RunState`, проверка совокупного бюджета и сохранение изображений при подготовке подтверждения. Удалённые строки `-14` и `-155` заменены расширенным импортом и разбором `outcome`.

Строк в отчёте: **42** (+40 / -2).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/services/tool_execution.py:+1` | — | [1](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:1) | `import base64` | Импортирует base64, чтобы точно считать байты уже проверенных изображений при бюджете. |
| `app/services/tool_execution.py:-14` | 14 | — | `from app.core.schemas.tools import ToolFailure` | Удалён узкий импорт ToolFailure; его заменил набор схем изображений. |
| `app/services/tool_execution.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:15) | `from app.core.schemas.tools import (` | Начинает многострочный импорт схем и лимитов изображений. |
| `app/services/tool_execution.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:16) | `    MAX_AGENT_IMAGE_BYTES,` | Импортирует суммарный лимит 512 КиБ для изображений одного агентного результата. |
| `app/services/tool_execution.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:17) | `    MAX_AGENT_IMAGES,` | Импортирует лимит двух изображений на ход. |
| `app/services/tool_execution.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:18) | `    PhotoToolResult,` | Импортирует PhotoToolResult, чтобы отделить payload от изображений. |
| `app/services/tool_execution.py:+19` | — | [19](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:19) | `    ToolFailure,` | Импортирует ToolFailure для кодов tool_output_budget. |
| `app/services/tool_execution.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:20) | `    ToolImage,` | Импортирует ToolImage для типизации добавляемых изображений. |
| `app/services/tool_execution.py:+21` | — | [21](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:21) | `)` | Закрывает многострочный импорт. |
| `app/services/tool_execution.py:+155` | — | [155](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:155) | `                "images": list(state.images),` | Переносит уже накопленные изображения в результат подготовки подтверждения, чтобы фото не терялись при PREPARE. |
| `app/services/tool_execution.py:+159` | — | [159](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:159) | `    def _add_images(self, state: RunState, images: Sequence[ToolImage]) -> None:` | Начинает helper, который проверяет и добавляет изображения в состояние запуска. |
| `app/services/tool_execution.py:+160` | — | [160](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:160) | `        if not images:` | Быстрый выход, если инструмент вернул только текст. |
| `app/services/tool_execution.py:+161` | — | [161](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:161) | `            return` | Не трогает состояние при пустом списке изображений. |
| `app/services/tool_execution.py:+162` | — | [162](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:162) | <code></code> | Пустая строка отделяет пустой случай от проверки лимита количества. |
| `app/services/tool_execution.py:+163` | — | [163](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:163) | `        if len(state.images) + len(images) > MAX_AGENT_IMAGES:` | Сравнивает уже накопленные и новые изображения с лимитом MAX_AGENT_IMAGES. |
| `app/services/tool_execution.py:+164` | — | [164](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:164) | `            raise ToolFailure(` | Открывает ToolFailure при превышении количества. |
| `app/services/tool_execution.py:+165` | — | [165](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:165) | `                "tool_output_budget",` | Код tool_output_budget позволяет агенту показать безопасную ошибку. |
| `app/services/tool_execution.py:+166` | — | [166](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:166) | `                "Инструмент вернул слишком много изображений. Уточните запрос.",` | Сообщение просит уточнить запрос, не раскрывая содержимое. |
| `app/services/tool_execution.py:+167` | — | [167](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:167) | `            )` | Закрывает вызов ToolFailure. |
| `app/services/tool_execution.py:+168` | — | [168](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:168) | <code></code> | Пустая строка отделяет проверку количества от проверки байтов. |
| `app/services/tool_execution.py:+169` | — | [169](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:169) | `        known_bytes = sum(` | Начинает подсчёт уже сохранённых байтов. |
| `app/services/tool_execution.py:+170` | — | [170](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:170) | `            len(base64.b64decode(image.data, validate=True)) for image in state.images` | Декодирует base64 каждого уже добавленного изображения; данные уже провалидированы ToolImage. |
| `app/services/tool_execution.py:+171` | — | [171](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:171) | `        )` | Закрывает sum по state.images. |
| `app/services/tool_execution.py:+172` | — | [172](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:172) | `        added_bytes = sum(len(base64.b64decode(image.data, validate=True)) for image in images)` | Считает байты новых изображений тем же строгим base64-декодированием. |
| `app/services/tool_execution.py:+173` | — | [173](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:173) | `        if known_bytes + added_bytes > MAX_AGENT_IMAGE_BYTES:` | Сравнивает суммарный объём с MAX_AGENT_IMAGE_BYTES (512 КиБ). |
| `app/services/tool_execution.py:+174` | — | [174](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:174) | `            raise ToolFailure(` | Открывает ToolFailure при превышении объёма. |
| `app/services/tool_execution.py:+175` | — | [175](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:175) | `                "tool_output_budget",` | Код tool_output_budget для безопасной ошибки. |
| `app/services/tool_execution.py:+176` | — | [176](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:176) | `                "Изображения результата слишком большие. Уточните запрос.",` | Сообщение просит уточнить запрос. |
| `app/services/tool_execution.py:+177` | — | [177](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:177) | `            )` | Закрывает вызов ToolFailure. |
| `app/services/tool_execution.py:+178` | — | [178](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:178) | <code></code> | Пустая строка отделяет проверки от изменения состояния. |
| `app/services/tool_execution.py:+179` | — | [179](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:179) | `        state.images.extend(images)` | Добавляет проверенные изображения в RunState только после обеих проверок. |
| `app/services/tool_execution.py:+180` | — | [180](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:180) | <code></code> | Пустая строка завершает _add_images. |
| `app/services/tool_execution.py:-155` | 155 | — | `        payload = await client.call_tool(call.name, call.arguments)` | Удалено присваивание payload напрямую из call_tool: результат теперь может быть PhotoToolResult. |
| `app/services/tool_execution.py:+185` | — | [185](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:185) | `        outcome = await client.call_tool(call.name, call.arguments)` | Сохраняет outcome, который может быть dict или PhotoToolResult. |
| `app/services/tool_execution.py:+193` | — | [193](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:193) | `        if isinstance(outcome, PhotoToolResult):` | Проверяет, вернул ли клиент отдельный результат с изображениями. |
| `app/services/tool_execution.py:+194` | — | [194](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:194) | `            payload = outcome.payload` | Для PhotoToolResult в историю модели идёт только structured payload. |
| `app/services/tool_execution.py:+195` | — | [195](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:195) | `            images = outcome.images` | Изображения забираются отдельно и не попадают в текст TOOL-сообщения. |
| `app/services/tool_execution.py:+196` | — | [196](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:196) | `        else:` | Иначе результат остаётся прежним dict. |
| `app/services/tool_execution.py:+197` | — | [197](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:197) | `            payload = outcome` | Payload — весь текстовый результат инструмента. |
| `app/services/tool_execution.py:+198` | — | [198](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:198) | `            images = []` | Изображений нет, сохраняется прежнее поведение. |
| `app/services/tool_execution.py:+199` | — | [199](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:199) | <code></code> | Пустая строка отделяет разбор результата от проверки размера текста. |
| `app/services/tool_execution.py:+206` | — | [206](/home/krypton/Dev/itmo/ai-in-sd-bot/app/services/tool_execution.py:206) | `        self._add_images(state, images)` | Добавляет изображения после проверки текстового бюджета, чтобы ошибка не теряла уже принятые фото. |

## [app/mcp/composite.py](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py)

Новый неотслеживаемый файл: все 137 строк добавлены. Клиент объединяет каталоги доступных серверов, исключает конфликты имён/URI, маршрутизирует вызовы к владельцу и изолирует start/recover/close.

Строк в отчёте: **137** (+137 / -0).

| ID | Старая | Новая | Код | Объяснение |
| --- | ---: | ---: | --- | --- |
| `app/mcp/composite.py:+1` | — | [1](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:1) | `import asyncio` | Импортирует asyncio для параллельного start/recover/aclose всех клиентов. |
| `app/mcp/composite.py:+2` | — | [2](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:2) | `import logging` | Импортирует logging для безопасных логов сбоев без секретов и payload. |
| `app/mcp/composite.py:+3` | — | [3](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:3) | `from collections.abc import Mapping, Sequence` | Импортирует Mapping и Sequence для контрактов каталогов и списка клиентов. |
| `app/mcp/composite.py:+4` | — | [4](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:4) | `from typing import Protocol` | Импортирует Protocol для структурного типа управляемого клиента. |
| `app/mcp/composite.py:+5` | — | [5](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:5) | <code></code> | Пустая строка отделяет стандартную библиотеку от локальных импортов. |
| `app/mcp/composite.py:+6` | — | [6](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:6) | `from app.core.ports import ToolClient` | Импортирует порт ToolClient, который реализует composite. |
| `app/mcp/composite.py:+7` | — | [7](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:7) | `from app.core.schemas.llm import ToolDefinition` | Импортирует ToolDefinition для типа объединённого каталога инструментов. |
| `app/mcp/composite.py:+8` | — | [8](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:8) | `from app.core.schemas.mcp import ResourceContent, ResourceDefinition` | Импортирует ResourceContent/ResourceDefinition для каталога и чтения ресурсов. |
| `app/mcp/composite.py:+9` | — | [9](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:9) | `from app.core.schemas.tools import PhotoToolResult, ToolFailure` | Импортирует PhotoToolResult и ToolFailure для проксирования результата и ошибок. |
| `app/mcp/composite.py:+10` | — | [10](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:10) | <code></code> | Пустая строка отделяет импорты от логгера. |
| `app/mcp/composite.py:+11` | — | [11](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:11) | `logger = logging.getLogger("app.mcp.composite")` | Создаёт отдельный логгер app.mcp.composite. |
| `app/mcp/composite.py:+12` | — | [12](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:12) | <code></code> | Пустая строка отделяет логгер от сообщений конфликтов. |
| `app/mcp/composite.py:+13` | — | [13](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:13) | `TOOL_COLLISION_MESSAGE = "Имя инструмента совпадает у нескольких MCP-серверов."` | Текст ошибки для совпадающего имени инструмента у разных серверов. |
| `app/mcp/composite.py:+14` | — | [14](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:14) | `RESOURCE_COLLISION_MESSAGE = "URI ресурса совпадает у нескольких MCP-серверов."` | Текст ошибки для совпадающего URI ресурса у разных серверов. |
| `app/mcp/composite.py:+15` | — | [15](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:15) | <code></code> | Пустая строка отделяет константы от протокола. |
| `app/mcp/composite.py:+16` | — | [16](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:16) | <code></code> | Вторая пустая строка разделяет определения верхнего уровня. |
| `app/mcp/composite.py:+17` | — | [17](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:17) | `class ManagedToolClient(ToolClient, Protocol):` | Описывает клиент, которым можно управлять: он умеет start/aclose и реализует ToolClient. |
| `app/mcp/composite.py:+18` | — | [18](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:18) | `    async def start(self) -> bool: ...` | Объявляет асинхронный start, возвращающий признак доступности. |
| `app/mcp/composite.py:+19` | — | [19](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:19) | <code></code> | Пустая строка отделяет методы протокола. |
| `app/mcp/composite.py:+20` | — | [20](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:20) | `    async def aclose(self) -> None: ...` | Объявляет асинхронное закрытие клиента. |
| `app/mcp/composite.py:+21` | — | [21](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:21) | <code></code> | Пустая строка завершает протокол. |
| `app/mcp/composite.py:+22` | — | [22](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:22) | <code></code> | Вторая пустая строка отделяет helper каталога. |
| `app/mcp/composite.py:+23` | — | [23](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:23) | `def _unique_entries[T](catalogs: Sequence[Mapping[str, T]]) -> dict[str, T]:` | Начинает объединение каталогов с исключением неоднозначных ключей. |
| `app/mcp/composite.py:+24` | — | [24](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:24) | `    candidates: dict[str, list[T]] = {}` | Хранит для каждого ключа список владельцев из доступных каталогов. |
| `app/mcp/composite.py:+25` | — | [25](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:25) | <code></code> | Пустая строка отделяет инициализацию от обхода каталогов. |
| `app/mcp/composite.py:+26` | — | [26](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:26) | `    for catalog in catalogs:` | Перебирает каталоги доступных клиентов. |
| `app/mcp/composite.py:+27` | — | [27](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:27) | `        for key, value in catalog.items():` | Перебирает пары ключ/значение в очередном каталоге. |
| `app/mcp/composite.py:+28` | — | [28](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:28) | `            candidates.setdefault(key, []).append(value)` | Добавляет значение в список кандидатов для этого ключа. |
| `app/mcp/composite.py:+29` | — | [29](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:29) | <code></code> | Пустая строка отделяет накопление от фильтрации. |
| `app/mcp/composite.py:+30` | — | [30](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:30) | `    return {key: values[0] for key, values in candidates.items() if len(values) == 1}` | Оставляет только ключи с ровно одним владельцем; конфликтующие исчезают из каталога. |
| `app/mcp/composite.py:+31` | — | [31](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:31) | <code></code> | Пустая строка завершает helper. |
| `app/mcp/composite.py:+32` | — | [32](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:32) | <code></code> | Вторая пустая строка отделяет класс composite. |
| `app/mcp/composite.py:+33` | — | [33](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:33) | `class CompositeToolClient(ToolClient):` | Объявляет составной клиент, реализующий ToolClient. |
| `app/mcp/composite.py:+34` | — | [34](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:34) | `    def __init__(self, clients: Sequence[ManagedToolClient]) -> None:` | Принимает последовательность управляемых клиентов. |
| `app/mcp/composite.py:+35` | — | [35](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:35) | `        self._clients = tuple(clients)` | Сохраняет клиентов как неизменяемый tuple, фиксируя порядок discovery. |
| `app/mcp/composite.py:+36` | — | [36](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:36) | <code></code> | Пустая строка отделяет конструктор от свойств. |
| `app/mcp/composite.py:+37` | — | [37](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:37) | `    @property` | Открывает свойство с клиентами. |
| `app/mcp/composite.py:+38` | — | [38](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:38) | `    def clients(self) -> tuple[ManagedToolClient, ...]:` | Возвращает кортеж управляемых клиентов. |
| `app/mcp/composite.py:+39` | — | [39](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:39) | `        return self._clients` | Отдаёт сохранённый tuple без копирования. |
| `app/mcp/composite.py:+40` | — | [40](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:40) | <code></code> | Пустая строка отделяет свойство clients от available. |
| `app/mcp/composite.py:+41` | — | [41](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:41) | `    @property` | Открывает свойство доступности composite. |
| `app/mcp/composite.py:+42` | — | [42](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:42) | `    def available(self) -> bool:` | Объявляет возврат bool. |
| `app/mcp/composite.py:+43` | — | [43](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:43) | `        return any(client.available for client in self._clients)` | Composite доступен, если доступен хотя бы один сервер. |
| `app/mcp/composite.py:+44` | — | [44](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:44) | <code></code> | Пустая строка отделяет available от tools. |
| `app/mcp/composite.py:+45` | — | [45](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:45) | `    @property` | Открывает свойство объединённого каталога инструментов. |
| `app/mcp/composite.py:+46` | — | [46](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:46) | `    def tools(self) -> Mapping[str, ToolDefinition]:` | Возвращает Mapping имя инструмента -> ToolDefinition. |
| `app/mcp/composite.py:+47` | — | [47](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:47) | `        available = [client for client in self._clients if client.available]` | Берёт только доступные клиенты, чтобы каталог не содержал мёртвых серверов. |
| `app/mcp/composite.py:+48` | — | [48](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:48) | `        return _unique_entries([client.tools for client in available])` | Объединяет их каталоги, исключая совпадающие имена. |
| `app/mcp/composite.py:+49` | — | [49](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:49) | <code></code> | Пустая строка отделяет tools от resources. |
| `app/mcp/composite.py:+50` | — | [50](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:50) | `    @property` | Открывает свойство объединённого каталога ресурсов. |
| `app/mcp/composite.py:+51` | — | [51](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:51) | `    def resources(self) -> Mapping[str, ResourceDefinition]:` | Возвращает Mapping URI -> ResourceDefinition. |
| `app/mcp/composite.py:+52` | — | [52](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:52) | `        available = [client for client in self._clients if client.available]` | Берёт только доступные клиенты. |
| `app/mcp/composite.py:+53` | — | [53](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:53) | `        return _unique_entries([client.resources for client in available])` | Объединяет ресурсы, исключая совпадающие URI. |
| `app/mcp/composite.py:+54` | — | [54](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:54) | <code></code> | Пустая строка отделяет каталоги от is_read_only. |
| `app/mcp/composite.py:+55` | — | [55](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:55) | `    def is_read_only(self, name: str) -> bool:` | Начинает проверку readOnlyHint для имени. |
| `app/mcp/composite.py:+56` | — | [56](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:56) | `        owners = self._tool_owners(name)` | Ищет всех доступных владельцев инструмента. |
| `app/mcp/composite.py:+57` | — | [57](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:57) | `        if len(owners) != 1:` | Если владельцев не один, имя неоднозначно или неизвестно. |
| `app/mcp/composite.py:+58` | — | [58](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:58) | `            return False` | Для конфликта/отсутствия возвращает False, требуя подтверждения. |
| `app/mcp/composite.py:+59` | — | [59](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:59) | `        return owners[0].is_read_only(name)` | Делегирует readOnlyHint единственному владельцу. |
| `app/mcp/composite.py:+60` | — | [60](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:60) | <code></code> | Пустая строка отделяет is_read_only от validate_arguments. |
| `app/mcp/composite.py:+61` | — | [61](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:61) | `    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:` | Начинает валидацию аргументов через владельца. |
| `app/mcp/composite.py:+62` | — | [62](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:62) | `        owner = self._resolve_tool(name)` | Находит единственного владельца или выбрасывает ошибку. |
| `app/mcp/composite.py:+63` | — | [63](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:63) | `        owner.validate_arguments(name, arguments)` | Передаёт аргументы каталогу исходного клиента, не дублируя схему. |
| `app/mcp/composite.py:+64` | — | [64](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:64) | <code></code> | Пустая строка отделяет validate_arguments от call_tool. |
| `app/mcp/composite.py:+65` | — | [65](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:65) | `    async def call_tool(` | Начинает проксирование вызова инструмента. |
| `app/mcp/composite.py:+66` | — | [66](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:66) | `        self,` | Первый параметр — self. |
| `app/mcp/composite.py:+67` | — | [67](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:67) | `        name: str,` | Имя инструмента. |
| `app/mcp/composite.py:+68` | — | [68](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:68) | `        arguments: Mapping[str, object],` | Аргументы вызова. |
| `app/mcp/composite.py:+69` | — | [69](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:69) | `        *,` | Начинает keyword-only параметры. |
| `app/mcp/composite.py:+70` | — | [70](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:70) | `        user_id: int \| None = None,` | Передаёт user_id для подтверждений. |
| `app/mcp/composite.py:+71` | — | [71](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:71) | `        confirmation_id: str \| None = None,` | Передаёт confirmation_id для мутирующих инструментов. |
| `app/mcp/composite.py:+72` | — | [72](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:72) | `    ) -> dict[str, object] \| PhotoToolResult:` | Возвращает текстовый payload или PhotoToolResult. |
| `app/mcp/composite.py:+73` | — | [73](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:73) | `        owner = self._resolve_tool(name)` | Находит единственного владельца имени. |
| `app/mcp/composite.py:+74` | — | [74](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:74) | `        return await owner.call_tool(` | Делегирует вызов владельцу без изменения аргументов. |
| `app/mcp/composite.py:+75` | — | [75](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:75) | `            name,` | Передаёт имя. |
| `app/mcp/composite.py:+76` | — | [76](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:76) | `            arguments,` | Передаёт аргументы. |
| `app/mcp/composite.py:+77` | — | [77](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:77) | `            user_id=user_id,` | Передаёт user_id. |
| `app/mcp/composite.py:+78` | — | [78](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:78) | `            confirmation_id=confirmation_id,` | Передаёт confirmation_id. |
| `app/mcp/composite.py:+79` | — | [79](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:79) | `        )` | Закрывает вызов owner.call_tool. |
| `app/mcp/composite.py:+80` | — | [80](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:80) | <code></code> | Пустая строка отделяет call_tool от read_resource. |
| `app/mcp/composite.py:+81` | — | [81](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:81) | `    async def read_resource(self, uri: str) -> ResourceContent:` | Начинает чтение ресурса. |
| `app/mcp/composite.py:+82` | — | [82](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:82) | `        owners = self._resource_owners(uri)` | Ищет всех доступных владельцев URI. |
| `app/mcp/composite.py:+83` | — | [83](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:83) | `        if len(owners) > 1:` | Если URI принадлежит нескольким серверам, чтение неоднозначно. |
| `app/mcp/composite.py:+84` | — | [84](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:84) | `            raise ToolFailure("capability_conflict", RESOURCE_COLLISION_MESSAGE)` | Выбрасывает capability_conflict с текстом о совпадении URI. |
| `app/mcp/composite.py:+85` | — | [85](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:85) | `        if not owners:` | Если владельцев нет, ресурс недоступен. |
| `app/mcp/composite.py:+86` | — | [86](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:86) | `            raise ToolFailure("resource_unavailable", "Ресурс недоступен.")` | Выбрасывает resource_unavailable. |
| `app/mcp/composite.py:+87` | — | [87](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:87) | <code></code> | Пустая строка отделяет проверки от делегирования. |
| `app/mcp/composite.py:+88` | — | [88](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:88) | `        return await owners[0].read_resource(uri)` | Читает ресурс у единственного владельца. |
| `app/mcp/composite.py:+89` | — | [89](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:89) | <code></code> | Пустая строка отделяет read_resource от start. |
| `app/mcp/composite.py:+90` | — | [90](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:90) | `    async def start(self) -> bool:` | Начинает параллельный запуск всех клиентов. |
| `app/mcp/composite.py:+91` | — | [91](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:91) | `        results = await asyncio.gather(` | Запускает gather по всем клиентам. |
| `app/mcp/composite.py:+92` | — | [92](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:92) | `            *(self._start(client) for client in self._clients),` | Оборачивает каждый start в _start с изоляцией исключений. |
| `app/mcp/composite.py:+93` | — | [93](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:93) | `        )` | Закрывает gather. |
| `app/mcp/composite.py:+94` | — | [94](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:94) | `        return any(results)` | Возвращает True, если стартовал хотя бы один сервер. |
| `app/mcp/composite.py:+95` | — | [95](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:95) | <code></code> | Пустая строка отделяет start от recover. |
| `app/mcp/composite.py:+96` | — | [96](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:96) | `    async def recover(self) -> bool:` | Начинает повторный discovery только недоступных серверов. |
| `app/mcp/composite.py:+97` | — | [97](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:97) | `        unavailable = tuple(client for client in self._clients if not client.available)` | Формирует tuple клиентов с available=False. |
| `app/mcp/composite.py:+98` | — | [98](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:98) | `        if not unavailable:` | Если все доступны, восстановление не требуется. |
| `app/mcp/composite.py:+99` | — | [99](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:99) | `            return False` | Возвращает False без сетевых вызовов. |
| `app/mcp/composite.py:+100` | — | [100](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:100) | <code></code> | Пустая строка отделяет ранний выход от запуска. |
| `app/mcp/composite.py:+101` | — | [101](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:101) | `        results = await asyncio.gather(` | Параллельно запускает только недоступные клиенты. |
| `app/mcp/composite.py:+102` | — | [102](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:102) | `            *(self._start(client) for client in unavailable),` | Передаёт каждого недоступного в _start. |
| `app/mcp/composite.py:+103` | — | [103](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:103) | `        )` | Закрывает gather. |
| `app/mcp/composite.py:+104` | — | [104](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:104) | `        return any(results)` | Возвращает True, если восстановился хотя бы один сервер. |
| `app/mcp/composite.py:+105` | — | [105](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:105) | <code></code> | Пустая строка отделяет recover от aclose. |
| `app/mcp/composite.py:+106` | — | [106](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:106) | `    async def aclose(self) -> None:` | Начинает закрытие всех клиентов. |
| `app/mcp/composite.py:+107` | — | [107](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:107) | `        await asyncio.gather(*(self._close(client) for client in self._clients))` | Параллельно закрывает каждого через _close, изолируя сбои. |
| `app/mcp/composite.py:+108` | — | [108](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:108) | <code></code> | Пустая строка отделяет публичные методы от приватных. |
| `app/mcp/composite.py:+109` | — | [109](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:109) | `    def _resolve_tool(self, name: str) -> ManagedToolClient:` | Начинает разрешение единственного владельца инструмента. |
| `app/mcp/composite.py:+110` | — | [110](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:110) | `        owners = self._tool_owners(name)` | Собирает доступных владельцев имени. |
| `app/mcp/composite.py:+111` | — | [111](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:111) | `        if len(owners) > 1:` | Если владельцев больше одного, имя конфликтует. |
| `app/mcp/composite.py:+112` | — | [112](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:112) | `            raise ToolFailure("capability_conflict", TOOL_COLLISION_MESSAGE)` | Выбрасывает capability_conflict с текстом о совпадении имён. |
| `app/mcp/composite.py:+113` | — | [113](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:113) | `        if not owners:` | Если владельцев нет, инструмент неизвестен или недоступен. |
| `app/mcp/composite.py:+114` | — | [114](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:114) | `            raise ToolFailure("unknown_tool", "Инструмент не разрешён или недоступен.")` | Выбрасывает unknown_tool. |
| `app/mcp/composite.py:+115` | — | [115](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:115) | <code></code> | Пустая строка отделяет ошибки от возврата. |
| `app/mcp/composite.py:+116` | — | [116](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:116) | `        return owners[0]` | Возвращает единственного владельца. |
| `app/mcp/composite.py:+117` | — | [117](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:117) | <code></code> | Пустая строка отделяет разрешение от поиска владельцев. |
| `app/mcp/composite.py:+118` | — | [118](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:118) | `    def _tool_owners(self, name: str) -> list[ManagedToolClient]:` | Начинает поиск владельцев инструмента. |
| `app/mcp/composite.py:+119` | — | [119](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:119) | `        return [client for client in self._clients if client.available and name in client.tools]` | Оставляет только доступные клиенты, в каталоге которых есть имя. |
| `app/mcp/composite.py:+120` | — | [120](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:120) | <code></code> | Пустая строка отделяет поиск инструментов от поиска ресурсов. |
| `app/mcp/composite.py:+121` | — | [121](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:121) | `    def _resource_owners(self, uri: str) -> list[ManagedToolClient]:` | Начинает поиск владельцев ресурса. |
| `app/mcp/composite.py:+122` | — | [122](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:122) | `        return [client for client in self._clients if client.available and uri in client.resources]` | Оставляет доступные клиенты, в каталоге которых есть URI. |
| `app/mcp/composite.py:+123` | — | [123](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:123) | <code></code> | Пустая строка отделяет поиск от запуска одного клиента. |
| `app/mcp/composite.py:+124` | — | [124](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:124) | `    async def _start(self, client: ManagedToolClient) -> bool:` | Начинает изолированный запуск одного клиента. |
| `app/mcp/composite.py:+125` | — | [125](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:125) | `        try:` | Открывает try вокруг client.start(). |
| `app/mcp/composite.py:+126` | — | [126](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:126) | `            await client.start()` | Запускает клиент; его discovery обновляет собственный каталог. |
| `app/mcp/composite.py:+127` | — | [127](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:127) | `        except Exception as error:` | Перехватывает любое исключение запуска. |
| `app/mcp/composite.py:+128` | — | [128](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:128) | `            logger.warning("mcp_composite_start_failed code=%s", type(error).__name__)` | Логирует только тип ошибки, без секретов и текста пользователя. |
| `app/mcp/composite.py:+129` | — | [129](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:129) | `            return False` | Считает клиент неуспешным, не прерывая остальные. |
| `app/mcp/composite.py:+130` | — | [130](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:130) | <code></code> | Пустая строка отделяет ошибку от успешного возврата. |
| `app/mcp/composite.py:+131` | — | [131](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:131) | `        return client.available` | Возвращает фактическую доступность клиента после start. |
| `app/mcp/composite.py:+132` | — | [132](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:132) | <code></code> | Пустая строка отделяет _start от _close. |
| `app/mcp/composite.py:+133` | — | [133](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:133) | `    async def _close(self, client: ManagedToolClient) -> None:` | Начинает изолированное закрытие одного клиента. |
| `app/mcp/composite.py:+134` | — | [134](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:134) | `        try:` | Открывает try вокруг client.aclose(). |
| `app/mcp/composite.py:+135` | — | [135](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:135) | `            await client.aclose()` | Закрывает клиент. |
| `app/mcp/composite.py:+136` | — | [136](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:136) | `        except Exception as error:` | Перехватывает любое исключение закрытия. |
| `app/mcp/composite.py:+137` | — | [137](/home/krypton/Dev/itmo/ai-in-sd-bot/app/mcp/composite.py:137) | `            logger.warning("mcp_composite_close_failed code=%s", type(error).__name__)` | Логирует только тип ошибки, не мешая закрыть остальные клиенты. |

## Отпечаток источника

Значения зафиксированы на момент создания отчёта; если `app` изменится,
хеши ниже перестанут совпадать и ссылки на строки нужно перепроверить.

| Величина | Значение |
| --- | --- |
| `git rev-parse HEAD` | `cbbf8e09ef4c2163fdeee8832da87852aae7d133` |
| SHA-256 `git diff --unified=0 HEAD -- app` | `4b58178c35283c2070bb4fc3953ae0c03cbb67fd6b74a596bd72fa34411a9843` |
| SHA-256 конкатенации отслеживаемых файлов `app` | `2ebcb832a94f356fcc98094ed141f047f756ca62c60923d0d54d795de5ce6e7d` |
| SHA-256 `sha256sum`-манифеста отслеживаемых файлов `app` | `2570c81d647bb94e0c6d0cab674666067a92ae08ea957f13c61d4b7acf7795e7` |
| SHA-256 `git status --porcelain -- app` | `3dc91985cfadebe9c5b3dcd4941ff395697ff9747e74c2826037134610657553` |
| SHA-256 `app/mcp/composite.py` | `50747f8b58ad5d13d0d036929d2cc5df33563ac362439e23e82eed2814d1e215` |

## Итоговая механическая проверка

Проверка выполнена отдельным скриптом в `/tmp` по финальному Markdown и тем же
источникам, что указаны в методике:

- `git diff --unified=0 HEAD -- app` — 465 добавлений / 25 удалений в 12 файлах;
- `git ls-files --others --exclude-standard app` — один файл
  `app/mcp/composite.py` на 137 добавлений;
- ожидаемый набор — **627** идентификаторов, фактически найдено **627**
  уникальных идентификаторов;
- `missing = 0`, `extra = 0`, `duplicates = 0`, `bad_rows = 0`;
- для каждой строки отдельно сверены старый/новый номер и точный текст в
  колонке «Код»; расхождений нет;
- суммы по файлам совпадают со сводкой выше: **+602 / −25 = 627**;
- проверка Markdown-таблиц: во всех таблицах одинаковое число колонок,
  литеральные `|` экранированы и в коде, и в объяснениях;
- пробелов покрытия нет; строки, отсутствующие в diff, в отчёт не добавлялись.

Ограничение проверки: она механически подтверждает покрытие и точность
цитируемого кода, но не заменяет смысловую рецензию объяснений. Если `app`
изменится после создания отчёта, сравните SHA-256 из раздела «Отпечаток
источника»; при несовпадении номера строк нужно перегенерировать.
