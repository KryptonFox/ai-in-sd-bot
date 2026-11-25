import base64
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_PNG_BYTES
from mcp_servers.math_tools.payloads import (
    parse_calculate,
    parse_plot,
    parse_solve,
)
from mcp_servers.math_tools.runtime import MathRuntime
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    LimitDirection,
    MathCalculateResult,
    MathPlotResult,
    MathSolveResult,
    PlotOutput,
    PlotType,
)


class MathService:
    def __init__(
        self, runtime: MathRuntime | None = None, *, max_png_bytes: int = MAX_PNG_BYTES
    ) -> None:
        self.runtime = runtime or MathRuntime()
        self._max_png_bytes = max_png_bytes

    async def calculate(
        self,
        expression: str | None = None,
        *,
        operation: CalculateOperation = CalculateOperation.SIMPLIFY,
        variable: str | None = None,
        order: int = 1,
        lower: str | float | int | None = None,
        upper: str | float | int | None = None,
        point: str | float | int | None = None,
        direction: LimitDirection = LimitDirection.BOTH,
        variables: Sequence[str] | None = None,
        functions: Sequence[str] | None = None,
        constraints: Sequence[str] | None = None,
    ) -> MathCalculateResult:
        request = parse_calculate(
            {
                "calculation": operation,
                "expression": expression,
                "variable": variable,
                "order": order,
                "lower": lower,
                "upper": upper,
                "point": point,
                "direction": direction,
                "variables": _optional_list(variables),
                "functions": _optional_list(functions),
                "constraints": _optional_list(constraints),
            }
        )
        response = await self.runtime.request(request.to_payload())
        return _calculate_result(response)

    async def solve(
        self,
        equations: Sequence[str],
        *,
        variables: Sequence[str] | None = None,
        real_only: bool = True,
    ) -> MathSolveResult:
        request = parse_solve(
            {
                "equations": list(equations),
                "variables": _optional_list(variables),
                "real_only": real_only,
            }
        )
        response = await self.runtime.request(request.to_payload())
        return _solve_result(response)

    async def plot(
        self,
        functions: Sequence[str],
        *,
        plot_type: PlotType = PlotType.CURVE,
        variable: str | None = None,
        parameter: str = "t",
        y_variable: str = "y",
        x_min: float | None = None,
        x_max: float | None = None,
        y_min: float | None = None,
        y_max: float | None = None,
        levels: Sequence[float] | None = None,
    ) -> PlotOutput:
        request = parse_plot(
            {
                "plot_type": plot_type,
                "functions": list(functions),
                "variable": variable,
                "parameter": parameter,
                "y_variable": y_variable,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "levels": list(levels) if levels is not None else None,
                "max_png_bytes": self._max_png_bytes,
            }
        )
        response = await self.runtime.request(request.to_payload())
        result_payload = response.get("result")
        encoded_png = response.get("png_base64")
        if not isinstance(result_payload, dict) or not isinstance(encoded_png, str):
            raise MathError("evaluation_failed")
        try:
            result = MathPlotResult.model_validate(result_payload)
            png = base64.b64decode(encoded_png, validate=True)
        except (ValidationError, ValueError):
            raise MathError("evaluation_failed") from None

        if (
            not png.startswith(b"\x89PNG\r\n\x1a\n")
            or len(png) > MAX_PNG_BYTES
            or result.png_bytes != len(png)
        ):
            raise MathError("evaluation_failed")
        if len(png) > self._max_png_bytes:
            raise MathError("resource_limit", "PNG превышает настроенный предел размера.")

        return PlotOutput(result=result, png=png)

    async def aclose(self) -> None:
        await self.runtime.aclose()


def _optional_list(values: Sequence[str] | None) -> list[str] | None:
    return list(values) if values is not None else None


def _calculate_result(payload: Mapping[str, object]) -> MathCalculateResult:
    try:
        return MathCalculateResult.model_validate(payload)
    except ValidationError:
        raise MathError("evaluation_failed") from None


def _solve_result(payload: Mapping[str, object]) -> MathSolveResult:
    try:
        return MathSolveResult.model_validate(payload)
    except ValidationError:
        raise MathError("evaluation_failed") from None
