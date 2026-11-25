import json
from collections.abc import Mapping


class IpcProtocolError(Exception):
    pass


def encode_message(payload: Mapping[str, object]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded.encode("utf-8") + b"\n"


def decode_message(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IpcProtocolError from error
    if not isinstance(value, dict):
        raise IpcProtocolError
    return value


def ready_message() -> dict[str, object]:
    return {"ready": True}


def success_message(result: Mapping[str, object]) -> dict[str, object]:
    return {"ok": True, "result": dict(result)}


def error_message(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "message": message}
