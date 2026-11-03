# Инструкции проекта

- Проект использует Python 3.12 и `uv`; зависимости фиксируются в `uv.lock`.
- Статическая проверка приложения, скриптов и тестов: `uv run --locked ty check`; линтинг и форматирование — Ruff. Описывайте границы компонентов явными типами; не подавляйте ошибки тайпчекера вместо исправления контрактов.
- Telegram handlers отвечают за представление и взаимодействие с пользователем; services — за orchestration и бизнес-сценарии; SQL находится в repositories. OpenAI SDK-типы допустимы только в `llm`, MCP SDK-типы — в `mcp` и `mcp_servers`; MCP input/output schemas обнаруживаются через discovery, а не дублируются в обработчиках.
- Не добавляйте Python-комментарии. Краткие английские docstring допустимы в больших или сложных функциях, когда поясняют неочевидный алгоритм.
- Не записывайте в логи секреты, пользовательский текст, Telegram ID или chain-of-thought.
- Обычные unit-тесты изолируют внешние системы моками. Интеграционные проверки с настоящим PostgreSQL в Docker запускаются только при `RUN_INTEGRATION=1`.
- Реальные сетевые вызовы и отправка сообщений выполняются только при явной авторизации пользователя.
- В общей рабочей копии сохраняйте чужие изменения: не откатывайте их и редактируйте только файлы, относящиеся к задаче. Не вводите дополнительные подтверждения пользователя для обычной разработки.
- Создание коммитов согласовывайте с пользователем заранее. После реализации и проверок оставляйте изменения в рабочей копии до согласования коммита.

### Code readability

Optimize for human comprehension, not minimum line count.

- Prefer explicit, straightforward algorithms over clever expressions.
- Separate logical steps with blank lines. Use multiline signatures and calls when their arguments are difficult to scan on one line.
- Preserve relevant data and relationships throughout processing.
- Keep methods cohesive and abstraction levels consistent.
- Avoid unnecessary indirection, overengineering, and excessive fragmentation.
- Use idiomatic Python with meaningful names and appropriate type hints.
- Use StrEnum for closed sets of domain actions and lifecycle states while preserving their serialized string values.
- Consider algorithmic complexity and avoid redundant processing.
- Extract helpers when they clarify meaningful operations, not merely to shorten methods.
- Keep existing behavior and public contracts stable during refactoring.

Code should be easy to understand, debug, and modify by someone unfamiliar with its implementation.
