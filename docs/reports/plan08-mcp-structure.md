# План 08. Итоговая структура MCP и математического сервера

Статус: документация по стабильному состоянию рабочей копии. Базовая фиксация
исходного кода: `cbbf8e09ef4c2163fdeee8832da87852aae7d133`. Отчёт описывает
фактическое содержимое файлов, а не только план: все ссылки ниже указывают на
текущие строки рабочей копии.

Дополнение: полный построчный разбор изменений `app` находится в
[`plan08-bot-lines.md`](plan08-bot-lines.md).

## 1. Что изменилось в архитектуре

В приложении появился независимый математический MCP-сервер `math_tools` и
составной клиент `CompositeToolClient`. Студенческий сервер `student_tools`
остаётся владельцем инструментов расписания, погоды и напоминаний; математический
сервер не зависит от PostgreSQL, Telegram и LLM. Оба сервера публикуют свои
инструменты через discovery, а приложение объединяет только однозначные имена и
URI.

Точка сборки (`app/__main__.py:43-50`) создаёт `ToolClients`, запускает
`clients.composite`, передаёт `clients.composite` в `ToolExecutionService` и
`DialogService`, а `ConfirmationService` получает непосредственно
`clients.student`. Поэтому подтверждённое напоминание не может случайно уйти в
математический сервер. Фоновое восстановление (`app/__main__.py:125-140`)
разделено на два независимых блока: подтверждения и MCP; `mcp.recover()`
перезапускает discovery только недоступных серверов.

Составной клиент реализован в новом файле `app/mcp/composite.py`. Он не копирует
схемы и валидаторы: каталог, `input_schema`/`output_schema` и проверки остаются
у исходного `MCPClient`. Composite только выбирает владельца по имени/URI и
делегирует ему вызов.

### 1.1. Поток данных

1. `create_tool_clients` (`app/mcp/connection.py:42-59`) создаёт студенческий
   клиент, необязательный математический клиент и `CompositeToolClient`.
2. `CompositeToolClient.start` (`app/mcp/composite.py:90-94`) параллельно
   запускает оба клиента; сбой одного не отменяет второй.
3. `MCPClient.start` (`app/mcp/client.py:58-70`) делает `list_tools` и
   `list_resources`, загружает каталог и выставляет `available`.
4. `CompositeToolClient.tools/resources` (`app/mcp/composite.py:45-53`) каждый
   раз собирает актуальный каталог только доступных клиентов.
5. `ToolExecutionService` получает определения инструментов из composite,
   валидирует аргументы и вызывает владельца.
6. `ConfirmationService` работает с `clients.student` напрямую
   (`app/__main__.py:48-52`).

## 2. Пакет `mcp_servers/math_tools`: 15 модулей

Пакет состоит из 15 Python-модулей (включая `__init__.py`); `.pyc` в `__pycache__`
не являются исходниками. Ниже — роль каждого модуля и ключевые позиции.

| Модуль | Роль | Ключевые строки |
| --- | --- | --- |
| `__init__.py` | Только docstring независимого сервера. | `1` |
| `limits.py` | Единые лимиты: выражение 512 символов, 128 AST-узлов, 3 уравнения, 3 переменные, 4 функции, 64 решения, результат 8000 символов, PNG 256 КиБ, операция 5 с, память 768 МиБ, диапазон −10…10, 2001 точка. | `1-14` |
| `errors.py` | Карта безопасных кодов/сообщений и `MathError` с проверкой кода. | `3-16`, `19-25` |
| `config.py` | `MathTransport` (`stdio`/`streamable-http`), `MathSettings.load`, проверка порта и Bearer-секрета не короче 32 символов для HTTP. | `14-16`, `19-67` |
| `schemas.py` | Закрытые `StrEnum` операций/статусов, Pydantic-модели результатов и `PlotOutput`. | `9-39`, `41-88` |
| `syntax.py` | Безопасный разбор текста: белый список функций/узлов AST, длина, число узлов, переменные, уравнения. | `8-46`, `48-60`, `71-209` |
| `expressions.py` | Построение SymPy-выражений из разрешённых узлов, сохранение исходного (невычисленного) дерева, сбор ограничений области определения. | `17-65`, `68-118`, `130-265`, `268-402` |
| `engine.py` | Операции `calculate`, `solve`, `plot`; проверка решений; формирование статусов и JSON-payload. | `49-159`, `206-303`, `373-398`, `526-575` |
| `plotting.py` | Отрисовка через Matplotlib Agg, маски области определения, эвристическое разбиение разрывов, сжатие PNG. | `24-74`, `77-137`, `140-182`, `274-287` |
| `worker.py` | Дочерний процесс: `RLIMIT_AS`, построчный JSON-протокол, отображение `MathError`/`MemoryError`/`RecursionError`/`OverflowError`. | `10-18`, `36-52`, `55-81` |
| `runtime.py` | `MathWorker`: один процесс, `asyncio.Lock`, 5-секундный таймаут операции, 30-секундный таймаут старта, kill+wait, фильтрация окружения. | `11-23`, `30-176`, `183-201`, `204-212` |
| `service.py` | Валидация дочерних аргументов, нормализация выражений/границ, вызов runtime, проверка результата и PNG. | `31-204` |
| `server.py` | FastMCP-сервер `math-tools`, три инструмента, `readOnlyHint`, `output_schema` для графика, перевод ошибок в `ToolError`. | `25-47`, `67-85`, `88-181` |
| `__main__.py` | Запуск stdio/HTTP, `/health`, Bearer-middleware, uvicorn. | `17-38`, `41-73` |
| `healthcheck.py` | CLI-проверка `GET /health` с кодом возврата 0/1. | `9-32`, `35-41` |

