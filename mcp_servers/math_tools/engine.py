from collections.abc import Mapping

from mcp_servers.math_tools.calculus import calculate as run_calculation
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.operations import WorkerOperation
from mcp_servers.math_tools.payloads import (
    parse_calculate,
    parse_plot,
    parse_solve,
)
from mcp_servers.math_tools.plot_dispatch import plot as run_plot
from mcp_servers.math_tools.solver import solve as run_solve


def handle_request(payload: Mapping[str, object]) -> dict[str, object]:
    operation = _operation(payload)
    if operation is WorkerOperation.CALCULATE:
        return calculate(payload)
    if operation is WorkerOperation.SOLVE:
        return solve(payload)
    if operation is WorkerOperation.PLOT:
        return plot(payload)
    raise MathError("invalid_arguments")


def calculate(payload: Mapping[str, object]) -> dict[str, object]:
    return run_calculation(parse_calculate(payload))


def solve(payload: Mapping[str, object]) -> dict[str, object]:
    return run_solve(parse_solve(payload))


def plot(payload: Mapping[str, object]) -> dict[str, object]:
    return run_plot(parse_plot(payload))


def _operation(payload: Mapping[str, object]) -> WorkerOperation:
    value = payload.get("operation")
    if isinstance(value, WorkerOperation):
        return value
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    try:
        return WorkerOperation(value)
    except ValueError:
        raise MathError("invalid_arguments") from None


__all__ = [
    "calculate",
    "handle_request",
    "plot",
    "solve",
]
