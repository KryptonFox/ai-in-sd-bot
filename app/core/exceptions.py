from enum import StrEnum


class LLMErrorCode(StrEnum):
    UNKNOWN = "llm_error"
    TIMEOUT = "llm_timeout"
    EMPTY_RESPONSE = "llm_empty_response"
    REFUSAL = "llm_refusal"
    NOT_CONFIGURED = "llm_not_configured"
    AUTHENTICATION = "llm_authentication"
    PERMISSION = "llm_permission"
    NOT_FOUND = "llm_not_found"
    RATE_LIMIT = "llm_rate_limit"
    UNAVAILABLE = "llm_unavailable"
    INVALID_REQUEST = "llm_invalid_request"
    CONNECTION = "llm_connection"
    INVALID_RESPONSE = "llm_invalid_response"
    GENERATION_LIMIT = "llm_generation_limit"


LLM_ERROR_MESSAGES: dict[str, str] = {
    LLMErrorCode.UNKNOWN: "Не удалось получить ответ языковой модели. Повторите запрос позже.",
    LLMErrorCode.TIMEOUT: (
        "Превышено время ожидания ответа языковой модели. Повторите запрос позже."
    ),
    LLMErrorCode.EMPTY_RESPONSE: (
        "Языковая модель вернула пустой ответ. Попробуйте переформулировать запрос."
    ),
    LLMErrorCode.REFUSAL: (
        "Модель отказалась выполнять запрос. Попробуйте изменить задачу или её формулировку."
    ),
    LLMErrorCode.NOT_CONFIGURED: (
        "Языковая модель не настроена: отсутствует API-ключ. Сообщите администратору бота."
    ),
    LLMErrorCode.AUTHENTICATION: (
        "Сервис языковой модели отклонил API-ключ. Администратору бота нужно проверить ключ."
    ),
    LLMErrorCode.PERMISSION: (
        "Сервис языковой модели запретил доступ. Администратору бота нужно проверить права "
        "и доступность выбранной модели."
    ),
    LLMErrorCode.NOT_FOUND: (
        "Сервис языковой модели не нашёл запрошенный адрес или модель. "
        "Администратору бота нужно проверить адрес сервиса и название модели."
    ),
    LLMErrorCode.RATE_LIMIT: (
        "Сервис языковой модели ограничил запросы или исчерпана квота. "
        "Повторите запрос позже; если ошибка повторяется, сообщите администратору бота."
    ),
    LLMErrorCode.UNAVAILABLE: (
        "На стороне сервиса языковой модели произошёл сбой. Повторите запрос позже."
    ),
    LLMErrorCode.INVALID_REQUEST: (
        "Сервис языковой модели отклонил параметры запроса. "
        "Администратору бота нужно проверить совместимость модели и настроек."
    ),
    LLMErrorCode.CONNECTION: (
        "Не удалось соединиться с сервисом языковой модели. Повторите запрос позже; "
        "если ошибка повторяется, администратору бота нужно проверить адрес сервиса и сеть."
    ),
    LLMErrorCode.INVALID_RESPONSE: (
        "Сервис языковой модели вернул ответ в неподдерживаемом формате. "
        "Повторите запрос; если ошибка повторяется, сообщите администратору бота."
    ),
    LLMErrorCode.GENERATION_LIMIT: (
        "Модель исчерпала лимит генерации, не сформировав ответ. "
        "Попробуйте сократить запрос или попросите администратора увеличить лимит генерации."
    ),
}


class AppError(Exception):
    pass


class LLMError(AppError):
    code: LLMErrorCode = LLMErrorCode.UNKNOWN
    status_code: int | None = None

    @property
    def user_message(self) -> str:
        return LLM_ERROR_MESSAGES[self.code]


class LLMTimeoutError(LLMError):
    code = LLMErrorCode.TIMEOUT


class LLMServiceError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        code: LLMErrorCode = LLMErrorCode.UNAVAILABLE,
        status_code: int | None = None,
        diagnostic_details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.diagnostic_details = diagnostic_details


class LLMEmptyResponseError(LLMError):
    code = LLMErrorCode.EMPTY_RESPONSE


class InvalidTemperatureError(AppError):
    pass


class InvalidModeError(AppError):
    pass


class QueueFullError(AppError):
    pass


class LLMRefusalError(LLMError):
    code = LLMErrorCode.REFUSAL


class InterruptedRunError(AppError):
    pass