### 2.1. Безопасный AST и точные десятичные литералы

Пользовательский текст **не** передаётся в `eval`, `sympify` или `parse_expr`.
`syntax._parse_expression` (`mcp_servers/math_tools/syntax.py:71-96`) сначала
проверяет длину 512, непустоту, ASCII и регулярное выражение допустимых символов
`^[0-9A-Za-z_+\-*/^().,\s]+$`. Затем `^` нормализуется в `**`, текст разбирается
через `ast.parse(..., mode="eval")`, считается число узлов (не более 128) и
проверяется каждый узел.

Разрешены только:

- `ast.Expression`, `ast.BinOp`, `ast.UnaryOp`, `ast.Load`;
- операции `Add`, `Sub`, `Mult`, `Div`, `Pow`, унарные `USub`, `UAdd`;
- целые и конечные вещественные `Constant`;
- имена переменных и константы `pi`, `e`, `E`, `oo`, `inf`;
- вызовы только разрешённых функций с проверенной арностью.

`_validate_nodes` (`syntax.py:99-140`) запрещает атрибуты, индексы, генераторы,
`lambda`, сравнения и прочие узлы. `FUNCTION_ARITIES` (`syntax.py:8-40`) задаёт
точные арности; `sin(x=1)` отклоняется из-за `keywords`, `danger(x)` даёт
`unknown_function`, неверная арность — `invalid_arguments`.

Точность десятичных литералов обеспечена отдельно. `ast` хранит `float`, но
`_constant_rational` (`expressions.py:251-265`) берёт **исходный текстовый
фрагмент** через `ast.get_source_segment(source, node)` и строит
`sympy.Rational(token.replace("_", ""))`. Поэтому `0.12345678901234567890123456789`
и `1.2345678901234567890123456789e-12` не теряют цифры (проверки
`tests/test_math_engine.py:98-112`, `286-299`). Для вычисляемого дерева
`_convert_expression` (`expressions.py:152-185`) использует те же рациональные
константы; для исходного дерева `_convert_original_expression`
(`expressions.py:188-237`) строит `Add`/`Mul`/`Pow` с `evaluate=False`, а
`sqrt` представляет как `Pow(..., Rational(1, 2), evaluate=False)`.

### 2.2. Область определения и проверка решений

`ParsedExpression` (`expressions.py:83-103`) хранит три вещи: нормализованный
текст, вычисленное `expression` и `original` — исходное дерево без сокращений.
`_collect_constraints` (`expressions.py:268-360`) обходит исходное дерево и
добавляет ограничения:

- деление — знаменатель `NONZERO`;
- отрицательная степень — основание `NONZERO`;
- чётный рациональный корень — основание `NONNEGATIVE` или `POSITIVE` с
  `real_only=True`;
- `sqrt` — аргумент неотрицателен (для вещественной ветви);
- `log`/`ln` — аргумент положителен, основание положительно и не равно 1;
- `tan`/`sec` — `cos(argument) != 0`; `cot`/`csc` — `sin(argument) != 0`;
- `asin`/`acos`, `acosh`, `atanh`, `atan2`, `factorial` — соответствующие
  ограничения области определения.

`_verify_solution` (`engine.py:373-398`) для каждого кандидата:

1. при `real_only` проверяет вещественность значений;
2. проверяет все ограничения левой и правой частей;
3. строит исходный residual `left.original - right.original` и подставляет
   кандидата;
4. требует, чтобы residual был нулём (`_is_zero_expression`, `engine.py:477-492`),
   используя точное `is_zero`, `simplify` и запасное 50-значное численное
   сравнение.

Поэтому сокращения не «заметают» выколотые точки: `(x²−1)/(x−1)=0` даёт только
`x=-1`, `(x−1)/(x−1)=x` отклоняет `x=1`, `sqrt(x)*sqrt(x)=-1` отклоняет `x=-1`,
`log(x)-log(x)=x` отклоняет `x=0` (`tests/test_math_engine.py:208-255`). Если
кандидат не прошёл проверку, он попадает в `discarded`. При отсутствии принятых
решений статус `no_solution`, а `verified` — `False`; иначе `verified=True`
относится к принятым решениям.

### 2.3. Инструменты MathMCP

Все три инструмента объявлены в `server.build_server`
(`mcp_servers/math_tools/server.py:88-164`) и имеют
`annotations={"readOnlyHint": True}` (`105`, `128`, `141-144`). Схемы
`input_schema`/`output_schema` генерируются FastMCP из аннотаций; для
`math_plot` дополнительно объявляет `output_schema=MathPlotResult.model_json_schema()`
(`server.py:144`), потому что обработчик возвращает `ToolResult`, а не модель.
Это единственное место, где схема задана явно; в приложении схемы не
дублируются.

#### `math_calculate` (`server.py:105-126`)

