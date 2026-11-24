import base64
import math
from collections.abc import Mapping, Sequence
from typing import cast

import sympy
from sympy import Basic, Expr, Symbol

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    DomainRelation,
    ParsedEquation,
    ParsedExpression,
    build_equation,
    build_expression,
    build_parsed_expression,
    build_variable,
)
from mcp_servers.math_tools.limits import (
    MAX_EQUATIONS,
    MAX_FUNCTIONS,
    MAX_RESULT_LENGTH,
    MAX_SOLUTIONS,
    MAX_VARIABLES,
    PLOT_SAMPLES,
)
from mcp_servers.math_tools.plotting import render_plot
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    LimitDirection,
    MathCalculateResult,
    MathPlotResult,
    MathSolveResult,
    PlotStatus,
    SolveStatus,
)
from mcp_servers.math_tools.syntax import validate_variable

FALLBACK_DIGITS = 50
LIMIT_DIRECTIONS: dict[LimitDirection, str] = {
    LimitDirection.BOTH: "+-",
    LimitDirection.LEFT: "-",
    LimitDirection.RIGHT: "+",
}


def handle_request(payload: Mapping[str, object]) -> dict[str, object]:
    operation = _text(payload, "operation")
    if operation == "calculate":
        return calculate(payload)
    if operation == "solve":
        return solve(payload)
    if operation == "plot":
        return plot(payload)
    raise MathError("invalid_arguments")


def calculate(payload: Mapping[str, object]) -> dict[str, object]:
    expression_text = _text(payload, "expression")
    operation = _text(payload, "calculation", "simplify")
    try:
        typed_operation = CalculateOperation(operation)
    except ValueError:
        raise MathError("invalid_arguments") from None
    variable = validate_variable(_text(payload, "variable", "x"))
    symbol = build_variable(variable)
    expression = build_expression(expression_text)

    exact = typed_operation is not CalculateOperation.NUMERIC
    value: Expr
    detail: str

    if typed_operation is CalculateOperation.SIMPLIFY:
        value = sympy.simplify(expression)
        detail = "Выражение упрощено."
    elif typed_operation is CalculateOperation.EVALUATE:
        value = sympy.simplify(expression.doit())
        detail = "Точное значение вычислено."
    elif typed_operation is CalculateOperation.NUMERIC:
        numeric_value = sympy.N(expression, 15)
        if numeric_value.free_symbols:
            return _undefined_calculate(
                typed_operation,
                expression_text,
                variable,
                exact,
                "Численное значение требует подстановки переменных.",
            )
        if _has_undefined(numeric_value):
            return _undefined_calculate(
                typed_operation,
                expression_text,
                variable,
                exact,
                "Численный результат не определён.",
            )
        numeric_text = _checked_text(_numeric_text(numeric_value))
        if not numeric_text:
            return _undefined_calculate(
                typed_operation,
                expression_text,
                variable,
                exact,
                "Численный результат не является конечным вещественным числом.",
            )
        return _calculate_payload(
            typed_operation,
            expression_text,
            variable,
            exact,
            numeric_text,
            numeric_text,
            "Численное значение с 15 значащими цифрами.",
        )
    elif typed_operation is CalculateOperation.DIFF:
        order = _integer(payload, "order", 1)
        if not 1 <= order <= 5:
            raise MathError("invalid_arguments")
        value = sympy.diff(expression, symbol, order)
        detail = f"Производная порядка {order} по переменной {variable}."
    elif typed_operation is CalculateOperation.INTEGRATE:
        value, detail = _integrate(expression, symbol, variable, payload)
    elif typed_operation is CalculateOperation.LIMIT:
        value, detail = _limit(expression, symbol, variable, payload)
    else:
        raise MathError("invalid_arguments")

    if _has_undefined(value):
        return _undefined_calculate(
            typed_operation,
            expression_text,
            variable,
            exact,
            _undefined_detail(value),
        )

    value = sympy.simplify(value)
    if _has_undefined(value):
        return _undefined_calculate(
            typed_operation,
            expression_text,
            variable,
            exact,
            _undefined_detail(value),
        )

    result_text = _checked_text(sympy.sstr(value))
    numeric_text = _checked_text(_numeric_text(value))
    return _calculate_payload(
        typed_operation,
        expression_text,
        variable,
        exact,
        result_text,
        numeric_text,
        detail,
    )


