import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import (
    DEFAULT_X_MAX,
    DEFAULT_X_MIN,
    MAX_EQUATIONS,
    MAX_FUNCTIONS,
    MAX_PLOT_LEVELS,
    MAX_PNG_BYTES,
    MAX_VARIABLES,
)
from mcp_servers.math_tools.operations import WorkerOperation
from mcp_servers.math_tools.schemas import CalculateOperation, LimitDirection, PlotType
from mcp_servers.math_tools.syntax import (
    EquationSyntax,
    ExpressionSyntax,
    parse_equation,
    parse_expression,
    validate_variable,
)

PARAMETRIC_DEFAULT_MIN = 0.0
PARAMETRIC_DEFAULT_MAX = 2 * math.pi


@dataclass(frozen=True)
class CalculateRequest:
    operation: CalculateOperation
    variable: str
    order: int
    direction: LimitDirection
    expression: ExpressionSyntax | None = None
    lower: ExpressionSyntax | None = None
    upper: ExpressionSyntax | None = None
    point: ExpressionSyntax | None = None
    variables: tuple[str, ...] | None = None
    functions: tuple[ExpressionSyntax, ...] | None = None
    constraints: tuple[EquationSyntax, ...] | None = None

    def required_expression(self) -> ExpressionSyntax:
        if self.expression is None:
            raise MathError("invalid_arguments")
        return self.expression

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "operation": WorkerOperation.CALCULATE.value,
            "calculation": self.operation.value,
            "variable": self.variable,
            "order": self.order,
            "direction": self.direction.value,
        }
        if self.expression is not None:
            payload["expression"] = self.expression.normalized
        if self.lower is not None:
            payload["lower"] = self.lower.normalized
        if self.upper is not None:
            payload["upper"] = self.upper.normalized
        if self.point is not None:
            payload["point"] = self.point.normalized
        if self.variables is not None:
            payload["variables"] = list(self.variables)
        if self.functions is not None:
            payload["functions"] = [function.normalized for function in self.functions]
        if self.constraints is not None:
            payload["constraints"] = [constraint.normalized for constraint in self.constraints]
        return payload


@dataclass(frozen=True)
class SolveRequest:
    equations: tuple[EquationSyntax, ...]
    variables: tuple[str, ...]
    real_only: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "operation": WorkerOperation.SOLVE.value,
            "equations": [equation.normalized for equation in self.equations],
            "variables": list(self.variables),
            "real_only": self.real_only,
        }


@dataclass(frozen=True)
class PlotRequest:
    plot_type: PlotType
    functions: tuple[ExpressionSyntax, ...]
    variable: str
    parameter: str
    y_variable: str
    x_min: float
    x_max: float
    y_min: float | None
    y_max: float | None
    levels: tuple[float, ...] | None
    max_png_bytes: int

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "operation": WorkerOperation.PLOT.value,
            "plot_type": self.plot_type.value,
            "functions": [function.normalized for function in self.functions],
            "variable": self.variable,
            "parameter": self.parameter,
            "y_variable": self.y_variable,
            "x_min": self.x_min,
            "x_max": self.x_max,
            "max_png_bytes": self.max_png_bytes,
        }
        if self.y_min is not None:
            payload["y_min"] = self.y_min
        if self.y_max is not None:
            payload["y_max"] = self.y_max
        if self.levels is not None:
            payload["levels"] = list(self.levels)
        return payload


def parse_calculate(payload: Mapping[str, object]) -> CalculateRequest:
    operation = _enum_value(
        payload,
        "calculation",
        CalculateOperation,
        CalculateOperation.SIMPLIFY,
    )
    variable = validate_variable(_text(payload, "variable", _default_variable(operation)))
    order = _integer(payload, "order", 1)
    if not 1 <= order <= 5:
        raise MathError("invalid_arguments")
    direction = _enum_value(payload, "direction", LimitDirection, LimitDirection.BOTH)

    expression = _optional_expression(payload, "expression")
    variables = _optional_variables(payload, "variables", MAX_VARIABLES)
    functions = _optional_expressions(payload, "functions", MAX_FUNCTIONS)
    constraints = _optional_equations(payload, "constraints", MAX_EQUATIONS)
    lower = _optional_bound(payload, "lower", variable)
    upper = _optional_bound(payload, "upper", variable)
    point = _optional_bound(payload, "point")

    if operation is CalculateOperation.JACOBIAN:
        if not functions or not variables:
            raise MathError("invalid_arguments")
        allowed = set(variables)
        for function in functions:
            if set(function.variables) - allowed:
                raise MathError(
                    "invalid_arguments",
                    "Функции Якобиана должны зависеть только от указанных переменных.",
                )
    elif operation is CalculateOperation.CONSTRAINED_EXTREMA:
        if not variables or len(variables) < 2:
            raise MathError(
                "invalid_arguments",
                "Для условных экстремумов укажите не менее двух переменных.",
            )
        if not constraints or len(constraints) != 1:
            raise MathError(
                "invalid_arguments",
                "Для условных экстремумов нужно одно уравнение-ограничение.",
            )
        if expression is None:
            raise MathError("invalid_arguments")
    elif expression is None:
        raise MathError("invalid_arguments")

    if operation is CalculateOperation.INTEGRATE:
        if (lower is None) != (upper is None):
            raise MathError("invalid_arguments")
    elif operation is CalculateOperation.LIMIT:
        if point is None:
            raise MathError("invalid_arguments")

    return CalculateRequest(
        operation=operation,
        variable=variable,
        order=order,
        direction=direction,
        expression=expression,
        lower=lower,
        upper=upper,
        point=point,
        variables=variables,
        functions=functions,
        constraints=constraints,
    )