| Аргумент | Тип/ограничение | По умолчанию | Смысл |
| --- | --- | --- | --- |
| `expression` | строка 1…512 | обязателен | математическое выражение |
| `operation` | `CalculateOperation` | `simplify` | `simplify`, `evaluate`, `numeric`, `diff`, `integrate`, `limit` |
| `variable` | строка 1…128 | `x` | переменная дифференцирования/интегрирования/предела |
| `order` | целое 1…5 | `1` | порядок производной |
| `lower`, `upper` | `str \| float \| int \| None` | `None` | границы определённого интеграла; задаются вместе |
| `point` | `str \| float \| int \| None` | `None` | точка предела |
| `direction` | `LimitDirection` | `both` | `both`, `left`, `right` |

Результат `MathCalculateResult` (`schemas.py:45-53`): `status` (`ok` или
`undefined`), `operation`, `expression`, `variable`, `result` (точная строка
SymPy), `numeric` (15 значащих цифр), `exact` (False только для `numeric`),
`detail`.

Поведение (`engine.py:60-159`):

- `simplify` — `sympy.simplify`;
- `evaluate` — `expression.doit()` и повторное упрощение;
- `numeric` — `sympy.N(..., 15)`; свободные символы, `nan`/`zoo` или отсутствие
  конечного числового значения дают `undefined`;
- `diff` — производная порядка 1…5;
- `integrate` — неопределённый интеграл либо определённый при обеих границах;
  границы не должны зависеть от переменной;
- `limit` — обязательная `point`, направление `both/left/right`.

`nan`, `zoo`, `AccumBounds`, невычисленные `Integral`/`Derivative`/`Limit`
превращаются в `undefined` с пояснением (`engine.py:657-674`). Строки результата
ограничены 8000 символами (`engine.py:693-696`).

#### `math_solve` (`server.py:128-139`)

| Аргумент | Тип/ограничение | По умолчанию | Смысл |
| --- | --- | --- | --- |
| `equations` | список 1…3 строк, каждая 1…512 | обязателен | уравнения; допустима запись `x^2 = 4` или выражение = 0 |
| `variables` | список до 3 имён | `None` | все неизвестные; если не заданы — выводятся из уравнений |
| `real_only` | bool | `True` | только вещественные решения |

Результат `MathSolveResult` (`schemas.py:56-66`): `status`
(`solutions`, `no_solution`, `infinite_solutions`, `undefined`), `equations`,
`variables`, `solutions`, `numeric_solutions`, `discarded`, `real_only`,
`verified`, `detail`.

Алгоритм (`engine.py:206-303`):

- одно уравнение и одна переменная — `sympy.solveset` в `Reals` или `Complexes`;
  `FiniteSet`/`Union` конечных множеств дают кандидатов; `ConditionSet` —
  `undefined`; бесконечное множество — `infinite_solutions`;
- система — `sympy.solve(..., dict=True)`; решение с оставшимися неизвестными
  символами трактуется как `infinite_solutions`; отсутствие результата —
  `no_solution`;
- переменные проверяются на уникальность, все свободные символы должны быть
  перечислены; максимум 3;
- кандидатов максимум 64, иначе `resource_limit`;
- каждый кандидат проходит `_verify_solution`; непрошедшие попадают в
  `discarded`.

Ограничение: **символические параметры не поддерживаются как отдельный режим**.
Нет API «решить относительно x, выразив через a»: если после решения остаются
свободные символы из списка неизвестных, это классифицируется как
`infinite_solutions` (`engine.py:349-350`), а не как параметрическое семейство.
Неравенства, assumptions и бесконечные множества решений не возвращаются
поэлементно.

#### `math_plot` (`server.py:141-162`)

| Аргумент | Тип/ограничение | По умолчанию | Смысл |
| --- | --- | --- | --- |
| `functions` | список 1…4 строк, каждая 1…512 | обязателен | функции `y=f(x)` на общих осях |
| `variable` | строка 1…128 | `x` | независимая переменная |
| `x_min` | float | `-10.0` | левая граница |
| `x_max` | float | `10.0` | правая граница |

`x_min`/`x_max` должны быть конечными и `x_min < x_max`
(`engine.py:532-535`). Каждая функция должна зависеть только от выбранной
переменной (`engine.py:537-546`).

Ответ — `ToolResult` (`server.py:167-181`):

- `TextContent` с JSON-представлением `MathPlotResult`;
- `ImageContent` с `mime_type="image/png"` и base64 PNG;
- `structured_content` — тот же JSON-payload.

`MathPlotResult` (`schemas.py:74-82`): `status` (`ok`/`undefined`), `functions`
(для каждой: `expression`, `segments`, `discontinuities`), `variable`, `x_min`,
`x_max`, `samples` (2001), `png_bytes` (0…262144), `detail`.

### 2.4. Discovery, схемы и `readOnlyHint`

`ToolCatalog.load` (`app/mcp/catalog.py:25-54`) — единственное место, где
разбираются discovery-схемы:

- инструмент без `output_schema` пропускается;
- обе схемы проверяются `Draft202012Validator.check_schema`;
- `ToolDefinition.parameters` — это ровно `tool.input_schema`, без копирования
  и без ручного описания в handler;