def _integrate(
    expression: Expr,
    symbol: sympy.Symbol,
    variable: str,
    payload: Mapping[str, object],
) -> tuple[Expr, str]:
    lower_text = _optional_text(payload, "lower")
    upper_text = _optional_text(payload, "upper")
    if bool(lower_text) != bool(upper_text):
        raise MathError("invalid_arguments")

    if not lower_text or not upper_text:
        return sympy.integrate(expression, symbol), f"Неопределённый интеграл по {variable}."

    lower = build_expression(lower_text)
    upper = build_expression(upper_text)
    if symbol in lower.free_symbols or symbol in upper.free_symbols:
        raise MathError("invalid_arguments")
    return (
        sympy.integrate(expression, (symbol, lower, upper)),
        f"Определённый интеграл по {variable} от {lower_text} до {upper_text}.",
    )


def _limit(
    expression: Expr,
    symbol: sympy.Symbol,
    variable: str,
    payload: Mapping[str, object],
) -> tuple[Expr, str]:
    point_text = _optional_text(payload, "point")
    if not point_text:
        raise MathError("invalid_arguments")
    direction_text = _text(payload, "direction", "both")
    try:
        direction = LimitDirection(direction_text)
    except ValueError:
        raise MathError("invalid_arguments") from None
    point = build_expression(point_text)
    value = sympy.limit(expression, symbol, point, dir=LIMIT_DIRECTIONS[direction])
    detail = f"Предел по {variable} в точке {point_text}, направление {direction.value}."
    return value, detail


def solve(payload: Mapping[str, object]) -> dict[str, object]:
    equation_texts = _text_list(payload, "equations", MAX_EQUATIONS)
    if not equation_texts:
        raise MathError("invalid_arguments")
    real_only = _boolean(payload, "real_only", True)
    equations = [build_equation(text) for text in equation_texts]

    inferred = sorted({str(symbol) for equation in equations for symbol in equation.free_symbols})
    requested = _optional_text_list(payload, "variables")
    if requested is None:
        variables = inferred
    else:
        variables = [validate_variable(name) for name in requested]
        if len(set(variables)) != len(variables):
            raise MathError("invalid_arguments")
        missing = sorted(set(inferred) - set(variables))
        if missing:
            raise MathError(
                "invalid_arguments",
                "Укажите все неизвестные: " + ", ".join(missing) + ".",
            )

    if not variables or len(variables) > MAX_VARIABLES:
        raise MathError("invalid_arguments")
    symbols = [build_variable(name) for name in variables]
    display_equations = [equation.normalized for equation in equations]

    candidates, classification = _solve_candidates(equations, symbols, real_only)
    if classification == "infinite_solutions":
        return _solve_payload(
            status=SolveStatus.INFINITE_SOLUTIONS,
            equations=display_equations,
            variables=variables,
            real_only=real_only,
            detail="Уравнение имеет бесконечно много решений; уточните ограничения.",
        )
    if classification == "undefined":
        return _solve_payload(
            status=SolveStatus.UNDEFINED,
            equations=display_equations,
            variables=variables,
            real_only=real_only,
            detail="Не удалось однозначно определить множество решений.",
        )

    if len(candidates) > MAX_SOLUTIONS:
        raise MathError("resource_limit", "Слишком много решений для выдачи.")

    accepted: list[dict[str, str]] = []
    numeric: list[dict[str, str]] = []
    discarded: list[dict[str, str]] = []

    for candidate in candidates:
        exact_solution = {
            variable: _checked_text(sympy.sstr(candidate[symbol]))
            for variable, symbol in zip(variables, symbols, strict=True)
        }
        if not _verify_solution(equations, candidate, symbols, real_only):
            discarded.append(exact_solution)
            continue

        accepted.append(exact_solution)
        numeric.append(
            {
                variable: _checked_text(_numeric_text(candidate[symbol]))
                for variable, symbol in zip(variables, symbols, strict=True)
            }
        )

    accepted, numeric = _sort_solutions(accepted, numeric, variables)
    if accepted:
        return _solve_payload(
            status=SolveStatus.SOLUTIONS,
            equations=display_equations,
            variables=variables,
            real_only=real_only,
            solutions=accepted,
            numeric_solutions=numeric,
            discarded=discarded,
            verified=True,
            detail=(
                "Все решения проверены подстановкой в исходные уравнения и по области определения."
            ),
        )

    detail = "Вещественных решений нет." if real_only else "Решений нет."
    if discarded:
        detail = (
            "Кандидаты решений не прошли проверку по исходным уравнениям или области определения."
        )
    return _solve_payload(
        status=SolveStatus.NO_SOLUTION,
        equations=display_equations,
        variables=variables,
        real_only=real_only,
        discarded=discarded,
        detail=detail,
    )


