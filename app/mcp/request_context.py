from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.schemas.specialist import SpecialistFact


@dataclass(frozen=True)
class MCPRequestContext:
    user_id: int
    web_search_allowed: bool = False
    reminder_allowed: bool = False
    attempted_sources: set[str] = field(default_factory=set)
    facts: list[SpecialistFact] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.user_id) is not int or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")


_request_context: ContextVar[MCPRequestContext | None] = ContextVar(
    "mcp_request_context", default=None
)


@contextmanager
def bind_mcp_request(context: MCPRequestContext) -> Generator[None]:
    token = _request_context.set(context)
    try:
        yield
    finally:
        _request_context.reset(token)


def current_mcp_request() -> MCPRequestContext | None:
    return _request_context.get()