- `read_only` — `tool.annotations.read_only_hint is True`;
- для входа и выхода компилируются валидаторы с `FormatChecker`.

`MCPClient.call_tool` (`app/mcp/client.py:96-126`) сначала валидирует аргументы
по discovery-схеме, затем payload по output-схеме. Поэтому `math_calculate`,
`math_solve` и `math_plot` автоматически получают ограничения из FastMCP:
`maxLength: 512`, `maxItems: 3/4`, enum операций и статусов. Тесты
`tests/test_math_transports.py:59-123` проверяют это на реальном stdio и HTTP.

### 2.5. Изоляция вычислений

`MathService` (`service.py:31-182`) валидирует аргументы в родительском
процессе, нормализует выражения и отправляет словарь в `MathRuntime`.
`MathRuntime` — тонкая обёртка над `MathWorker` (`runtime.py:204-212`).

`MathWorker`:

- один активный процесс и `asyncio.Lock` (`runtime.py:38-42`): запросы
  сериализуются, второй ждёт первого;
- запуск `sys.executable -m mcp_servers.math_tools.worker` с `cwd` корня
  проекта, `stdin/stdout` PIPE, `stderr=DEVNULL`, лимитом строки 4 МиБ
  (`runtime.py:86-96`);
- ожидание строки `{"ready": true}` с отдельным **30-секундным** таймаутом
  старта (`STARTUP_TIMEOUT_SECONDS`, `runtime.py:13`, `101-124`); первый импорт
  SymPy/Matplotlib происходит в worker до `ready` и не входит в 5-секундный
  бюджет операции;
- таймаут операции ровно 5 секунд (`OPERATION_TIMEOUT_SECONDS`, `limits.py:9`,
  `runtime.py:58-64`); при истечении процесс убивается и дожидается
  `process.wait()`;
- отмена (`CancelledError`) также убивает и дожидается процесс;
- ошибка транспорта/битый JSON/пустой ответ — kill, затем `worker_failed`;
- `aclose` берёт lock, закрывает stdin, убивает и ждёт процесс, очищает
  `_process`.

В worker (`worker.py:10-18`) выставляется `RLIMIT_AS = min(768 МиБ, hard)`;
если ОС не позволяет, ошибка игнорируется. `_execute` (`worker.py:36-52`)
превращает `MemoryError` в `memory_limit`, `RecursionError`/`OverflowError` в
`resource_limit`, прочие — в `evaluation_failed`. `operation="limits"`
возвращает фактические `memory_limit_bytes` и `timeout_seconds` без вычислений.

Окружение worker очищается от `BOT_TOKEN`, `DATABASE_URL`, `LLM_API_KEY`,
`MCP_SHARED_SECRET`, `OPENAI_API_KEY`, `POSTGRES_PASSWORD`,
`TELEGRAM_PROXY_URL` (`runtime.py:15-23`, `183-201`), добавляет `PYTHONPATH`,
`MPLBACKEND=Agg` и по одному потоку BLAS/OpenMP/MKL/NumExpr. В stdio-режиме
фабрика math-клиента передаёт только белый список ОС-переменных
(`app/mcp/connection.py:13-32`), а не всё окружение бота.

### 2.6. Графики: выборка, разрывы, область определения

`render_plot` (`plotting.py:24-74`):

- 2001 равномерная точка `np.linspace(x_min, x_max, 2001)`;
- дополнительные 2000 средних точек для проверки поведения между соседями;
- для каждой функции значения и маска ограничений вычисляются в узлах и
  серединах;
- значения вне области определения заменяются на `np.nan`;
- комплексные значения сохраняются только если мнимая часть в пределах
  `1e-9 * max(1, |real|)`, иначе `nan`;
- разрывы ищутся эвристически (`plotting.py:140-182`): нефинитная середина,
  выброс середины больше `8*span+1e-12`, знак меняется при больших значениях и
  скачке, а также экстремальный скачок `span > 1e6*scale`.
- оба конца интервала разрыва заменяются на `nan`, поэтому кривая рвётся;
- `segments` считается по числу непрерывных участков (`plotting.py:225-233`);
- `discontinuities` — позиции провалов и середины разорванных интервалов,
  дедуплицированные с допуском `1e-9` (`plotting.py:185-222`).

Оси (`plotting.py:236-271`): фиксированный `xlim`, сетка, нулевые линии,
подписи; `ylim` по конечным значениям, при разбросе больше `1e4` — по 2-му и
98-му процентилям; без значений — `[-1, 1]`; добавляется 5% поля. PNG
сохраняется с DPI 100, 84, 68, пока размер не станет ≤256 КиБ; иначе
`resource_limit` (`plotting.py:274-287`).

Практические ограничения:

- **узкая дыра может быть пропущена**: проверяются только 2001 узел и середины
  соседних интервалов; особенность/выколотая точка уже, чем шаг сетки, может не
  попасть ни в узел, ни в середину;
- разрыв определяется эвристикой по значениям, а не символическим анализом
  предела; возможны как ложные разрывы на очень резких, но непрерывных
  участках, так и пропуск сложного разрыва;
- показываются только вещественные ветви; комплексные значения с заметной
  мнимой частью отбрасываются;
- диапазон не ограничен по ширине, но при очень широком `x_min..x_max`
  разрешение на единицу длины падает;