def _solve_candidates(
    equations: Sequence[ParsedEquation],
    symbols: Sequence[sympy.Symbol],
    real_only: bool,
) -> tuple[list[dict[sympy.Symbol, Expr]], str]:
    if len(equations) == 1 and len(symbols) == 1:
        domain = sympy.S.Reals if real_only else sympy.S.Complexes
        try:
            solution_set = sympy.solveset(equations[0].difference, symbols[0], domain=domain)
        except (NotImplementedError, ValueError):
            solution_set = None
        finite_values = _finite_set_values(solution_set)
        if finite_values is not None:
            return ([{symbols[0]: value} for value in finite_values], "finite")
        if solution_set is None or isinstance(solution_set, sympy.ConditionSet):
            return [], "undefined"
        return [], "infinite_solutions"

    try:
        raw_solutions = sympy.solve(
            [equation.difference for equation in equations],
            list(symbols),
            dict=True,
        )
    except (NotImplementedError, ValueError, TypeError):
        return [], "undefined"

    if isinstance(raw_solutions, dict):
        raw_solutions = [raw_solutions]
    if not isinstance(raw_solutions, list):
        return [], "undefined"
    if not raw_solutions:
        return [], "finite"

    candidates: list[dict[sympy.Symbol, Expr]] = []
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            return [], "undefined"
        candidate: dict[sympy.Symbol, Expr] = {}
        for symbol in symbols:
            value = raw_solution.get(symbol)
            if value is None:
                return [], "undefined"
            if value.free_symbols & set(symbols):
                return [], "infinite_solutions"
            candidate[symbol] = value
        candidates.append(candidate)
    return candidates, "finite"


def _finite_set_values(solution_set: object) -> list[Expr] | None:
    if solution_set is None:
        return None
    if solution_set == sympy.S.EmptySet:
        return []
    if isinstance(solution_set, sympy.FiniteSet):
        return [value for value in solution_set if isinstance(value, Expr)]
    if isinstance(solution_set, sympy.Union):
        values: list[Expr] = []
        for part in solution_set.args:
            if not isinstance(part, sympy.FiniteSet):
                return None
            values.extend(value for value in part if isinstance(value, Expr))
        return values
    return None


def _verify_solution(
    equations: Sequence[ParsedEquation],
    solution: Mapping[sympy.Symbol, Expr],
    symbols: Sequence[sympy.Symbol],
    real_only: bool,
) -> bool:
    if real_only:
        for symbol in symbols:
            if not _is_real_value(solution[symbol]):
                return False

    substitutions = {symbol: solution[symbol] for symbol in symbols}
    for equation in equations:
        constraints = equation.left.constraints + equation.right.constraints
        if not _constraints_hold(constraints, substitutions, real_only):
            return False

        original_residual = sympy.Add(
            equation.left.original,
            sympy.Mul(sympy.Integer(-1), equation.right.original, evaluate=False),
            evaluate=False,
        )
        if not _is_zero_expression(_substitute(original_residual, substitutions)):
            return False

    return True


def _constraints_hold(
    constraints: Sequence[DomainConstraint],
    substitutions: Mapping[Symbol, Expr],
    real_only: bool,
) -> bool:
    for constraint in constraints:
        if constraint.real_only and not real_only:
            continue
        value = _substitute(constraint.expression, substitutions)
        if not _relation_holds(value, constraint.relation):
            return False
    return True


