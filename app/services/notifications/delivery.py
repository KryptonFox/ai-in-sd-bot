import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.core.trace import OperationTrace, TraceStep, bind_trace, trace_step
from app.db.notification_records import (
    NotificationKind,
    NotificationSettingsRecord,
    NotificationState,
    OutgoingNotificationRecord,
)
from app.services.notifications.contracts import (
    BlockedDelivery,
    BotDelivery,
    BriefingContext,
    BriefingProvider,
    NotificationDeliveryError,
    NotificationDeliveryRepository,
    NotificationPolicies,
    TickReport,
    TraceSink,
)
from app.services.notifications.rendering import (
    briefing_fallback,
    payload_local_date,
    reminder_text,
    render_briefing,
)
from app.services.notifications.windows import (
    briefing_scheduled_at,
    briefing_window_open,
    valid_zone,
)

logger = logging.getLogger("app.services.notifications")


@dataclass(frozen=True)
class _SendPermit:
    settings: NotificationSettingsRecord


class NotificationDelivery:
    def __init__(
        self,
        repository: NotificationDeliveryRepository,
        *,
        clock: Clock,
        deliver: BotDelivery,
        briefing: BriefingProvider,
        policies: NotificationPolicies,
        trace_sink: TraceSink | None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._deliver = deliver
        self._briefing = briefing
        self._policies = policies
        self._trace_sink = trace_sink
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> TickReport:
        async with self._tick_lock:
            now = self._clock.now()
            prepared_reminders = await self._repository.prepare_reminder_events(
                now,
                self._policies.reminder_grace,
            )
            prepared_briefings = await self._prepare_briefings(now)
            recovered = await self._repository.recover_expired_leases(
                now,
                self._policies.max_attempts,
            )

            report = TickReport(
                prepared_reminders=prepared_reminders,
                prepared_briefings=prepared_briefings,
                recovered_leases=recovered,
            )

            for _ in range(self._policies.batch_size):
                claim_now = self._clock.now()
                job = await self._repository.claim_due_event(
                    claim_now,
                    claim_now + self._policies.lease_duration,
                    self._policies.max_attempts,
                )
                if job is None:
                    break

                trace = OperationTrace(
                    job.user_id,
                    job.trace_id.hex,
                    self._clock.now(),
                    id=job.trace_id,
                    route=job.kind.value,
                )
                try:
                    with bind_trace(trace):
                        state = await self._deliver_one(job)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "notification_delivery_failed event=%s kind=%s exception=%s",
                        job.id,
                        job.kind.value,
                        type(error).__name__,
                    )
                    continue
                finally:
                    await self._emit_trace(trace)

                report = _increment_report(report, state)

            return report

    async def _prepare_briefings(self, now: datetime) -> int:
        prepared = 0
        for settings in await self._repository.briefing_candidates():
            zone = valid_zone(settings.timezone)
            if zone is None:
                continue

            local_now = now.astimezone(zone)
            if not briefing_window_open(
                local_now,
                self._policies.briefing_start,
                self._policies.briefing_end,
            ):
                continue

            local_date = local_now.date()
            scheduled_for = briefing_scheduled_at(
                local_date,
                zone,
                self._policies.briefing_start,
            )
            created = await self._repository.create_briefing_event(
                settings.user_id,
                local_date,
                zone.key,
                scheduled_for,
                uuid.uuid4(),
                now,
            )
            if created:
                prepared += 1

        return prepared

    async def _deliver_one(self, job: OutgoingNotificationRecord) -> NotificationState:
        with trace_step("notification", "attempt") as attempt:
            return await self._deliver_attempt(job, attempt)

    async def _deliver_attempt(
        self,
        job: OutgoingNotificationRecord,
        attempt: TraceStep | None,
    ) -> NotificationState:
        prepared_at = self._clock.now()
        if _lease_expired(job, prepared_at):
            if attempt is not None:
                attempt.fail("lease_expired")
            return NotificationState.FAILED

        settings = await self._repository.settings(job.user_id)
        blocked_at = self._clock.now()
        if _lease_expired(job, blocked_at):
            if attempt is not None:
                attempt.fail("lease_expired")
            return NotificationState.FAILED

        blocked = self._blocked_delivery(job, settings, blocked_at)
        if blocked is not None:
            if attempt is not None:
                attempt.fail(blocked.code)
            await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                blocked.state,
                blocked.code,
                blocked_at,
            )
            return blocked.state

        assert settings is not None

        if job.kind is NotificationKind.REMINDER:
            text = reminder_text(job, prepared_at)
        else:
            text = await self._briefing_text(job, settings, prepared_at)

        if text is None:
            if attempt is not None:
                attempt.fail("invalid_event")
            failed_at = self._clock.now()
            await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                NotificationState.FAILED,
                "invalid_event",
                failed_at,
            )
            return NotificationState.FAILED

        revalidated = await self._revalidate_before_send(job, attempt)
        if isinstance(revalidated, NotificationState):
            return revalidated

        destination = revalidated.settings.destination_chat_id
        assert destination is not None

        send_step: TraceStep | None = None
        try:
            with trace_step("telegram", "send") as send_step:
                async with asyncio.timeout(self._policies.delivery_timeout.total_seconds()):
                    await self._deliver(destination, text)
        except NotificationDeliveryError as error:
            if send_step is not None:
                send_step.fail(error.code)
            completed_at = self._clock.now()
            return await self._handle_delivery_failure(
                job,
                revalidated.settings,
                completed_at,
                attempt,
                code=error.code,
                retryable=error.retryable,
                retry_after=error.retry_after,
            )
        except TimeoutError:
            if send_step is not None:
                send_step.fail("delivery_timeout")
            completed_at = self._clock.now()
            return await self._handle_delivery_failure(
                job,
                revalidated.settings,
                completed_at,
                attempt,
                code="delivery_timeout",
                retryable=True,
                retry_after=None,
            )
        except Exception:
            if send_step is not None:
                send_step.fail("delivery_error")
            completed_at = self._clock.now()
            return await self._handle_delivery_failure(
                job,
                revalidated.settings,
                completed_at,
                attempt,
                code="delivery_error",
                retryable=True,
                retry_after=None,
            )

        completed_at = self._clock.now()
        sent = await self._repository.mark_sent(
            job.id,
            job.lease_id,
            completed_at,
        )
        if not sent:
            if attempt is not None:
                attempt.fail("lease_expired_uncertain")
            return NotificationState.FAILED

        return NotificationState.SENT

    async def _revalidate_before_send(
        self,
        job: OutgoingNotificationRecord,
        attempt: TraceStep | None,
    ) -> _SendPermit | NotificationState:
        now = self._clock.now()
        if _lease_expired(job, now):
            if attempt is not None:
                attempt.fail("lease_expired")
            return NotificationState.FAILED

        settings = await self._repository.settings(job.user_id)
        now = self._clock.now()
        if _lease_expired(job, now):
            if attempt is not None:
                attempt.fail("lease_expired")
            return NotificationState.FAILED

        blocked = self._blocked_delivery(job, settings, now)
        if blocked is not None:
            if attempt is not None:
                attempt.fail(blocked.code)
            await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                blocked.state,
                blocked.code,
                now,
            )
            return blocked.state

        assert settings is not None

        if job.kind is NotificationKind.DAILY_BRIEFING and _briefing_inputs_changed(
            job,
            settings,
        ):
            if attempt is not None:
                attempt.fail("settings_changed")
            await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                NotificationState.SUPPRESSED,
                "settings_changed",
                now,
            )
            return NotificationState.SUPPRESSED

        return _SendPermit(settings)

    def _blocked_delivery(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord | None,
        now: datetime,
    ) -> BlockedDelivery | None:
        if job.kind is NotificationKind.REMINDER:
            return self._blocked_reminder(job, settings, now)

        return self._blocked_briefing(job, settings, now)

    def _blocked_reminder(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord | None,
        now: datetime,
    ) -> BlockedDelivery | None:
        if now - job.scheduled_for > self._policies.reminder_grace:
            return BlockedDelivery(NotificationState.MISSED, "window_expired")
        if settings is None or settings.destination_chat_id is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "destination_unavailable")

        return None

    def _blocked_briefing(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord | None,
        now: datetime,
    ) -> BlockedDelivery | None:
        if settings is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "settings_unavailable")
        if settings.destination_chat_id is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "destination_unavailable")
        if not settings.briefing_enabled or settings.briefing_consent_at is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "briefing_disabled")
        if settings.city is None or settings.city_confirmed_at is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "city_unavailable")

        zone = valid_zone(settings.timezone)
        if zone is None:
            return BlockedDelivery(NotificationState.SUPPRESSED, "timezone_unavailable")

        local_date = payload_local_date(job.payload)
        if local_date is None:
            return BlockedDelivery(NotificationState.FAILED, "invalid_event")

        local_now = now.astimezone(zone)
        if local_now.date() != local_date or not briefing_window_open(
            local_now,
            self._policies.briefing_start,
            self._policies.briefing_end,
        ):
            return BlockedDelivery(NotificationState.MISSED, "window_expired")

        return None

    async def _briefing_text(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord,
        now: datetime,
    ) -> str | None:
        cached = job.payload.get("message")
        if isinstance(cached, str) and cached.strip():
            return cached

        assert settings.city is not None
        assert settings.timezone is not None
        local_date = payload_local_date(job.payload)
        if local_date is None:
            return None

        context = BriefingContext(
            user_id=job.user_id,
            trace_id=job.trace_id,
            local_date=local_date,
            timezone=settings.timezone,
            city=settings.city,
            scheduled_for=job.scheduled_for,
            prepared_at=now,
        )

        with trace_step("briefing", "prepare") as prepare_step:
            try:
                async with asyncio.timeout(self._policies.briefing_deadline.total_seconds()):
                    content = await self._briefing(context)
            except TimeoutError:
                if prepare_step is not None:
                    prepare_step.fail("timeout")
                content = briefing_fallback("Подготовка сводки превысила допустимое время.")
            except Exception:
                if prepare_step is not None:
                    prepare_step.fail("briefing_unavailable")
                content = briefing_fallback("Источники сводки временно недоступны.")

        text = render_briefing(context, content, now)
        payload = {**job.payload, "message": text}
        saved = await self._repository.save_payload(
            job.id,
            job.lease_id,
            payload,
            now,
        )
        return text if saved else None

    async def _handle_delivery_failure(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord,
        now: datetime,
        attempt: TraceStep | None,
        *,
        code: str,
        retryable: bool,
        retry_after: float | None,
    ) -> NotificationState:
        if not retryable or job.attempt_count >= self._policies.max_attempts:
            if attempt is not None:
                attempt.fail(code)
            marked = await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                NotificationState.FAILED,
                code,
                now,
            )
            return NotificationState.FAILED if marked else _uncertain(attempt)

        delay = self._retry_delay(job.attempt_count)
        if retry_after is not None and retry_after > 0:
            delay = max(delay, timedelta(seconds=retry_after))

        next_attempt_at = now + delay
        if not self._send_allowed_at(job, settings, next_attempt_at):
            if attempt is not None:
                attempt.fail("window_expired")
            marked = await self._repository.mark_terminal(
                job.id,
                job.lease_id,
                NotificationState.MISSED,
                "window_expired",
                now,
            )
            return NotificationState.MISSED if marked else _uncertain(attempt)

        if attempt is not None:
            attempt.fail(code)
        marked = await self._repository.mark_retry(
            job.id,
            job.lease_id,
            next_attempt_at,
            code,
            now,
        )
        return NotificationState.RETRY if marked else _uncertain(attempt)

    async def _emit_trace(self, trace: OperationTrace) -> None:
        if self._trace_sink is None:
            return

        try:
            async with asyncio.timeout(2):
                await self._trace_sink(trace)
        except Exception as error:
            logger.warning(
                "notification_trace_unavailable exception=%s",
                type(error).__name__,
            )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        index = min(max(attempt_count - 1, 0), len(self._policies.retry_delays) - 1)
        return self._policies.retry_delays[index]

    def _send_allowed_at(
        self,
        job: OutgoingNotificationRecord,
        settings: NotificationSettingsRecord,
        moment: datetime,
    ) -> bool:
        if job.kind is NotificationKind.REMINDER:
            return moment - job.scheduled_for <= self._policies.reminder_grace

        zone = valid_zone(settings.timezone)
        if zone is None:
            return False

        local_date = payload_local_date(job.payload)
        if local_date is None:
            return False

        local_moment = moment.astimezone(zone)
        return local_moment.date() == local_date and briefing_window_open(
            local_moment,
            self._policies.briefing_start,
            self._policies.briefing_end,
        )