- `x_min`/`x_max` не могут быть бесконечными; нельзя построить несколько
  отдельных осей или 3D-график.

### 2.7. Таймауты и ограничения: что именно действует

| Ограничение | Значение | Где |
| --- | --- | --- |
| Длина выражения | 512 символов | `limits.py:1`, `syntax.py:74-75` |
| Узлы AST | 128 | `limits.py:2`, `syntax.py:92-93` |
| Уравнения | 1…3 | `limits.py:3`, `server.py:130` |
| Переменные | до 3 | `limits.py:4`, `server.py:131` |
| Функции графика | 1…4 | `limits.py:5`, `server.py:147-150` |
| Решения | до 64 | `limits.py:6`, `engine.py:251-252` |
| Строка результата | 8000 символов | `limits.py:7`, `engine.py:693-696` |
| PNG | 256 КиБ | `limits.py:8`, `plotting.py:285-287` |
| Операция | 5 секунд | `limits.py:9`, `runtime.py:58-64` |
| Память worker | 768 МиБ | `limits.py:10-11`, `worker.py:10-18` |
| Старт worker | 30 секунд | `runtime.py:13`, `101-108` |
| Строка протокола worker | 4 МиБ | `runtime.py:14`, `95` |
| I/O изображений агента | 2 × 256 КиБ | `app/core/schemas/tools.py:16-22` |

## 3. `CompositeToolClient`: коллизии, discovery и восстановление

`app/mcp/composite.py` — новый файл из 137 строк.

### 3.1. Коллизии имён и URI

`_unique_entries` (`composite.py:23-30`) собирает для каждого ключа список
владельцев и оставляет только `len(values) == 1`. Поэтому совпадающее имя
инструмента или URI ресурса **исчезает из общего каталога**. Прямой вызов
такого имени обрабатывается `_resolve_tool` (`109-116`): если доступных
владельцев больше одного, выбрасывается `ToolFailure("capability_conflict", ...)`;
если ноль — `unknown_tool`. Для ресурсов аналогично `read_resource`
(`81-88`): больше одного владельца — `capability_conflict`, ноль —
`resource_unavailable`.

`is_read_only` (`55-59`) возвращает `True` только при ровно одном владельце;
конфликт или отсутствие имени дают `False`, а разрешение владельца отклоняет
вызов до выполнения. Если один из конфликтующих серверов стал недоступен, имя снова
становится однозначным: composite читает каталоги динамически и вызов уходит
оставшемуся владельцу (`tests/test_composite_mcp.py:260-308`).

### 3.2. Discovery и независимое восстановление

`CompositeToolClient.start` (`90-94`) параллельно вызывает `_start` для всех
клиентов. `_start` (`124-131`) ловит любое исключение, логирует только тип
ошибки и возвращает `False`; остальные серверы продолжают запуск. `start`
возвращает `True`, если доступен хотя бы один сервер.

`recover` (`96-104`) сначала отбирает клиентов с `available == False`. Если
таких нет — возвращает `False` без сетевых вызовов. Иначе параллельно
запускает только их. Это повторный discovery без перезапуска бота; успешный
`MCPClient.start` заново загружает tools/resources и снова включает сервер в
каталог. `aclose` (`106-107`) параллельно закрывает всех через `_close`
(`133-137`), причём сбой одного не мешает закрыть остальные.

Фоновый цикл в `app/__main__.py:129-140` вызывает `mcp.recover()` каждые 15
секунд; ошибки логируются как `mcp_recovery_unavailable`, не затрагивая
подтверждения. Подтверждения восстанавливаются отдельным вызовом
`confirmations.recover()`.

### 3.3. Подтверждения и маршрутизация

`CompositeToolClient.call_tool` (`65-79`) находит владельца и передаёт ему
`user_id` и `confirmation_id`. `ConfirmationService` в `app/__main__.py:48-52`
получает **не composite**, а `clients.student`, поэтому подготовка/исполнение
напоминания всегда идёт в `student_tools`. Это проверяется
`tests/test_composite_mcp.py:311-332`: вызов `add_reminder` через composite
попадает только в студенческий клиент, а математический не вызывается.

Composite не дублирует схемы: `validate_arguments` (`61-63`) делегирует
исходному `MCPClient`, `tools`/`resources` возвращают определения владельцев,
`is_read_only` читает аннотацию исходного discovery.

### 3.4. Один сервер, два транспорта и общий секрет

`ToolClients` (`app/mcp/connection.py:35-39`) хранит `student`, `math` (может
быть `None`) и `composite`.

- В **stdio** всегда создаются оба клиента. Если `MCP_SHARED_SECRET` пуст,
  `create_tool_clients` генерирует один `token_urlsafe(32)` на оба процесса
  (`connection.py:42-47`), чтобы дочерние серверы приняли вызовы.
- В **streamable-http** `create_math_tools_client` возвращает `None`, если
  `MATH_MCP_SERVER_URL` пуст (`115-118`). Это сохраняет прежний режим одного
  `student_tools`. Если URL задан, требуется `MCP_SHARED_SECRET`; оба клиента
  используют один и тот же `Authorization: Bearer ...` (`119-125`).
