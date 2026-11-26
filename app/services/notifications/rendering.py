import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.notification_records import (
    NotificationSettingsRecord,
    OutgoingNotificationRecord,
)
from app.services.notifications.contracts import (
    BriefingContent,
    BriefingContext,
    BriefingSection,
    NotificationSettingsView,
)
from app.services.notifications.windows import valid_zone

MAX_TELEGRAM_TEXT = 4000
UTC_ZONE = ZoneInfo("UTC")


def settings_view(settings: NotificationSettingsRecord | None) -> NotificationSettingsView:
    if settings is None:
        return NotificationSettingsView(
            destination_saved=False,
            timezone=None,
            city=None,
            briefing_enabled=False,
        )

    return NotificationSettingsView(
        destination_saved=settings.destination_chat_id is not None,
        timezone=settings.timezone,
        city=settings.city,
        briefing_enabled=settings.briefing_enabled,
    )


def payload_local_date(payload: dict[str, object]) -> date | None:
    raw = payload.get("local_date")
    if not isinstance(raw, str):
        return None

    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def reminder_text(job: OutgoingNotificationRecord, now: datetime) -> str | None:
    text = job.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    zone_name = job.payload.get("timezone")
    zone = valid_zone(zone_name if isinstance(zone_name, str) else None) or UTC_ZONE
    local_time = job.scheduled_for.astimezone(zone)
    header = "🔔 Напоминание"

    delay = now - job.scheduled_for
    if delay > timedelta(0):
        minutes = max(1, math.ceil(delay.total_seconds() / 60))
        header = f"🔔 Напоминание (задержка {minutes} мин.)"

    return truncate(
        "\n".join(
            [
                header,
                "",
                text.strip(),
                "",
                f"Время: {local_time:%d.%m.%Y %H:%M} ({zone.key}).",
            ]
        )
    )


def briefing_fallback(problem: str) -> BriefingContent:
    return BriefingContent(
        sections=(
            BriefingSection(
                title="Данные сводки",
                problem=problem,
            ),
        )
    )


def render_briefing(
    context: BriefingContext,
    content: BriefingContent,
    prepared_at: datetime,
) -> str:
    lines = [
        f"☀️ Доброе утро! Сводка на {context.local_date:%d.%m.%Y}",
        "",
    ]

    sections = content.sections or (
        BriefingSection(title="Сводка", problem="Нет данных для сводки."),
    )

    for section in sections:
        title = section.title.strip() or "Раздел"
        lines.append(title)

        if section.text is not None and section.text.strip():
            lines.append(section.text.strip())
        elif section.problem is not None and section.problem.strip():
            lines.append(f"⚠️ {section.problem.strip()}")
        else:
            lines.append("Нет данных.")

        lines.append("")

    zone = valid_zone(context.timezone) or UTC_ZONE
    local_prepared = prepared_at.astimezone(zone)
    lines.append(f"Подготовлено в {local_prepared:%H:%M} ({zone.key}).")
    return truncate("\n".join(lines))


def truncate(text: str) -> str:
    if len(text) <= MAX_TELEGRAM_TEXT:
        return text
    return text[: MAX_TELEGRAM_TEXT - 1].rstrip() + "…"
