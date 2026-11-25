import os
import sys
from collections.abc import Callable, Mapping

try:
    import resource as _resource
except ImportError:
    _resource = None

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.ipc import (
    IpcProtocolError,
    decode_message,
    encode_message,
    error_message,
    ready_message,
    success_message,
)
from mcp_servers.math_tools.limits import MEMORY_LIMIT_MIB, OPERATION_TIMEOUT_SECONDS
from mcp_servers.math_tools.operations import WorkerOperation

type RequestHandler = Callable[[Mapping[str, object]], dict[str, object]]


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
    if _resource is None:
        return
    limit = MEMORY_LIMIT_BYTES
    try:
        _, hard = _resource.getrlimit(_resource.RLIMIT_AS)
        if hard != _resource.RLIM_INFINITY:
            limit = min(limit, hard)
        _resource.setrlimit(_resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError):
        return


def _memory_report() -> dict[str, object]:
    report: dict[str, object] = {
        "memory_limit_bytes": 0,
        "memory_limit_enforced": False,
        "timeout_seconds": OPERATION_TIMEOUT_SECONDS,
    }
    if _resource is None:
        return report
    try:
        soft, _ = _resource.getrlimit(_resource.RLIMIT_AS)
    except (OSError, ValueError):
        return report
    if soft == _resource.RLIM_INFINITY or soft <= 0:
        return report
    report["memory_limit_bytes"] = int(soft)
    report["memory_limit_enforced"] = True
    return report


def _warmup(handle_request: RequestHandler) -> None:
    handle_request(
        {
            "operation": WorkerOperation.CALCULATE.value,
            "calculation": "simplify",
            "expression": "x + x",
            "variable": "x",
            "order": 1,
            "direction": "both",
        }
    )
    handle_request(
        {
            "operation": WorkerOperation.PLOT.value,
            "functions": ["x"],
            "variable": "x",
            "x_min": 0.0,
            "x_max": 1.0,
        }
    )


def _parse_operation(value: object) -> WorkerOperation | None:
    if not isinstance(value, str):
        return None
    try:
        return WorkerOperation(value)
    except ValueError:
        return None


def _execute(payload: Mapping[str, object], handle_request: RequestHandler) -> dict[str, object]:
    operation = _parse_operation(payload.get("operation"))
    if operation is WorkerOperation.LIMITS:
        return success_message({"status": "ok", **_memory_report()})
    if operation is WorkerOperation.WARMUP:
        try:
            _warmup(handle_request)
        except MathError as error:
            return error_message(error.code, error.message)
        except MemoryError:
            return error_message("memory_limit", "Вычисление превысило лимит памяти.")
        except Exception:
            return error_message("evaluation_failed", "Не удалось прогреть вычислитель.")
        return success_message({"status": "ok", **_memory_report()})
    try:
        result = handle_request(payload)
    except MathError as error:
        return error_message(error.code, error.message)
    except MemoryError:
        return error_message("memory_limit", "Вычисление превысило лимит памяти.")
    except RecursionError:
        return error_message("resource_limit", "Вычисление превысило допустимую сложность.")
    except OverflowError:
        return error_message("resource_limit", "Вычисление превысило допустимый числовой диапазон.")
    except Exception:
        return error_message("evaluation_failed", "Не удалось вычислить выражение.")
    return success_message(result)


def main() -> None:
    _apply_memory_limit()
    from mcp_servers.math_tools.engine import handle_request

    _write(ready_message())
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            payload = decode_message(raw_line)
        except IpcProtocolError:
            _write(error_message("invalid_arguments", "Некорректный запрос к вычислителю."))
            continue
        _write(_execute(payload, handle_request))


def _write(payload: dict[str, object]) -> None:
    sys.stdout.buffer.write(encode_message(payload))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