- Студенческий stdio-процесс получает PostgreSQL/schedule-переменные и секрет,
  но не `BOT_TOKEN`, `LLM_API_KEY`, `TELEGRAM_PROXY_URL`
  (`connection.py:74-104`).
- Математический stdio-процесс получает только белый список ОС-переменных и
  `MCP_SHARED_SECRET` (`connection.py:127-145`); `runtime.py:15-23` повторно
  вычищает секреты уже внутри worker.
- В HTTP математический контейнер не получает DB/Telegram/LLM-переменные:
  см. `compose.yaml:112-117`.

Оба транспорта проверены реальными тестами:
`tests/test_composite_mcp.py:668-705` (stdio и Streamable HTTP с настоящими
серверами и fake DB/weather/LLM).

### 3.5. Health

`CompositeToolClient.available` (`composite.py:41-43`) — `any` по клиентам.
`HealthState.mcp` хранит composite (`app/health.py:15`), а `health_result`
(`health.py:35-37`) отдаёт `mcp: available`, если доступен хотя бы один сервер.
При этом общий HTTP-статус `/health` по-прежнему требует инициализированного
polling и успешного `SELECT 1` (`health.py:18-37`); недоступность MCP не роняет
`/health` в 503, но видна в теле. Это сохраняет прежнюю семантику готовности
бота.

### 3.6. Compose и облачная упаковка

`compose.yaml`:

- `bot` получает `MCP_TRANSPORT: streamable-http`, адреса
  `student-tools-mcp:8000/mcp` и `math-tools-mcp:8000/mcp`, общий секрет
  (`42-45`), подключён к default и `mcp_internal` (`62-64`).
- `student-tools-mcp` (`66-104`) запускает `mcp_servers.student_tools` по HTTP,
  имеет healthcheck и внутреннюю сеть; публикации портов нет.
- `math-tools-mcp` (`106-130`) запускает `mcp_servers.math_tools` по HTTP,
  получает только `MCP_SHARED_SECRET`, `MCP_HOST`, `MCP_PORT`, `HOME`,
  `MPLCONFIGDIR`; не получает DB/Telegram/LLM. Сеть — только `mcp_internal`
  (`118-119`), healthcheck — `mcp_servers.math_tools.healthcheck` (`120-125`).
- сеть `mcp_internal` объявлена `internal: true` (`135-137`), поэтому наружу
  порты MCP не публикуются.

`Dockerfile:7-13` ставит зависимости из `uv.lock` без dev-группы, копирует
`app` и `mcp_servers`, запускает от непривилегированного `student`. Это
позволяет одним образом запускать и бота, и оба MCP-сервера (Compose задаёт
`command`).

`scripts/cloud.py`:

- `source_archive` (`88-124`) включает `app/**/*.py`, SQL-миграции,
  `mcp_servers/**/*.py` и `mcp_servers/*/data/*.json`; исключает `.env*`,
  `__pycache__`, `.local` и симлинки (`104-116`). Поэтому 15 файлов
  `mcp_servers/math_tools` попадают в архив, а секреты — нет.
- при redeploy останавливаются старые `bot`, `mcp-server`,
  `student-tools-mcp` и `math-tools-mcp` (`cloud.py:594-606`);
- `deploy logs` включает `math-tools-mcp` (`cloud.py:685-692`).

По сообщению ведущего, финальная приёмка подтвердила: образ
`itmo-ai-bot:acceptance` собран; одноразовый контейнер с `--network none` даёт
`/health` 200, отсутствующий/неверный Bearer — 401, авторизованный discovery —
3 инструмента, healthcheck-модуль — exit 0; dummy cloud Compose содержит 4
сервиса без опубликованных портов, math internal-only и без DB/Telegram/LLM
env; архив содержит 15 math-файлов и не содержит env; реальный stdio-env-аудит
не находит секретов и `MCP_TRANSPORT`; оба реальных транспорта собирают
student+math с fake DB/weather/LLM и подтверждением напоминания только в
student. Это свидетельство приёмки, а не выполненный в этом отчёте деплой.

## 4. PNG: структурированный payload отдельно от изображения

### 4.1. Путь от MCP до `PhotoToolResult`

`math_plot` возвращает `ToolResult` с двумя блоками (`server.py:167-181`):
`TextContent` (JSON `MathPlotResult`) и `ImageContent` (base64 PNG). Модель
получает **только** structured payload.

`MCPClient.call_tool` (`client.py:116-126`):

1. проверяет `structured_content` по output-схеме;
2. `_images(result)` (`128-148`) перебирает `result.content`;
3. пропускает всё, кроме `types.ImageContent`;
4. требует `mime_type == "image/png"`;
5. ограничивает число двумя;
6. создаёт `ToolImage`, который валидирует base64 и PNG;
7. если изображения есть и инструмент не read-only — `invalid_output`;
8. возвращает `PhotoToolResult(payload=payload, images=images)`, иначе прежний
   `dict`.

`PhotoToolResult` (`app/core/schemas/tools.py:237-239`) хранит payload и
`images` раздельно. Это гарантирует, что в `ChatMessage` для модели не попадёт
base64.

### 4.2. Состояние, результат и событие

`ToolExecutionService._read` (`app/services/tool_execution.py:181-213`):

