import json
import os
import resource
import sys
from typing import Any

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MEMORY_LIMIT_MIB, OPERATION_TIMEOUT_SECONDS


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _environment_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


MEMORY_LIMIT_MIB = _environment_integer("MATH_MCP_MEMORY_LIMIT_MIB", MEMORY_LIMIT_MIB)
MEMORY_LIMIT_BYTES = MEMORY_LIMIT_MIB * 1024 * 1024
OPERATION_TIMEOUT_SECONDS = _environment_number(
    "MATH_MCP_OPERATION_TIMEOUT_SECONDS",
    OPERATION_TIMEOUT_SECONDS,
)


def _apply_memory_limit() -> None:
    limit = MEMORY_LIMIT_BYTES
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError):
        return


def _memory_report() -> dict[str, object]:
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_AS)
    except (OSError, ValueError):
        soft = MEMORY_LIMIT_BYTES
    return {
        "memory_limit_bytes": int(soft),
        "timeout_seconds": OPERATION_TIMEOUT_SECONDS,
    }


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "message": message}


def _warmup(handle_request: Any) -> None:
    handle_request(
        {
            "operation": "calculate",
            "calculation": "simplify",
            "expression": "x + x",
            "variable": "x",
            "order": 1,
            "direction": "both",
        }
    )
    handle_request(
        {
            "operation": "plot",
            "functions": ["x"],
            "variable": "x",
            "x_min": 0.0,
            "x_max": 1.0,
        }
    )


def _execute(payload: dict[str, Any], handle_request: Any) -> dict[str, object]:
    operation = payload.get("operation")
    if operation == "limits":
        return {"ok": True, "result": _memory_report()}
    if operation == "warmup":
        try:
            _warmup(handle_request)
        except MathError as error:
            return _error(error.code, error.message)
        except MemoryError:
            return _error("memory_limit", "Вычисление превысило лимит памяти.")
        except Exception:
            return _error("evaluation_failed", "Не удалось прогреть вычислитель.")
        return {"ok": True, "result": {"status": "ok", **_memory_report()}}
    try:
        result = handle_request(payload)
    except MathError as error:
        return _error(error.code, error.message)
    except MemoryError:
        return _error("memory_limit", "Вычисление превысило лимит памяти.")
    except RecursionError:
        return _error("resource_limit", "Вычисление превысило допустимую сложность.")
    except OverflowError:
        return _error("resource_limit", "Вычисление превысило допустимый числовой диапазон.")
    except Exception:
        return _error("evaluation_failed", "Не удалось вычислить выражение.")
    return {"ok": True, "result": result}


def main() -> None:
    _apply_memory_limit()
    from mcp_servers.math_tools.engine import handle_request

    _write({"ready": True})
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(_error("invalid_arguments", "Некорректный запрос к вычислителю."))
            continue
        if not isinstance(payload, dict):
            _write(_error("invalid_arguments", "Некорректный запрос к вычислителю."))
            continue
        _write(_execute(payload, handle_request))


def _write(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(encoded.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
