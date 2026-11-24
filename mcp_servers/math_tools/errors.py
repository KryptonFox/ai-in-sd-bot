from collections.abc import Mapping

ERROR_MESSAGES: Mapping[str, str] = {
    "invalid_expression": "Выражение не является корректной математической записью.",
    "expression_too_long": "Выражение длиннее 512 символов.",
    "expression_too_complex": "Выражение содержит больше 128 узлов.",
    "unsupported_syntax": "Разрешены только числа, переменные, + - * / ** ^, скобки и функции.",
    "unknown_function": "Математическая функция не поддерживается.",
    "invalid_variable": "Недопустимое имя переменной.",
    "invalid_arguments": "Параметры инструмента некорректны.",
    "resource_limit": "Результат превышает допустимый размер или сложность.",
    "timeout": "Вычисление не уложилось в отведённое время.",
    "worker_start_timeout": "Вычислительный процесс не запустился вовремя.",
    "queue_timeout": "Вычислитель занят: запрос не дождался свободного исполнителя.",
    "memory_limit": "Вычисление превысило выделенный лимит памяти.",
    "worker_failed": "Вычислительный процесс завершился аварийно.",
    "evaluation_failed": "Не удалось вычислить выражение.",
}


class MathError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        if not code or not code.replace("_", "").isalnum():
            raise ValueError("code must be a safe identifier")
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "Инструмент не выполнил запрос.")
        super().__init__(self.message)