- `outcome` — `dict` или `PhotoToolResult`;
- в `json.dumps({"source": call.name, "data": payload})` идёт только payload;
- `_add_images` (`159-179`) проверяет количество (≤2) и суммарный объём
  (≤512 КиБ), затем добавляет в `RunState.images`;
- текстовый бюджет `tool_result_max_chars` проверяется по payload, как раньше.

`RunState.images` (`app/core/schemas/execution.py:21`) копируется в
`AgentResult.images` (`execution.py:44`). `AgentResult` ограничивает список
двумя (`app/core/schemas/agent.py:38`). При подготовке подтверждения
`_prepare` также копирует уже накопленные изображения
(`tool_execution.py:151-157`), поэтому фото не теряется между read-вызовом и
`PREPARE`.

`PostgresTurnStore.complete` сохраняет `result.model_dump_json()` в JSONB
`agent_events.result` (`app/db/repositories/turn_repo.py:103-110`); `claim`
читает его через `AgentResult.model_validate` (`turn_repo.py:35-36`).
`interrupt`/`recover` также используют `model_dump_json` (`121-126`,
`153-156`). Старые записи без поля `images` валидируются с пустым списком
благодаря `default_factory=list`. **Миграция БД не нужна**: меняется только
содержимое существующего JSONB, схема таблиц не трогается.

История диалога и аудит остаются текстовыми: `complete` вставляет
`reply_text(result)` и аргументы без изображений (`turn_repo.py:71-102`);
`tests/test_tool_photos.py:603-642` проверяет, что base64 нет ни в
assistant-сообщении, ни в аудите, ни в логах.

### 4.3. Повторная выдача и Telegram

`DialogService.process_user_message` при `Replay` возвращает сохранённый
`claim.result` с изображениями (`app/services/dialog_service.py:111-119`).
Для подтверждения актуальный результат копируется с
`images=list(claim.result.images)` (`115-117`), чтобы повторная выдача не
потеряла фото после перезапуска/повтора события.

`app/handlers/dialog.py:149-155` сначала отправляет форматированный текст, затем
`_send_result_photos` (`87-113`). Для каждого изображения:

- `base64.b64decode(image.data, validate=True)`;
- `BufferedInputFile(..., filename="result-N.png")`;
- `answer_photo`;
- ошибки считаются, после цикла отправляется одно текстовое уведомление
  «📷 Не удалось отправить фото результата.»;
- в лог попадают только `request_id` и количество, без base64.

Порядок «текст, затем фото» и порядок нескольких фото проверены
`tests/test_tool_photos.py:730-782`; сбой фото не ломает текстовый ответ
(`785-805`).

### 4.4. Проверки и границы PNG

`app/core/schemas/tools.py`:

- `MAX_IMAGE_BYTES = 256 * 1024` — на одно изображение;
- `MAX_AGENT_IMAGES = 2`, `MAX_AGENT_IMAGE_BYTES = 512 * 1024` — на результат;
- `MAX_IMAGE_DIMENSION = 8192`, `MAX_IMAGE_PIXELS = 8 * 1024 * 1024`;
- `MAX_DECODED_IMAGE_BYTES = 32 * 1024 * 1024`;
- `MAX_BASE64_LENGTH` — верхняя граница строки до декодирования;
- `_decode_png_base64` требует непустую строку, strict base64, каноническое
  пере-кодирование, размер ≤256 КиБ;
- `_validate_png` проверяет подпись, чанки, длины, CRC, обязательные
  IHDR/IDAT/IEND, запрещает данные после IEND, проверяет допустимые color
  type/bit depth, dimensions/pixels, точный распакованный размер zlib и
  финальное декодирование Pillow;
- `_png_pixel_data_size` корректно считает и interlaced Adam7;
- `ToolImage.mime_type` — литерал `image/png`;
- `PhotoToolResult.images` — максимум 2.

Дополнительно `MCPClient` отклоняет изображения от мутирующих инструментов
(`readOnlyHint != True`), а `ToolExecutionService` — превышение общего бюджета
двух изображений/512 КиБ. Всё это не меняет старые текстовые результаты: если
`ImageContent` нет, возвращается ровно прежний `dict` (`client.py:124-126`).

## 5. OpenCode: минимальный флаг дополнительных заголовков

Это отдельное изменение по запросу пользователя, не новый провайдер и не
поддержка reasoning/thinking.

- `app/config.py:51-58` — общий `_boolean`; `app/config.py:75` — поле
  `llm_opencode_session: bool = False`; `app/config.py:155` — разбор
  `LLM_OPENCODE_SESSION`; `app/config.py:244` — передача в `Settings`.
- `app/llm/client.py:44-45` — константы `itmo-study-bot/0.1` и
  `x-opencode-session`.
- `app/llm/client.py:69` — при включённом флаге создаётся
  `ses_<uuid4>`; идентификатор стабилен для времени жизни конкретного
  `OpenAIClient` и не содержит Telegram ID. При выключенном флаге строка пустая.
- `app/llm/client.py:106-115` — `_request_options` возвращает `{}` или
  `extra_headers` с `User-Agent` и `x-opencode-session`.
- `app/llm/client.py:183-187` — заголовки добавляются в нестриминговый
  `create`; `app/llm/client.py:235` — в потоковый `stream`.
