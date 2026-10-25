class AppError(Exception):
    pass


class LLMError(AppError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMServiceError(LLMError):
    pass


class LLMEmptyResponseError(LLMError):
    pass


class InvalidTemperatureError(AppError):
    pass


class InvalidModeError(AppError):
    pass


class QueueFullError(AppError):
    pass


class LLMRefusalError(LLMError):
    pass
