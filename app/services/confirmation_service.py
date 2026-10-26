from app.core.clock import SystemClock
from app.core.context import get_request_id
from app.core.schemas.agent import AgentResult, EventContext
from app.core.schemas.tools import ToolFailure
from app.core.timezones import normalize_reminder
from app.db.repositories import confirmation_repo as repo
from app.db.repositories.audit_repo import record_decision


class ConfirmationService:
    def __init__(self, pool, mcp, clock=None, ttl=300):
        self.pool, self.mcp, self.clock, self.ttl = pool, mcp, clock or SystemClock(), ttl

    async def existing(self, context: EventContext):
        row = await repo.find_event(self.pool, context.user_id, context.event_id)
        return self.describe(row) if row else None

    def describe(self, row):
        if row["state"] == "succeeded":
            return self.success(row["result"])
        if row["state"] != "pending":
            return AgentResult(
                text={
                    "cancelled": "Создание напоминания отменено.",
                    "expired": "Подтверждение истекло. Отправьте новый запрос.",
                    "failed": "Не удалось создать напоминание. Отправьте новый запрос.",
                }.get(row["state"], "Действие уже выполняется; повторного создания не будет.")
            )
        if row["expires_at"] <= self.clock.now():
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

    @staticmethod
    def success(payload):
        return AgentResult(
            text=(
                f"Напоминание №{payload['id']} сохранено.\n{payload['text']}\n"
                f"Время: {payload['remind_at']} ({payload['timezone']})."
            ),
            decision="tool",
            tool_name="add_reminder",
        )

    async def prepare(self, context, arguments, timezone, *, position, calls_remaining):
        normalized = normalize_reminder(arguments, timezone, self.clock.now())
        row = await repo.prepare(
            self.pool,
            user_id=context.user_id,
            event_id=context.event_id,
            position=position,
            arguments=normalized.model_dump(mode="json"),
            timezone=timezone,
            now=self.clock.now(),
            ttl=self.ttl,
            calls_remaining=calls_remaining,
            request_id=context.request_id,
        )
        return self.describe(row)

    async def confirm(self, user_id, identifier, *, cancel=False):
        try:
            row = await repo.claim(self.pool, identifier, user_id, self.clock.now(), cancel=cancel)
        except ValueError:
            row = None
        if row is None:
            await record_decision(
                self.pool,
                user_id=user_id,
                event_id="confirmation:denied",
                request_id=get_request_id() or "local",
                decision="error",
                validation="unauthorized",
                execution="denied",
                reason="confirmation_owner_required",
                created_at=self.clock.now(),
            )
            return AgentResult(
                text="Подтверждение не найдено или принадлежит другому пользователю.",
                decision="error",
            )
        if not row.get("claimed"):
            await self._audit(row, row["state"], reason="confirmation_state")
            return self.describe(row)
        return await self._execute(row)

    async def _audit(self, row, execution, *, reason, duration_ms=0):
        await record_decision(
            self.pool,
            user_id=row["user_id"],
            event_id=row["event_id"],
            request_id=row["request_id"],
            decision="error" if execution in {"failed", "expired"} else "tool",
            tool_name="add_reminder",
            arguments=row["arguments"],
            execution=execution,
            reason=reason,
            duration_ms=duration_ms,
            created_at=self.clock.now(),
        )

    async def _execute(self, row):
        now = self.clock.now()
        try:
            payload = await self.mcp.call_tool(
                "add_reminder", row["arguments"], user_id=row["user_id"], confirmation_id=row["id"]
            )
            await repo.complete(self.pool, row["id"], payload, self.clock.now())
            result = self.success(payload)
            status = "succeeded"
        except ToolFailure as error:
            transient = error.code in {"mcp_timeout", "mcp_unavailable", "service_unavailable"}
            if not transient:
                await repo.mark_failed(self.pool, row["id"], error.code, self.clock.now())
            result = AgentResult(
                text=(
                    "Результат операции пока не подтверждён. Повторное создание не выполняется; "
                    "состояние будет сверено автоматически."
                    if transient
                    else "Инструмент отклонил создание напоминания. Отправьте новый запрос."
                ),
                decision="error",
            )
            status = error.code
        except Exception:
            result = AgentResult(
                text="Состояние операции будет сверено; создание не подтверждено.", decision="error"
            )
            status = "unknown_outcome"
        await self._audit(
            row,
            status,
            reason="confirmed_operation",
            duration_ms=(self.clock.now() - now).total_seconds() * 1000,
        )
        return result

    async def recover(self):
        from app.db.repositories.reminder_repo import find_result

        for row in await repo.recoverable(self.pool, self.clock.now()):
            result = await find_result(self.pool, row["idempotency_key"])
            if result is not None:
                await repo.complete(self.pool, row["id"], result, self.clock.now())
                await self._audit(row, "succeeded", reason="recovered_committed_result")
                continue
            claimed = await repo.recover_claim(self.pool, row["id"], self.clock.now())
            if claimed:
                await self._execute(claimed)
            elif row["calls_remaining"] <= 0:
                resolved = await repo.finalize_exhausted(self.pool, row["id"], self.clock.now())
                if resolved:
                    await self._audit(
                        resolved,
                        resolved["state"],
                        reason="recovered_committed_result"
                        if resolved["state"] == "succeeded"
                        else "call_budget",
                    )