- `LLM_OPENCODE_SESSION=0` по умолчанию; при `0` запросы и заголовки не
  меняются. Отдельного `provider`, `thinking` или `reasoning` в payload нет:
  тесты `tests/test_opencode_go.py:159-196` проверяют их отсутствие, а
  `198-253` — стабильность заголовка и отдельную сессию на каждый клиент.
- Ограничения: это режим одного процесса/клиента, не многопользовательская
  изоляция; сессия не переживает перезапуск; смена `.env` требует перезапуска;
  никаких дополнительных настроек модели или провайдера не добавлено.

Живая проверка OpenCode Go (по сообщению ведущего, шесть из шести
авторизованных запросов): успешно прошли non-streaming и streaming probes,
полный цикл `math_calculate` с реальным tool call и продолжением, полный цикл
`math_plot` с реальным tool call, валидированным PNG 640×420 размером 25 988
байт и продолжением. Суммарно 10 971 prompt / 433 completion / 11 404 total
токенов; в запросе модели были только структурированные tool-результаты, без
PNG/base64. В этом сэмпле не потребовалось эхо `reasoning_content`; thinking и
provider-specific параметры не добавлялись. Подробности и оговорки:
[`opencode-go-live-check.md`](opencode-go-live-check.md). Это ограниченный
сэмпл (6 запросов, синтетические промпты), а не гарантия нагрузки или
провайдер-wide совместимости. Live-отчёт фиксирует исторический снимок `.env`;
ведущий затем удалил дублирующую активную строку флага OpenCode, сохранив
эффективное значение и режим файла `600`; содержимое `.env` в отчётах не
приводится.

## 6. Проверки и доказательства

На стабильном коде подтверждены ведущим:

- `uv run --locked pytest -q` — **636 passed, 17 skipped** за **26.42 s**;
- `RUN_INTEGRATION=1 uv run --locked pytest -m integration -q` — **17 passed,
  636 deselected**;
- `uv run --locked ty check`, `ruff check .`, `ruff format --check .`
  (176 файлов), `uv lock --check`, `git diff --check` — проходят;
- Docker-образ `itmo-ai-bot:acceptance` собран; одноразовый контейнер с
  `--network none`: math `/health` 200, отсутствующий/неверный Bearer 401,
  авторизованный discovery 3 инструмента, healthcheck-модуль exit 0;
- dummy cloud Compose: 4 сервиса, нет опубликованных портов, math internal-only
  и без DB/Telegram/LLM env;
- архив: 15 math-файлов, без env; реальный stdio-env-аудит: нет секретов и
  `MCP_TRANSPORT`;
- оба реальных транспорта (stdio и Streamable HTTP) собирают student+math с
  fake DB/weather/LLM; подтверждение напоминания уходит только в student;
- живая приёмка OpenCode Go: **6/6** авторизованных запросов (non-streaming,
  streaming, `math_calculate` + continuation, `math_plot` + continuation);
  суммарно **10 971 prompt / 433 completion / 11 404 total** токенов. Реальный
  tool call и продолжение сработали, `math_plot` вернул один валидированный PNG
  **640×420, 25 988 байт**; в запросе модели были только структурированные
  (text-only) tool-результаты, без PNG/base64. В этом сэмпле эхо
  `reasoning_content` не потребовалось. Источник:
  [`opencode-go-live-check.md`](opencode-go-live-check.md).

Ограничения доказательств: живые вызовы Telegram, боевые БД/погода и деплой
не выполнялись. Live LLM-проверка — ограниченный сэмпл из 6 запросов на
синтетических промптах; она не доказывает поведение под нагрузкой, длинный
контекст или совместимость со всеми провайдерами. Docker/PostgreSQL-проверки,
сборка образа, Compose-приёмка и live-запуск получены от ведущего и не выдаются
за самостоятельно выполненные в этом отчёте. Исходники `app` в ходе live-проверки
не менялись; коммит не создавался.

Исходная рабочая копия уже содержала правки только в `docs/plans/README.md`,
`plan.md` и неотслеживаемый `docs/plans/lab2/08-math-mcp.md`; они не являются
реализацией и не приписаны этому изменению. Базовая фиксация исходного кода —
`cbbf8e09ef4c2163fdeee8832da87852aae7d133`.

## 7. Файлы и отпечаток

Основные изменённые/новые исходники:

- `mcp_servers/math_tools/` — 15 модулей, 2670 строк;
- `app/mcp/composite.py` — 137 строк;
- `app/mcp/connection.py` — 153 строки;
- `app/mcp/client.py` — 201 строка;
- `app/core/schemas/tools.py` — PNG-валидация и модели;
- `app/services/tool_execution.py` — бюджет изображений;
- `app/handlers/dialog.py` — отправка фото;
- `app/llm/client.py` — OpenCode-заголовки;
- `compose.yaml`, `scripts/cloud.py`, `pyproject.toml` и `uv.lock` — упаковка
  и зависимости.

Точный построчный diff с идентификаторами `path:+N`/`path:-N` и проверкой
покрытия — в `docs/reports/plan08-bot-lines.md`. Если исходники `app` изменятся,
SHA-256 diff и хеши файлов в конце того отчёта перестанут совпадать; это
сигнал, что ссылки на строки устарели.
