from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from time import monotonic
from uuid import UUID, uuid4

from app.core.clock import SystemClock
from app.core.exceptions import LLMError
from app.core.schemas.tools import ToolFailure


class TraceStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass
class TraceStep:
    component: str
    action: str
    started_at: datetime
    parent_id: UUID | None
    id: UUID = field(default_factory=uuid4)
    duration_ms: float = 0
    status: TraceStatus = TraceStatus.OK
    tool_name: str | None = None
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_code: str | None = None

    def fail(self, code: str) -> None:
        self.status = TraceStatus.ERROR
        self.error_code = code if code.isascii() and code.replace("_", "").isalnum() else "error"


@dataclass
class OperationTrace:
    user_id: int
    request_id: str
    started_at: datetime
    id: UUID = field(default_factory=uuid4)
    route: str | None = None
    steps: list[TraceStep] = field(default_factory=list)


_trace: ContextVar[OperationTrace | None] = ContextVar("operation_trace", default=None)
_parent: ContextVar[UUID | None] = ContextVar("operation_trace_parent", default=None)


@contextmanager
def bind_trace(trace: OperationTrace) -> Generator[None]:
    token = _trace.set(trace)
    parent_token = _parent.set(None)
    try:
        yield
    finally:
        _parent.reset(parent_token)
        _trace.reset(token)


def current_trace() -> OperationTrace | None:
    return _trace.get()


@contextmanager
def trace_step(
    component: str,
    action: str,
    *,
    tool_name: str | None = None,
    model_name: str | None = None,
) -> Generator[TraceStep | None]:
    trace = current_trace()
    if trace is None:
        yield None
        return

    started = monotonic()
    step = TraceStep(
        component,
        action,
        SystemClock().now(),
        _parent.get(),
        tool_name=tool_name,
        model_name=model_name,
    )
    trace.steps.append(step)
    token = _parent.set(step.id)
    try:
        yield step
    except BaseException as error:
        step.fail(
            str(error.code) if isinstance(error, (LLMError, ToolFailure)) else type(error).__name__
        )
        raise
    finally:
        step.duration_ms = (monotonic() - started) * 1000
        _parent.reset(token)
