import base64
import math
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import (
    MAX_EQUATIONS,
    MAX_FUNCTIONS,
    MAX_PLOT_LEVELS,
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
    PlotType,
)
from mcp_servers.math_tools.syntax import (
    equation_sides,
    expression_variables,
    normalize_expression,
    validate_variable,
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
        variable_name = validate_variable(
            variable if variable is not None else _default_variable(operation)
        )
        if not 1 <= order <= 5:
            raise MathError("invalid_arguments")

        payload: dict[str, object] = {
            "operation": "calculate",
            "calculation": operation,
            "variable": variable_name,
            "order": order,
            "direction": direction,
        }

        if expression is not None:
            payload["expression"] = normalize_expression(expression)

        if variables is not None:
            payload["variables"] = _variable_names(variables)
        if functions is not None:
            payload["functions"] = _normalized_list(functions, MAX_FUNCTIONS)
        if constraints is not None:
            payload["constraints"] = _normalized_equations(constraints, MAX_EQUATIONS)

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

        if operation == CalculateOperation.SERIES:
            lower_text = _bound_text(lower)
            upper_text = _bound_text(upper)
            if lower_text:
                lower_normalized = normalize_expression(lower_text)
                if variable_name in expression_variables(lower_normalized):
                    raise MathError("invalid_arguments")
                payload["lower"] = lower_normalized
            if upper_text:
                upper_normalized = normalize_expression(upper_text)
                if variable_name in expression_variables(upper_normalized):
                    raise MathError("invalid_arguments")
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
        if not 1 <= len(functions) <= MAX_FUNCTIONS:
            raise MathError("invalid_arguments")
        variable_name = validate_variable(
            variable if variable is not None else _default_plot_variable(plot_type)
        )
        parameter_name = validate_variable(parameter)
        y_variable_name = validate_variable(y_variable)
        _validate_range(x_min, x_max)
        _validate_range(y_min, y_max)

        normalized_functions = [normalize_expression(text) for text in functions]
        if plot_type is PlotType.CURVE:
            for normalized in normalized_functions:
                extra = set(expression_variables(normalized)) - {variable_name}
                if extra:
                    raise MathError(
                        "invalid_expression",
                        f"Функция должна зависеть только от {variable_name}.",
                    )
        elif plot_type is PlotType.PARAMETRIC:
            if len(normalized_functions) != 2:
                raise MathError(
                    "invalid_arguments",
                    "Для параметрической кривой нужны две функции x(t) и y(t).",
                )
            for normalized in normalized_functions:
                extra = set(expression_variables(normalized)) - {parameter_name}
                if extra:
                    raise MathError(
                        "invalid_expression",
                        f"Функция должна зависеть только от {parameter_name}.",
                    )
        elif plot_type in (PlotType.CONTOUR, PlotType.SURFACE):
            if len(normalized_functions) != 1:
                raise MathError("invalid_arguments", "Для этого типа графика нужна одна функция.")
            allowed = {variable_name, y_variable_name}
            for normalized in normalized_functions:
                if set(expression_variables(normalized)) - allowed:
                    raise MathError(
                        "invalid_expression",
                        f"Функция должна зависеть только от {variable_name} и {y_variable_name}.",
                    )
        else:
            if len(normalized_functions) != 1:
                raise MathError("invalid_arguments", "Для этого типа графика нужна одна функция.")
            for normalized in normalized_functions:
                extra = set(expression_variables(normalized)) - {variable_name}
                if extra:
                    raise MathError(
                        "invalid_expression",
                        f"Комплексная функция должна зависеть только от {variable_name}.",
                    )

        payload: dict[str, object] = {
            "operation": "plot",
            "plot_type": plot_type,
            "functions": normalized_functions,
            "variable": variable_name,
        }
        if x_min is not None and x_max is not None:
            payload["x_min"] = x_min
            payload["x_max"] = x_max
        if y_min is not None and y_max is not None:
            payload["y_min"] = y_min
            payload["y_max"] = y_max
        if plot_type is PlotType.PARAMETRIC:
            payload["parameter"] = parameter_name
        if plot_type in (PlotType.CONTOUR, PlotType.SURFACE):
            payload["y_variable"] = y_variable_name
        if levels is not None:
            if plot_type is not PlotType.CONTOUR:
                raise MathError("invalid_arguments", "levels применимы только к contour.")
            if not 1 <= len(levels) <= MAX_PLOT_LEVELS:
                raise MathError("invalid_arguments")
            normalized_levels: list[float] = []
            for level in levels:
                if isinstance(level, bool) or not isinstance(level, (int, float)):
                    raise MathError("invalid_arguments")
                number = float(level)
                if not math.isfinite(number):
                    raise MathError("invalid_arguments")
                normalized_levels.append(number)
            payload["levels"] = normalized_levels

        response = await self.runtime.request(payload)
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


def _default_variable(operation: CalculateOperation) -> str:
    return "n" if operation is CalculateOperation.SERIES else "x"


def _default_plot_variable(plot_type: PlotType) -> str:
    if plot_type in (PlotType.DOMAIN_COLORING, PlotType.COMPLEX_HEIGHT):
        return "z"
    return "x"


def _validate_range(lower: float | None, upper: float | None) -> None:
    if (lower is None) != (upper is None):
        raise MathError("invalid_arguments")
    if lower is None or upper is None:
        return
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise MathError("invalid_arguments")


def _variable_names(variables: Sequence[str]) -> list[str]:
    if not 1 <= len(variables) <= MAX_VARIABLES:
        raise MathError("invalid_arguments")
    names = [validate_variable(name) for name in variables]
    if len(set(names)) != len(names):
        raise MathError("invalid_arguments")
    return names


def _normalized_list(values: Sequence[str], maximum: int) -> list[str]:
    if not 1 <= len(values) <= maximum:
        raise MathError("invalid_arguments")
    return [normalize_expression(value) for value in values]


def _normalized_equations(values: Sequence[str], maximum: int) -> list[str]:
    if not 1 <= len(values) <= maximum:
        raise MathError("invalid_arguments")
    result: list[str] = []
    for value in values:
        left, right = equation_sides(value)
        result.append(f"{left} = {right}")
    return result


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