def parse_solve(payload: Mapping[str, object]) -> SolveRequest:
    equation_texts = _text_list(payload, "equations", MAX_EQUATIONS)
    equations = [parse_equation(text) for text in equation_texts]
    inferred = sorted(
        {
            variable
            for equation in equations
            for variable in (*equation.left.variables, *equation.right.variables)
        }
    )

    requested = _optional_variables(payload, "variables", MAX_VARIABLES)
    if requested is None or len(requested) == 0:
        variables = tuple(inferred)
    else:
        variables = requested
        missing = sorted(set(inferred) - set(variables))
        if missing:
            raise MathError(
                "invalid_arguments",
                "Укажите все неизвестные: " + ", ".join(missing) + ".",
            )

    if not variables or len(variables) > MAX_VARIABLES:
        raise MathError("invalid_arguments")

    return SolveRequest(
        equations=tuple(equations),
        variables=variables,
        real_only=_boolean(payload, "real_only", True),
    )


def parse_plot(payload: Mapping[str, object]) -> PlotRequest:
    plot_type = _enum_value(payload, "plot_type", PlotType, PlotType.CURVE)
    functions = _required_expressions(payload, "functions", MAX_FUNCTIONS)
    variable = validate_variable(_text(payload, "variable", _default_plot_variable(plot_type)))
    parameter = validate_variable(_text(payload, "parameter", "t"))
    y_variable = validate_variable(_text(payload, "y_variable", "y"))

    x_min, x_max = _resolved_range(
        payload,
        "x_min",
        "x_max",
        (PARAMETRIC_DEFAULT_MIN, PARAMETRIC_DEFAULT_MAX)
        if plot_type is PlotType.PARAMETRIC
        else (DEFAULT_X_MIN, DEFAULT_X_MAX),
    )
    y_min, y_max = _resolved_range(payload, "y_min", "y_max", (x_min, x_max))
    if plot_type in (PlotType.CURVE, PlotType.PARAMETRIC):
        y_min, y_max = None, None

    levels = _optional_number_list(payload, "levels", MAX_PLOT_LEVELS)
    if levels is not None:
        if plot_type is not PlotType.CONTOUR:
            raise MathError("invalid_arguments", "levels применимы только к contour.")
        if not levels:
            raise MathError("invalid_arguments", "Для contour нужен хотя бы один уровень.")
        levels = tuple(sorted(set(levels)))

    _validate_plot_dependencies(
        plot_type,
        functions,
        variable,
        parameter,
        y_variable,
    )

    max_png_bytes = _integer(payload, "max_png_bytes", MAX_PNG_BYTES)
    if not 1 <= max_png_bytes <= MAX_PNG_BYTES:
        raise MathError("invalid_arguments")

    return PlotRequest(
        plot_type=plot_type,
        functions=functions,
        variable=variable,
        parameter=parameter,
        y_variable=y_variable,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        levels=levels,
        max_png_bytes=max_png_bytes,
    )