def _relation_holds(value: Expr, relation: DomainRelation) -> bool:
    if relation is DomainRelation.NONZERO:
        return _is_nonzero(value)

    sign = _sign(value)
    if sign is None:
        return False
    if relation is DomainRelation.POSITIVE:
        return sign > 0
    if relation is DomainRelation.NONNEGATIVE:
        return sign >= 0
    if relation is DomainRelation.NEGATIVE:
        return sign < 0
    if relation is DomainRelation.NONPOSITIVE:
        return sign <= 0
    return False


def _sign(value: Expr) -> int | None:
    if value.is_zero is True:
        return 0
    if value.is_positive is True:
        return 1
    if value.is_negative is True:
        return -1

    simplified = sympy.simplify(value)
    if simplified.is_zero is True:
        return 0
    if simplified.is_positive is True:
        return 1
    if simplified.is_negative is True:
        return -1

    numeric = _bounded_numeric(simplified)
    if numeric is None:
        return None
    if numeric.is_zero is True:
        return 0
    if numeric.is_positive is True:
        return 1
    if numeric.is_negative is True:
        return -1
    return None


def _is_nonzero(value: Expr) -> bool:
    if value.is_zero is True:
        return False
    if value.is_zero is False:
        return True

    simplified = sympy.simplify(value)
    if simplified.is_zero is True:
        return False
    if simplified.is_zero is False:
        return True

    numeric = _bounded_numeric(simplified)
    return numeric is not None and numeric.is_zero is not True


def _is_zero_expression(value: Expr) -> bool:
    if value.is_zero is True:
        return True
    if value.is_zero is False:
        return False
    if _has_undefined(value):
        return False

    simplified = sympy.simplify(value)
    if simplified.is_zero is True:
        return True
    if simplified.is_zero is False:
        return False

    numeric = _bounded_numeric(simplified)
    return numeric is not None and numeric.is_zero is True


def _is_real_value(value: Expr) -> bool:
    if value.is_real is True:
        return True
    if value.is_real is False:
        return False

    numeric = _bounded_numeric(value)
    return numeric is not None and numeric.is_real is True


def _bounded_numeric(value: Expr) -> Expr | None:
    if value.free_symbols:
        return None
    try:
        numeric = sympy.N(value, FALLBACK_DIGITS)
    except (TypeError, ValueError):
        return None
    if _has_undefined(numeric):
        return None
    if numeric in (sympy.oo, -sympy.oo):
        return None
    return numeric


def _substitute(
    expression: Basic,
    substitutions: Mapping[Symbol, Expr],
) -> Expr:
    return cast(Expr, expression.subs(list(substitutions.items())))


def plot(payload: Mapping[str, object]) -> dict[str, object]:
    function_texts = _text_list(payload, "functions", MAX_FUNCTIONS)
    if not function_texts:
        raise MathError("invalid_arguments")
    variable = validate_variable(_text(payload, "variable", "x"))
    symbol = build_variable(variable)
    x_min = _number(payload, "x_min", -10.0)
    x_max = _number(payload, "x_max", 10.0)
    if not math.isfinite(x_min) or not math.isfinite(x_max) or x_min >= x_max:
        raise MathError("invalid_arguments")

    parsed_functions: list[tuple[str, ParsedExpression]] = []
    for text in function_texts:
        parsed = build_parsed_expression(text)
        extra_symbols = parsed.expression.free_symbols - {symbol}
        if extra_symbols:
            raise MathError(
                "invalid_expression",
                f"Функция должна зависеть только от {variable}.",
            )
        parsed_functions.append((parsed.text, parsed))

    png, series, has_points = render_plot(
        parsed_functions,
        variable,
        symbol,
        x_min,
        x_max,
        PLOT_SAMPLES,
    )
    status = PlotStatus.OK if has_points else PlotStatus.UNDEFINED
    detail = (
        "График построен; разрывы и участки вне вещественной области разорваны."
        if has_points
        else "Ни одна функция не имеет вещественных значений на выбранном диапазоне."
    )
    result = MathPlotResult(
        status=status,
        functions=series,
        variable=variable,
        x_min=x_min,
        x_max=x_max,
        samples=PLOT_SAMPLES,
        png_bytes=len(png),
        detail=detail,
    )
    return {
        "result": result.model_dump(mode="json"),
        "png_base64": _encode_png(png),
    }


