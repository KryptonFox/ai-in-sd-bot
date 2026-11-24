import base64
import math
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import (
    MAX_EQUATIONS,
    MAX_FUNCTIONS,
    MAX_PNG_BYTES,
    MAX_VARIABLES,
)
from mcp_servers.math_tools.runtime import MathRuntime
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    LimitDirection,
    MathCalculateResult,
    MathPlotResult,
    MathSolveResult,
    PlotOutput,
)
from mcp_servers.math_tools.syntax import (
    equation_sides,
    expression_variables,
    normalize_expression,
    validate_variable,
)


class MathService:
    def __init__(self, runtime: MathRuntime | None = None) -> None:
        self.runtime = runtime or MathRuntime()

    async def calculate(
        self,
        expression: str,
        *,
        operation: CalculateOperation = CalculateOperation.SIMPLIFY,
        variable: str = "x",
        order: int = 1,
        lower: str | float | int | None = None,
        upper: str | float | int | None = None,
        point: str | float | int | None = None,
        direction: LimitDirection = LimitDirection.BOTH,
    ) -> MathCalculateResult:
        normalized = normalize_expression(expression)
        variable_name = validate_variable(variable)
        if not 1 <= order <= 5:
            raise MathError("invalid_arguments")

        payload: dict[str, object] = {
            "operation": "calculate",
            "calculation": operation,
            "expression": normalized,
            "variable": variable_name,
            "order": order,
            "direction": direction,
        }

        if operation == CalculateOperation.INTEGRATE:
            lower_text = _bound_text(lower)
            upper_text = _bound_text(upper)
            if bool(lower_text) != bool(upper_text):
                raise MathError("invalid_arguments")
            if lower_text and upper_text:
                lower_normalized = normalize_expression(lower_text)
                upper_normalized = normalize_expression(upper_text)
                if variable_name in expression_variables(lower_normalized):
                    raise MathError("invalid_arguments")
                if variable_name in expression_variables(upper_normalized):
                    raise MathError("invalid_arguments")
                payload["lower"] = lower_normalized
                payload["upper"] = upper_normalized

        if operation == CalculateOperation.LIMIT:
            point_text = _bound_text(point)
            if not point_text:
                raise MathError("invalid_arguments")
            payload["point"] = normalize_expression(point_text)

        response = await self.runtime.request(payload)
        return _calculate_result(response)

    async def solve(
        self,
        equations: Sequence[str],
        *,
        variables: Sequence[str] | None = None,
        real_only: bool = True,
    ) -> MathSolveResult:
        if not 1 <= len(equations) <= MAX_EQUATIONS:
            raise MathError("invalid_arguments")

        free_variables: set[str] = set()
        for text in equations:
            left, right = equation_sides(text)
            free_variables.update(expression_variables(left))
            free_variables.update(expression_variables(right))

        if variables is None or len(variables) == 0:
            variable_names = sorted(free_variables)
        else:
            if not 1 <= len(variables) <= MAX_VARIABLES:
                raise MathError("invalid_arguments")
            variable_names = [validate_variable(name) for name in variables]
            if len(set(variable_names)) != len(variable_names):
                raise MathError("invalid_arguments")
            missing = sorted(free_variables - set(variable_names))
            if missing:
                raise MathError(
                    "invalid_arguments",
                    "Укажите все неизвестные: " + ", ".join(missing) + ".",
                )

        if not variable_names or len(variable_names) > MAX_VARIABLES:
            raise MathError("invalid_arguments")

        response = await self.runtime.request(
            {
                "operation": "solve",
                "equations": list(equations),
                "variables": variable_names,
                "real_only": real_only,
            }
        )
        return _solve_result(response)

    async def plot(
        self,
        functions: Sequence[str],
        *,
        variable: str = "x",
        x_min: float = -10.0,
        x_max: float = 10.0,
    ) -> PlotOutput:
        if not 1 <= len(functions) <= MAX_FUNCTIONS:
            raise MathError("invalid_arguments")
        variable_name = validate_variable(variable)
        if not math.isfinite(x_min) or not math.isfinite(x_max) or x_min >= x_max:
            raise MathError("invalid_arguments")

        normalized_functions: list[str] = []
        for text in functions:
            normalized = normalize_expression(text)
            extra = set(expression_variables(normalized)) - {variable_name}
            if extra:
                raise MathError(
                    "invalid_expression",
                    f"Функция должна зависеть только от {variable_name}.",
                )
            normalized_functions.append(normalized)

        response = await self.runtime.request(
            {
                "operation": "plot",
                "functions": normalized_functions,
                "variable": variable_name,
                "x_min": x_min,
                "x_max": x_max,
            }
        )
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
        return PlotOutput(result=result, png=png)

    async def aclose(self) -> None:
        await self.runtime.aclose()


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


def _bound_text(value: str | float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise MathError("invalid_arguments")
    return str(value).strip()
