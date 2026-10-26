from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.schemas.tools import ReminderArguments, ToolFailure


def normalize_reminder(arguments: dict, timezone: str | None, now: datetime) -> ReminderArguments:
    if timezone is None:
        raise ToolFailure(
            "timezone_required", "Сначала задайте часовой пояс: /timezone Europe/Moscow."
        )
    value = arguments.get("remind_at")
    if not isinstance(value, str):
        raise ToolFailure("invalid_time", "Укажите точное время ISO 8601 с часовым поясом.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolFailure("timezone_required", "Время должно содержать явный часовой пояс.")
    zone = ZoneInfo(timezone)
    local = parsed.replace(tzinfo=None)
    candidates = {
        local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if local.replace(tzinfo=zone, fold=fold)
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == local
    }
    if len(candidates) != 1:
        raise ToolFailure(
            "ambiguous_time", "Это время неоднозначно или не существует из-за DST. Уточните время."
        )
    normalized = local.replace(tzinfo=zone)
    if normalized.utcoffset() != parsed.utcoffset():
        raise ToolFailure("timezone_mismatch", "Уточните время в сохранённом часовом поясе.")
    result = ReminderArguments.model_validate({**arguments, "remind_at": normalized})
    if result.remind_at <= now:
        raise ToolFailure("past_time", "Время напоминания должно быть в будущем.")
    return result
