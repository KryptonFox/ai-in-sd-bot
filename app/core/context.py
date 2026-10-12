from contextvars import ContextVar, Token
from uuid import uuid4

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def generate_request_id() -> str:
    return uuid4().hex[:8]


def get_request_id() -> str:
    return _request_id_ctx.get()


def set_request_id(request_id: str) -> Token[str]:
    return _request_id_ctx.set(request_id)
