from collections.abc import Mapping
from datetime import datetime

from app.core.exceptions import LLM_ERROR_MESSAGES
from app.core.schemas.agent import AgentDecision, AgentResult, DialogResult, InProgress
from app.core.schemas.status import TimezoneStatus, ToolStatus
from app.db.records import ConfirmationRecord, ConfirmationState, DecisionAudit

_ACTION_TEXT = {
    AgentDecision.RESPOND: "ответ без инструмента",
    AgentDecision.CLARIFY: "уточнение запроса",
    AgentDecision.TOOL: "обращение к инструменту",
    AgentDecision.PREPARE: "подготовка действия с подтверждением",
    AgentDecision.ERROR: "ошибка обработки",
}

_TOOL_PURPOSE = {
    "get_weather": "Для ответа нужны актуальные погодные данные.",
    "get_schedule": "Для ответа нужно расписание на выбранный день.",
    "get_schedule_week": "Для ответа нужно расписание выбранной недели.",
    "read_resource": "Для ответа нужно прочитать расписание текущей недели.",
    "add_reminder": "Пользователь запросил создание напоминания.",
}

_STATUS_TEXT = {
    "ok": "пройдена",
    "plain_response": "ответ сформирован",
    "structured_response": "ответ сформирован",
    "completed": "обработка завершена",
    "awaiting_confirmation": "ожидается подтверждение владельца",
    "confirmed_operation": "Владелец подтвердил создание напоминания.",
    "recovered_committed_result": "Результат уже выполненного действия восстановлен из БД.",
    "succeeded": "действие выполнено",
    "cancelled": "действие отменено",
    "expired": "срок подтверждения истёк",
    "failed": "действие не выполнено",
    "dry_run": "выбрано действие без выполнения",
    "unknown_outcome": "результат действия пока неизвестен",
    "interrupted": "запрос прерван",
    "call_budget": "Достигнут лимит обращений к инструментам.",
    "step_budget": "Достигнут лимит шагов модели.",
    "context_budget": "Запрос и история не помещаются в контекст модели.",
    "location_clarification": "Название места не найдено или требует уточнения.",
    "timezone_required": "Для напоминания требуется настроить часовой пояс.",
    "timezone_mismatch": "Указанное время не соответствует сохранённому часовому поясу.",
    "ambiguous_time": "Локальное время неоднозначно или не существует из-за перехода DST.",
    "past_time": "Указанное время напоминания уже прошло.",
    "invalid_arguments": "Аргументы действия не прошли проверку.",
    "invalid_decision": "Решение модели не прошло проверку.",
    "mixed_decision": "Модель одновременно предложила ответ и обращение к инструменту.",
    "unknown_tool": "Модель выбрала недоступный инструмент.",
    "mcp_unavailable": "MCP-сервер недоступен.",
    "mcp_timeout": "MCP-сервер не ответил вовремя.",
    "weather_unavailable": "Сервис погоды недоступен.",
    "invalid_output": "Ответ инструмента не прошёл проверку.",
    "truncated_response": "Модель исчерпала лимит генерации до завершения ответа.",
    "truncated_tool_call": "Модель не завершила аргументы инструмента; действие не выполнялось.",
}


def confirmation_result(row: ConfirmationRecord, now: datetime) -> AgentResult:
    if row["state"] == ConfirmationState.SUCCEEDED:
        assert row["result"] is not None
        return reminder_success(row["result"])
    if row["state"] != ConfirmationState.PENDING:
        return AgentResult(
            text={
                ConfirmationState.CANCELLED: "Создание напоминания отменено.",
                ConfirmationState.EXPIRED: "Подтверждение истекло. Отправьте новый запрос.",
                ConfirmationState.FAILED: "Не удалось создать напоминание. Отправьте новый запрос.",
            }.get(row["state"], "Действие уже выполняется; повторного создания не будет.")
        )
    if row["expires_at"] <= now:
        return AgentResult(text="Подтверждение истекло. Отправьте новый запрос.")
    args = row["arguments"]
    return AgentResult(
        text=(
            f"Создать напоминание?\nТекст: {args['text']}\n"
            f"Время: {args['remind_at']}\nЗона: {row['timezone']}\n"
            f"Подтверждение действует до {row['expires_at'].isoformat()}."
        ),
        decision=AgentDecision.PREPARE,
        confirmation_id=row["id"],
        tool_name="add_reminder",
        arguments=args,
    )


def reminder_success(payload: Mapping[str, object]) -> AgentResult:
    return AgentResult(
        text=(
            f"Напоминание №{payload['id']} сохранено.\n{payload['text']}\n"
            f"Время: {payload['remind_at']} ({payload['timezone']})."
        ),
        decision=AgentDecision.TOOL,
        tool_name="add_reminder",
    )


def reply_text(result: DialogResult) -> str:
    if isinstance(result, InProgress):
        return "Этот запрос уже обрабатывается."
    if result.truncated:
        return (
            result.text
            + "\n\nОтвет обрезан из-за лимита генерации модели. Можно попросить продолжить."
        )
    return result.text


def tools_text(tools: list[ToolStatus]) -> str:
    if not tools:
        return "MCP-инструменты сейчас недоступны."
    lines = ["**Доступные инструменты:**"]
    for tool in tools:
        access = "изменение, нужно подтверждение" if tool.requires_confirmation else "чтение"
        lines.append(f"- `{tool.name}` — {tool.description} ({access})")
    return "\n".join(lines)


def timezone_text(status: TimezoneStatus) -> str:
    if status.zone is None:
        return "Часовой пояс не задан. Используйте /timezone Europe/Moscow."
    return f"Часовой пояс: {status.zone}. Текущее время: {status.now.isoformat()}"


def decision_text(record: DecisionAudit | None) -> str:
    if record is None:
        return "Решений пока нет. Отправьте запрос."

    action = _ACTION_TEXT.get(record["decision"], record["decision"])
    validation = _status_text(record["validation"])
    execution = _status_text(record["execution"])
    reason = _decision_reason(record)

    return (
        f"Время: {record['created_at'].isoformat()}\nДействие: {action}\n"
        f"Инструмент: {record['tool_name'] or 'нет'}\nПараметры: {record['arguments']}\n"
        f"Проверка: {validation}\nВыполнение: {execution}\n"
        f"Длительность: {record['duration_ms']:.0f} мс\nПричина: {reason}"
    )


def _status_text(value: str) -> str:
    return LLM_ERROR_MESSAGES.get(value) or _STATUS_TEXT.get(value, value)


def _decision_reason(record: DecisionAudit) -> str:
    if record["reason"] not in {"plain_response", "structured_response", "completed"}:
        return _status_text(record["reason"])

    if record["decision"] == AgentDecision.CLARIFY:
        return "Для выполнения запроса нужно уточнить недостающие параметры."

    tool_name = record["tool_name"]
    if tool_name in _TOOL_PURPOSE:
        return _TOOL_PURPOSE[tool_name]

    return "На запрос можно ответить без обращения к внешним данным."