def _encode_png(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


def _sort_solutions(
    solutions: list[dict[str, str]],
    numeric: list[dict[str, str]],
    variables: Sequence[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    indexed = list(zip(solutions, numeric, strict=True))
    indexed.sort(key=lambda pair: tuple(pair[0][variable] for variable in variables))
    return [pair[0] for pair in indexed], [pair[1] for pair in indexed]


def _calculate_payload(
    operation: CalculateOperation,
    expression: str,
    variable: str,
    exact: bool,
    result: str,
    numeric: str,
    detail: str,
) -> dict[str, object]:
    return MathCalculateResult(
        status=CalculateStatus.OK,
        operation=operation,
        expression=expression.strip(),
        variable=variable,
        result=result,
        numeric=numeric,
        exact=exact,
        detail=detail,
    ).model_dump(mode="json")


def _undefined_calculate(
    operation: CalculateOperation,
    expression: str,
    variable: str,
    exact: bool,
    detail: str,
) -> dict[str, object]:
    return MathCalculateResult(
        status=CalculateStatus.UNDEFINED,
        operation=operation,
        expression=expression.strip(),
        variable=variable,
        result="",
        numeric="",
        exact=exact,
        detail=detail,
    ).model_dump(mode="json")


def _solve_payload(
    *,
    status: SolveStatus,
    equations: Sequence[str],
    variables: Sequence[str],
    real_only: bool,
    solutions: Sequence[Mapping[str, str]] = (),
    numeric_solutions: Sequence[Mapping[str, str]] = (),
    discarded: Sequence[Mapping[str, str]] = (),
    verified: bool = False,
    detail: str,
) -> dict[str, object]:
    return MathSolveResult(
        status=status,
        equations=list(equations),
        variables=list(variables),
        solutions=[dict(solution) for solution in solutions],
        numeric_solutions=[dict(solution) for solution in numeric_solutions],
        discarded=[dict(solution) for solution in discarded],
        real_only=real_only,
        verified=verified,
        detail=detail,
    ).model_dump(mode="json")


def _has_undefined(value: Expr) -> bool:
    if value is sympy.nan or value is sympy.zoo:
        return True
    if value.has(sympy.nan, sympy.zoo):
        return True
    if value.has(sympy.Integral, sympy.Derivative, sympy.Limit):
        return True
    return bool(value.has(sympy.AccumBounds))


def _undefined_detail(value: Expr) -> str:
    if value in (sympy.nan, sympy.zoo) or value.has(sympy.nan, sympy.zoo):
        return "Результат не определён: выражение содержит nan или zoo."
    if value.has(sympy.Integral, sympy.Derivative, sympy.Limit):
        return "Результат остался в невычисленном виде."
    if value.has(sympy.AccumBounds):
        return "Предел не существует: значения колеблются."
    return "Результат не определён."


def _numeric_text(value: Expr) -> str:
    if value.free_symbols or not value.is_number:
        return ""
    if _has_undefined(value):
        return ""
    if value in (sympy.oo, -sympy.oo):
        return ""
    try:
        numeric = sympy.N(value, 15)
    except (TypeError, ValueError):
        return ""
    if _has_undefined(numeric):
        return ""
    return sympy.sstr(numeric)


def _checked_text(text: str) -> str:
    if len(text) > MAX_RESULT_LENGTH:
        raise MathError("resource_limit", "Результат слишком велик для выдачи.")
    return text


def _text(payload: Mapping[str, object], key: str, default: str | None = None) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    return value


def _optional_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if isinstance(value, bool):
        raise MathError("invalid_arguments")
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise MathError("invalid_arguments")
    return value.strip()


def _integer(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key, default)
    if type(value) is not int:
        raise MathError("invalid_arguments")
    return value


def _number(payload: Mapping[str, object], key: str, default: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MathError("invalid_arguments")
    return float(value)


def _boolean(payload: Mapping[str, object], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if type(value) is not bool:
        raise MathError("invalid_arguments")
    return value


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


def _optional_text_list(payload: Mapping[str, object], key: str) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_VARIABLES:
        raise MathError("invalid_arguments")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MathError("invalid_arguments")
        result.append(item)
    return result


__all__ = [
    "calculate",
    "handle_request",
    "plot",
    "solve",
]