def _lease_expired(job: OutgoingNotificationRecord, moment: datetime) -> bool:
    return job.lease_until is not None and moment >= job.lease_until


def _briefing_inputs_changed(
    job: OutgoingNotificationRecord,
    settings: NotificationSettingsRecord,
) -> bool:
    city = settings.city
    if city is None:
        return True

    payload_timezone = job.payload.get("timezone")
    if isinstance(payload_timezone, str) and payload_timezone != settings.timezone:
        return True

    expected = {
        "city_name": city.name,
        "city_country": city.country,
        "city_region": city.region,
        "city_latitude": round(city.latitude, 6),
        "city_longitude": round(city.longitude, 6),
    }

    for key, current in expected.items():
        if key not in job.payload:
            continue
        stored = job.payload[key]
        if key in {"city_latitude", "city_longitude"} and isinstance(stored, (int, float)):
            stored = round(float(stored), 6)
        if stored != current:
            return True

    return False


def _uncertain(attempt: TraceStep | None) -> NotificationState:
    if attempt is not None:
        attempt.fail("lease_expired_uncertain")
    return NotificationState.FAILED


def _increment_report(report: TickReport, state: NotificationState) -> TickReport:
    values = {
        "prepared_reminders": report.prepared_reminders,
        "prepared_briefings": report.prepared_briefings,
        "recovered_leases": report.recovered_leases,
        "sent": report.sent + (1 if state is NotificationState.SENT else 0),
        "retried": report.retried + (1 if state is NotificationState.RETRY else 0),
        "failed": report.failed + (1 if state is NotificationState.FAILED else 0),
        "missed": report.missed + (1 if state is NotificationState.MISSED else 0),
        "suppressed": report.suppressed + (1 if state is NotificationState.SUPPRESSED else 0),
    }
    return TickReport(**values)