def _validate_plot_dependencies(
    plot_type: PlotType,
    functions: Sequence[ExpressionSyntax],
    variable: str,
    parameter: str,
    y_variable: str,
) -> None:
    if plot_type is PlotType.CURVE:
        for function in functions:
            if set(function.variables) - {variable}:
                raise MathError(
                    "invalid_expression",
                    f"Функция должна зависеть только от {variable}.",
                )
        return

    if plot_type is PlotType.PARAMETRIC:
        if len(functions) != 2:
            raise MathError(
                "invalid_arguments",
                "Для параметрической кривой нужны две функции x(t) и y(t).",
            )
        for function in functions:
            if set(function.variables) - {parameter}:
                raise MathError(
                    "invalid_expression",
                    f"Функция должна зависеть только от {parameter}.",
                )
        return

    if len(functions) != 1:
        raise MathError("invalid_arguments", "Для этого типа графика нужна одна функция.")

    if plot_type in (PlotType.CONTOUR, PlotType.SURFACE):
        if y_variable == variable:
            raise MathError("invalid_arguments")
        for function in functions:
            if set(function.variables) - {variable, y_variable}:
                raise MathError(
                    "invalid_expression",
                    f"Функция должна зависеть только от {variable} и {y_variable}.",
                )
        return

    for function in functions:
        if set(function.variables) - {variable}:
            raise MathError(
                "invalid_expression",
                f"Комплексная функция должна зависеть только от {variable}.",
            )


def _default_variable(operation: CalculateOperation) -> str:
    return "n" if operation is CalculateOperation.SERIES else "x"


def _default_plot_variable(plot_type: PlotType) -> str:
    if plot_type in (PlotType.DOMAIN_COLORING, PlotType.COMPLEX_HEIGHT):
        return "z"
    return "x"


def _resolved_range(
    payload: Mapping[str, object],
    lower_key: str,
    upper_key: str,
    default: tuple[float, float],
) -> tuple[float, float]:
    lower = _optional_number(payload, lower_key)
    upper = _optional_number(payload, upper_key)
    if (lower is None) != (upper is None):
        raise MathError("invalid_arguments")
    if lower is None or upper is None:
        return default
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise MathError("invalid_arguments")
    return lower, upper


def _optional_bound(
    payload: Mapping[str, object],
    key: str,
    variable: str | None = None,
) -> ExpressionSyntax | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise MathError("invalid_arguments")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    text = value.strip()
    if not text:
        return None
    parsed = parse_expression(text)
    if variable is not None and variable in parsed.variables:
        raise MathError("invalid_arguments")
    return parsed


def _optional_expression(payload: Mapping[str, object], key: str) -> ExpressionSyntax | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    return parse_expression(value)


def _optional_expressions(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> tuple[ExpressionSyntax, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    return _expressions(value, maximum)


def _required_expressions(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> tuple[ExpressionSyntax, ...]:
    value = payload.get(key)
    if value is None:
        raise MathError("invalid_arguments")
    return _expressions(value, maximum)


def _expressions(value: object, maximum: int) -> tuple[ExpressionSyntax, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise MathError("invalid_arguments")
    result: list[ExpressionSyntax] = []
    for item in value:
        if not isinstance(item, str):
            raise MathError("invalid_arguments")
        result.append(parse_expression(item))
    return tuple(result)


def _optional_equations(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> tuple[EquationSyntax, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise MathError("invalid_arguments")
    result: list[EquationSyntax] = []
    for item in value:
        if not isinstance(item, str):
            raise MathError("invalid_arguments")
        result.append(parse_equation(item))
    return tuple(result)


def _optional_variables(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> tuple[str, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > maximum:
        raise MathError("invalid_arguments")
    names = tuple(validate_variable(item) for item in value)
    if len(set(names)) != len(names):
        raise MathError("invalid_arguments")
    return names


def _text_list(payload: Mapping[str, object], key: str, maximum: int) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise MathError("invalid_arguments")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MathError("invalid_arguments")
        result.append(item)
    return result


def _text(payload: Mapping[str, object], key: str, default: str) -> str:
    value = payload.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    return value


def _integer(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key, default)
    if value is None:
        value = default
    if type(value) is not int:
        raise MathError("invalid_arguments")
    return value


def _boolean(payload: Mapping[str, object], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if value is None:
        value = default
    if type(value) is not bool:
        raise MathError("invalid_arguments")
    return value


def _optional_number(payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MathError("invalid_arguments")
    number = float(value)
    if not math.isfinite(number):
        raise MathError("invalid_arguments")
    return number


def _optional_number_list(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> tuple[float, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise MathError("invalid_arguments")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise MathError("invalid_arguments")
        number = float(item)
        if not math.isfinite(number):
            raise MathError("invalid_arguments")
        result.append(number)
    return tuple(result)


def _enum_value[T: StrEnum](
    payload: Mapping[str, object],
    key: str,
    enum_type: type[T],
    default: T,
) -> T:
    value = payload.get(key, default)
    if value is None:
        value = default
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    try:
        return enum_type(value)
    except ValueError:
        raise MathError("invalid_arguments") from None


__all__ = [
    "CalculateRequest",
    "PlotRequest",
    "SolveRequest",
    "parse_calculate",
    "parse_plot",
    "parse_solve",
]
