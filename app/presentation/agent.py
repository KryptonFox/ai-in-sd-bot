from collections.abc import Mapping
from datetime import datetime

from app.core.exceptions import LLM_ERROR_MESSAGES
from app.core.schemas.agent import AgentResult, DialogResult, InProgress
from app.core.schemas.status import TimezoneStatus, ToolStatus
from app.db.records import ConfirmationRecord, DecisionAudit


def confirmation_result(row: ConfirmationRecord, now: datetime) -> AgentResult:
    if row["state"] == "succeeded":
        assert row["result"] is not None
        return reminder_success(row["result"])
    if row["state"] != "pending":
        return AgentResult(
            text={
                "cancelled": "Создание напоминания отменено.",
                "expired": "Подтверждение истекло. Отправьте новый запрос.",
                "failed": "Не удалось создать напоминание. Отправьте новый запрос.",
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
        decision="prepare",
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
        decision="tool",
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
    validation = LLM_ERROR_MESSAGES.get(record["validation"], record["validation"])
    reason = LLM_ERROR_MESSAGES.get(record["reason"], record["reason"])
    return (
        f"Время: {record['created_at'].isoformat()}\nДействие: {record['decision']}\n"
        f"Инструмент: {record['tool_name'] or 'нет'}\nПараметры: {record['arguments']}\n"
        f"Проверка: {validation}\nВыполнение: {record['execution']}\n"
        f"Длительность: {record['duration_ms']:.0f} мс\nПричина: {reason}"
    )
