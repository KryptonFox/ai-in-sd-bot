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
    DEFAULT_X_MAX,
    DEFAULT_X_MIN,
    MAX_EQUATIONS,
    MAX_FUNCTIONS,
    MAX_PLOT_LEVELS,
    MAX_RESULT_LENGTH,
    MAX_SOLUTIONS,
    MAX_VARIABLES,
    PLOT_SAMPLES,
)
from mcp_servers.math_tools.plotting import (
    PlotRendering,
    render_complex_height,
    render_contour,
    render_curve,
    render_domain_coloring,
    render_parametric,
    render_surface,
)
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    ConvergenceStatus,
    ExtremaKind,
    LimitDirection,
    MathCalculateResult,
    MathExtremum,
    MathPartial,
    MathPlotResult,
    MathPlotZero,
    MathSolveResult,
    PlotStatus,
    PlotType,
    SolveStatus,
)
from mcp_servers.math_tools.syntax import validate_variable

FALLBACK_DIGITS = 50
LIMIT_DIRECTIONS: dict[LimitDirection, str] = {
    LimitDirection.BOTH: "+-",
    LimitDirection.LEFT: "-",
    LimitDirection.RIGHT: "+",
}
PARAMETRIC_DEFAULT_MIN = 0.0
PARAMETRIC_DEFAULT_MAX = 2 * math.pi


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
    operation_text = _text(payload, "calculation", "simplify")
    try:
        typed_operation = CalculateOperation(operation_text)
    except ValueError:
        raise MathError("invalid_arguments") from None

    if typed_operation is CalculateOperation.JACOBIAN:
        return _calculate_jacobian(payload)
    if typed_operation is CalculateOperation.CONSTRAINED_EXTREMA:
        return _calculate_constrained_extrema(payload)

    variable = validate_variable(_text(payload, "variable", "x"))
    symbol = build_variable(variable)

    if typed_operation is CalculateOperation.SERIES:
        return _calculate_series(payload, variable, symbol)

    expression_text = _text(payload, "expression")
    expression = build_expression(expression_text)
    exact = typed_operation is not CalculateOperation.NUMERIC
    value: Expr | None = None
    detail = ""
    convergence: ConvergenceStatus | None = None

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
    elif typed_operation is CalculateOperation.PARTIAL_DIFF:
        return _calculate_partial_diff(payload, expression_text, expression, variable)
    elif typed_operation is CalculateOperation.INTEGRATE:
        value, detail, convergence = _integrate(expression, symbol, variable, payload)
    elif typed_operation is CalculateOperation.LIMIT:
        value, detail = _limit(expression, symbol, variable, payload)
    else:
        raise MathError("invalid_arguments")

    if value is None:
        return _undefined_calculate(
            typed_operation,
            expression_text,
            variable,
            exact,
            detail,
        )

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
        convergence=convergence,
    )


def _calculate_partial_diff(
    payload: Mapping[str, object],
    expression_text: str,
    expression: Expr,
    variable: str,
) -> dict[str, object]:
    order = _integer(payload, "order", 1)
    if not 1 <= order <= 5:
        raise MathError("invalid_arguments")

    requested = _optional_text_list(payload, "variables", MAX_VARIABLES)
    if requested is None:
        variables = sorted(str(symbol) for symbol in expression.free_symbols)
    else:
        variables = [validate_variable(name) for name in requested]
    if not variables or len(set(variables)) != len(variables):
        raise MathError("invalid_arguments")

    partials: list[MathPartial] = []
    exact_parts: list[str] = []
    numeric_parts: list[str] = []
    for name in variables:
        symbol = build_variable(name)
        derivative = sympy.simplify(sympy.diff(expression, symbol, order))
        if _has_undefined(derivative):
            raise MathError("evaluation_failed", _undefined_detail(derivative))
        exact_text = _checked_text(sympy.sstr(derivative))
        numeric_text = _checked_text(_numeric_text(derivative))
        partials.append(
            MathPartial(
                variable=name,
                order=order,
                exact=exact_text,
                numeric=numeric_text,
            )
        )
        exact_parts.append(exact_text)
        numeric_parts.append(numeric_text)

    result_text = exact_parts[0] if len(exact_parts) == 1 else "; ".join(exact_parts)
    numeric_text = (
        numeric_parts[0]
        if len(numeric_parts) == 1
        else ("; ".join(numeric_parts) if any(numeric_parts) else "")
    )
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.PARTIAL_DIFF,
        expression=expression_text,
        variable=variable,
        result=_checked_text(result_text),
        numeric=_checked_text(numeric_text),
        exact=True,
        detail=f"Частные производные порядка {order} по переменным: {', '.join(variables)}.",
        variables=variables,
        partials=partials,
    )


def _calculate_jacobian(payload: Mapping[str, object]) -> dict[str, object]:
    function_texts = _optional_text_list(payload, "functions", MAX_FUNCTIONS)
    if not function_texts:
        raise MathError("invalid_arguments")
    variable_names = _optional_text_list(payload, "variables", MAX_VARIABLES)
    if not variable_names:
        raise MathError("invalid_arguments")

    variables = [validate_variable(name) for name in variable_names]
    if len(set(variables)) != len(variables):
        raise MathError("invalid_arguments")
    symbols = [build_variable(name) for name in variables]
    expressions = [build_expression(text) for text in function_texts]
    allowed_symbols = set(symbols)
    for expression in expressions:
        if expression.free_symbols - allowed_symbols:
            raise MathError(
                "invalid_arguments",
                "Функции Якобиана должны зависеть только от указанных переменных.",
            )

    matrix = sympy.Matrix(expressions).jacobian(symbols)
    exact_rows: list[list[str]] = []
    numeric_rows: list[list[str]] = []
    for row in range(matrix.rows):
        exact_row: list[str] = []
        numeric_row: list[str] = []
        for column in range(matrix.cols):
            entry = sympy.simplify(matrix[row, column])
            if _has_undefined(entry):
                raise MathError("evaluation_failed", _undefined_detail(entry))
            exact_row.append(_checked_text(sympy.sstr(entry)))
            numeric_row.append(_checked_text(_numeric_text(entry)))
        exact_rows.append(exact_row)
        numeric_rows.append(numeric_row)

    result_text = _checked_text(sympy.sstr(matrix))
    numeric_text = (
        _checked_text(sympy.sstr(sympy.Matrix(numeric_rows)))
        if all(entry for row in numeric_rows for entry in row)
        else ""
    )
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.JACOBIAN,
        expression="; ".join(text.strip() for text in function_texts),
        variable=variables[0],
        result=result_text,
        numeric=numeric_text,
        exact=True,
        detail="Матрица Якоби; строки соответствуют функциям, столбцы — переменным.",
        variables=variables,
        jacobian=exact_rows,
        jacobian_numeric=numeric_rows,
    )


def _calculate_constrained_extrema(payload: Mapping[str, object]) -> dict[str, object]:
    expression_text = _text(payload, "expression")
    variable_names = _optional_text_list(payload, "variables", MAX_VARIABLES)
    if not variable_names or len(variable_names) < 2:
        raise MathError(
            "invalid_arguments",
            "Для условных экстремумов укажите не менее двух переменных.",
        )
    constraints = _optional_text_list(payload, "constraints", MAX_EQUATIONS)
    if not constraints or len(constraints) != 1:
        raise MathError(
            "invalid_arguments",
            "Для условных экстремумов нужно одно уравнение-ограничение.",
        )

    variables = [validate_variable(name) for name in variable_names]
    if len(set(variables)) != len(variables):
        raise MathError("invalid_arguments")
    symbols = [build_variable(name) for name in variables]
    allowed_symbols = set(symbols)
    objective = build_expression(expression_text)
    constraint = build_equation(constraints[0])
    if objective.free_symbols - allowed_symbols:
        raise MathError(
            "invalid_arguments",
            "Целевая функция должна зависеть только от указанных переменных.",
        )
    if constraint.free_symbols - allowed_symbols:
        raise MathError(
            "invalid_arguments",
            "Ограничение должно содержать только указанные переменные.",
        )

    multiplier = sympy.Dummy("lambda", real=True)
    constraint_expression = constraint.difference
    equations = [
        sympy.diff(objective, symbol) - multiplier * sympy.diff(constraint_expression, symbol)
        for symbol in symbols
    ]
    equations.append(constraint_expression)

    try:
        raw_solutions = sympy.solve(equations, [*symbols, multiplier], dict=True)
    except (NotImplementedError, TypeError, ValueError):
        raw_solutions = []
    if not isinstance(raw_solutions, list):
        raw_solutions = []

    candidates: list[tuple[dict[Symbol, Expr], Expr, Expr]] = []
    original_constraint = sympy.Add(
        constraint.left.original,
        sympy.Mul(sympy.Integer(-1), constraint.right.original, evaluate=False),
        evaluate=False,
    )
    constraint_checks = constraint.left.constraints + constraint.right.constraints
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            continue
        if multiplier not in raw_solution or any(symbol not in raw_solution for symbol in symbols):
            continue
        point = {symbol: sympy.simplify(raw_solution[symbol]) for symbol in symbols}
        multiplier_value = sympy.simplify(raw_solution[multiplier])
        if any(not _is_real_value(value) for value in point.values()):
            continue
        if not _is_real_value(multiplier_value):
            continue
        if not _constraints_hold(constraint_checks, point, True):
            continue
        if not _is_zero_expression(_substitute(original_constraint, point)):
            continue

        substitutions = {**point, multiplier: multiplier_value}
        gradient_verified = True
        for symbol in symbols:
            residual = sympy.diff(objective, symbol) - multiplier * sympy.diff(
                constraint_expression,
                symbol,
            )
            if not _is_zero_expression(_substitute(residual, substitutions)):
                gradient_verified = False
                break
        if not gradient_verified:
            continue

        objective_value = sympy.simplify(objective.subs(list(point.items())))
        if not _is_real_value(objective_value):
            continue
        candidates.append((point, objective_value, multiplier_value))

    candidates = _deduplicate_extrema(candidates)
    candidates.sort(key=lambda candidate: sympy.sstr(candidate[1]))

    if not candidates:
        return _calculate_result_payload(
            status=CalculateStatus.NO_SOLUTION,
            operation=CalculateOperation.CONSTRAINED_EXTREMA,
            expression=expression_text,
            variable=variables[0],
            result="",
            numeric="",
            exact=True,
            detail=("Стационарные точки Лагранжа не найдены или не прошли проверку ограничения."),
            variables=variables,
            constraint=constraint.normalized,
        )

    values = [candidate[1] for candidate in candidates]
    minimum = sympy.Min(*values)
    maximum = sympy.Max(*values)
    extrema: list[MathExtremum] = []
    result_parts: list[str] = []
    numeric_parts: list[str] = []
    for point, value, multiplier_value in candidates:
        if len(candidates) == 1 or _is_same_exact(minimum, maximum):
            kind = ExtremaKind.STATIONARY
        elif _is_same_exact(value, minimum):
            kind = ExtremaKind.MINIMUM
        elif _is_same_exact(value, maximum):
            kind = ExtremaKind.MAXIMUM
        else:
            kind = ExtremaKind.STATIONARY

        point_text = {str(symbol): _checked_text(sympy.sstr(point[symbol])) for symbol in symbols}
        numeric_point = {
            str(symbol): _checked_text(_numeric_text(point[symbol])) for symbol in symbols
        }
        value_text = _checked_text(sympy.sstr(value))
        numeric_value_text = _checked_text(_numeric_text(value))
        multiplier_text = _checked_text(sympy.sstr(multiplier_value))
        numeric_multiplier = _checked_text(_numeric_text(multiplier_value))
        extrema.append(
            MathExtremum(
                point=point_text,
                numeric_point=numeric_point,
                value=value_text,
                numeric_value=numeric_value_text,
                multiplier=multiplier_text,
                numeric_multiplier=numeric_multiplier,
                kind=kind,
            )
        )
        point_display = ", ".join(f"{name}={point_text[name]}" for name in variables)
        result_parts.append(f"({point_display}) = {value_text}")
        numeric_parts.append(f"({point_display}) = {numeric_value_text}")

    result_text = _checked_text("; ".join(result_parts))
    numeric_text = _checked_text("; ".join(numeric_parts))
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.CONSTRAINED_EXTREMA,
        expression=expression_text,
        variable=variables[0],
        result=result_text,
        numeric=numeric_text,
        exact=True,
        detail=(
            "Найдены стационарные точки Лагранжа; исходное ограничение, уравнения "
            "Лагранжа и область определения проверены подстановкой. "
            "Классификация minimum/maximum относится только к найденным кандидатам "
            "и строга для компактного ограничения при полном наборе кандидатов."
        ),
        variables=variables,
        constraint=constraint.normalized,
        extrema=extrema,
    )


def _deduplicate_extrema(
    candidates: Sequence[tuple[dict[Symbol, Expr], Expr, Expr]],
) -> list[tuple[dict[Symbol, Expr], Expr, Expr]]:
    unique: list[tuple[dict[Symbol, Expr], Expr, Expr]] = []
    for candidate in candidates:
        point = candidate[0]
        if any(_same_point(point, existing[0]) for existing in unique):
            continue
        unique.append(candidate)
    return unique


def _same_point(
    left: Mapping[Symbol, Expr],
    right: Mapping[Symbol, Expr],
) -> bool:
    if set(left) != set(right):
        return False
    return all(_is_same_exact(left[symbol], right[symbol]) for symbol in left)


def _is_same_exact(left: Expr, right: Expr) -> bool:
    return _is_zero_expression(sympy.simplify(left - right))


def _numeric_order(value: Expr) -> float:
    try:
        numeric = float(sympy.N(value, 15))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return numeric


def _calculate_series(
    payload: Mapping[str, object],
    variable: str,
    symbol: Symbol,
) -> dict[str, object]:
    expression_text = _text(payload, "expression")
    expression = build_expression(expression_text)
    lower_text = _optional_text(payload, "lower") or "1"
    upper_text = _optional_text(payload, "upper") or "oo"
    lower = _series_bound(build_expression(lower_text))
    upper = _series_bound(build_expression(upper_text))
    if symbol in lower.free_symbols or symbol in upper.free_symbols:
        raise MathError("invalid_arguments")

    series_symbol = sympy.Symbol(variable, integer=True)
    series_expression = expression.subs(symbol, series_symbol)
    series = sympy.Sum(series_expression, (series_symbol, lower, upper))
    infinite = upper in (sympy.oo, -sympy.oo)
    convergence = _series_convergence(series) if infinite else None

    absolute_convergence: ConvergenceStatus | None = None
    conditional: bool | None = None
    if infinite:
        absolute_series = sympy.Sum(
            sympy.Abs(series_expression),
            (series_symbol, lower, upper),
        )
        absolute_convergence = _series_convergence(absolute_series)
        if convergence is ConvergenceStatus.CONVERGENT:
            if absolute_convergence is ConvergenceStatus.CONVERGENT:
                conditional = False
            elif absolute_convergence is ConvergenceStatus.DIVERGENT:
                conditional = True

    try:
        value = series.doit()
    except (NotImplementedError, TypeError, ValueError):
        value = series

    if value in (sympy.oo, -sympy.oo):
        if convergence is None:
            convergence = ConvergenceStatus.DIVERGENT
        return _calculate_result_payload(
            status=CalculateStatus.OK,
            operation=CalculateOperation.SERIES,
            expression=expression_text,
            variable=variable,
            result="",
            numeric="",
            exact=True,
            detail="Ряд расходится: сумма неограниченно растёт.",
            variables=[variable],
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    if _has_undefined(value):
        numeric_text = _series_numeric_text(series)
        if convergence is ConvergenceStatus.DIVERGENT:
            return _calculate_result_payload(
                status=CalculateStatus.OK,
                operation=CalculateOperation.SERIES,
                expression=expression_text,
                variable=variable,
                result="",
                numeric="",
                exact=True,
                detail="Ряд расходится.",
                variables=[variable],
                convergence=convergence,
                absolute_convergence=absolute_convergence,
                conditional=conditional,
            )
        return _calculate_result_payload(
            status=CalculateStatus.UNDEFINED,
            operation=CalculateOperation.SERIES,
            expression=expression_text,
            variable=variable,
            result="",
            numeric=numeric_text,
            exact=False,
            detail="Точная сумма не найдена; численное значение приведено отдельно.",
            variables=[variable],
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    value = sympy.simplify(value)
    if _has_undefined(value):
        return _calculate_result_payload(
            status=CalculateStatus.UNDEFINED,
            operation=CalculateOperation.SERIES,
            expression=expression_text,
            variable=variable,
            result="",
            numeric="",
            exact=False,
            detail=_undefined_detail(value),
            variables=[variable],
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    result_text = _checked_text(sympy.sstr(value))
    numeric_text = _checked_text(_numeric_text(value))
    convergence_detail = _convergence_detail(convergence, absolute_convergence, conditional)
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.SERIES,
        expression=expression_text,
        variable=variable,
        result=result_text,
        numeric=numeric_text,
        exact=True,
        detail=f"Сумма ряда по {variable}: {convergence_detail}",
        variables=[variable],
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
    )


def _series_bound(value: Expr) -> Expr:
    if value in (sympy.oo, -sympy.oo):
        return value
    simplified = sympy.simplify(value)
    if simplified.is_integer is not True:
        raise MathError(
            "invalid_arguments",
            "Границы ряда должны быть целыми числами или бесконечностью.",
        )
    return simplified


def _series_convergence(series: sympy.Sum) -> ConvergenceStatus:
    try:
        result = series.is_convergent()
    except (NotImplementedError, TypeError, ValueError):
        return ConvergenceStatus.UNKNOWN
    if result is True or result is sympy.true:
        return ConvergenceStatus.CONVERGENT
    if result is False or result is sympy.false:
        return ConvergenceStatus.DIVERGENT
    return ConvergenceStatus.UNKNOWN


def _series_numeric_text(series: sympy.Sum) -> str:
    try:
        numeric = series.evalf(15)
    except (TypeError, ValueError):
        return ""
    if _has_undefined(numeric):
        return ""
    return _checked_text(sympy.sstr(numeric))


def _convergence_detail(
    convergence: ConvergenceStatus | None,
    absolute_convergence: ConvergenceStatus | None,
    conditional: bool | None,
) -> str:
    if convergence is ConvergenceStatus.DIVERGENT:
        return "ряд расходится."
    if convergence is ConvergenceStatus.UNKNOWN:
        return "сходимость не определена."
    if convergence is ConvergenceStatus.CONVERGENT:
        if conditional is True:
            return "ряд сходится условно; ряд модулей расходится."
        if absolute_convergence is ConvergenceStatus.CONVERGENT:
            return "ряд сходится абсолютно."
        return "ряд сходится."
    return "конечная сумма вычислена."


def _integrate(
    expression: Expr,
    symbol: sympy.Symbol,
    variable: str,
    payload: Mapping[str, object],
) -> tuple[Expr, str, ConvergenceStatus | None]:
    lower_text = _optional_text(payload, "lower")
    upper_text = _optional_text(payload, "upper")
    if bool(lower_text) != bool(upper_text):
        raise MathError("invalid_arguments")

    if not lower_text or not upper_text:
        return sympy.integrate(expression, symbol), f"Неопределённый интеграл по {variable}.", None

    lower = build_expression(lower_text)
    upper = build_expression(upper_text)
    if symbol in lower.free_symbols or symbol in upper.free_symbols:
        raise MathError("invalid_arguments")

    integral = sympy.Integral(expression, (symbol, lower, upper))
    value = integral.doit()
    if _needs_exact_recovery(value):
        recovered = _recover_integral(expression, symbol, lower, upper)
        if recovered is not None:
            value = recovered
            detail = (
                f"Определённый интеграл по {variable} от {lower_text} до {upper_text}; "
                "точное значение получено дифференцированием семейства интегралов "
                "и проверено символьно."
            )
            return value, detail, ConvergenceStatus.CONVERGENT

    if value in (sympy.oo, -sympy.oo):
        return (
            value,
            f"Интеграл по {variable} от {lower_text} до {upper_text} расходится.",
            ConvergenceStatus.DIVERGENT,
        )
    if _has_undefined(value):
        return (
            value,
            f"Интеграл по {variable} от {lower_text} до {upper_text} не определён.",
            ConvergenceStatus.UNKNOWN,
        )

    return (
        value,
        f"Определённый интеграл по {variable} от {lower_text} до {upper_text}; сходится.",
        ConvergenceStatus.CONVERGENT,
    )


def _needs_exact_recovery(value: Expr) -> bool:
    return bool(value.has(sympy.Integral, sympy.meijerg))


def _recover_integral(
    expression: Expr,
    symbol: sympy.Symbol,
    lower: Expr,
    upper: Expr,
) -> Expr | None:
    if lower != 0 or upper != sympy.oo:
        return None

    family_symbol = sympy.Dummy("a", positive=True)
    family = (1 + symbol**2) ** (-family_symbol)
    matched_integrand = -sympy.diff(family, family_symbol).subs(family_symbol, 1)
    if not _is_zero_expression(sympy.simplify(expression - matched_integrand)):
        return None

    family_integral = (
        sympy.sqrt(sympy.pi)
        * sympy.gamma(family_symbol - sympy.Rational(1, 2))
        / (2 * sympy.gamma(family_symbol))
    )
    value = -sympy.diff(family_integral, family_symbol).subs(family_symbol, 1)
    return sympy.simplify(value)


def _limit(
    expression: Expr,
    symbol: sympy.Symbol,
    variable: str,
    payload: Mapping[str, object],
) -> tuple[Expr | None, str]:
    point_text = _optional_text(payload, "point")
    if not point_text:
        raise MathError("invalid_arguments")
    direction_text = _text(payload, "direction", "both")
    try:
        direction = LimitDirection(direction_text)
    except ValueError:
        raise MathError("invalid_arguments") from None
    point = build_expression(point_text)
    try:
        value = sympy.limit(expression, symbol, point, dir=LIMIT_DIRECTIONS[direction])
    except ValueError:
        return None, "Предел не существует: односторонние пределы различаются."
    except NotImplementedError:
        return None, "Не удалось вычислить предел в указанной точке."
    detail = f"Предел по {variable} в точке {point_text}, направление {direction.value}."
    return value, detail


def solve(payload: Mapping[str, object]) -> dict[str, object]:
    equation_texts = _text_list(payload, "equations", MAX_EQUATIONS)
    if not equation_texts:
        raise MathError("invalid_arguments")
    real_only = _boolean(payload, "real_only", True)
    equations = [build_equation(text) for text in equation_texts]

    inferred = sorted({str(symbol) for equation in equations for symbol in equation.free_symbols})
    requested = _optional_text_list(payload, "variables", MAX_VARIABLES)
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
    plot_type_text = _text(payload, "plot_type", "curve")
    try:
        plot_type = PlotType(plot_type_text)
    except ValueError:
        raise MathError("invalid_arguments") from None

    function_texts = _text_list(payload, "functions", MAX_FUNCTIONS)
    if not function_texts:
        raise MathError("invalid_arguments")
    variable = validate_variable(_text(payload, "variable", "x"))
    symbol = build_variable(variable)

    x_min, x_max = _plot_x_range(payload, plot_type)
    y_min, y_max = _plot_y_range(payload, x_min, x_max, plot_type)
    levels = _plot_levels(payload) if plot_type is PlotType.CONTOUR else ()
    if plot_type is not PlotType.CONTOUR and payload.get("levels") is not None:
        raise MathError("invalid_arguments", "levels применимы только к contour.")

    if plot_type is PlotType.CURVE:
        parsed_functions = _parse_curve_functions(function_texts, variable, symbol)
        rendering = render_curve(
            parsed_functions,
            variable,
            symbol,
            x_min,
            x_max,
            PLOT_SAMPLES,
        )
        detail = (
            "График построен; разрывы и участки вне вещественной области разорваны."
            if rendering.has_points
            else "Ни одна функция не имеет вещественных значений на выбранном диапазоне."
        )
        return _plot_payload(
            plot_type=plot_type,
            rendering=rendering,
            variable=variable,
            x_min=x_min,
            x_max=x_max,
            y_min=None,
            y_max=None,
            detail=detail,
        )

    if plot_type is PlotType.PARAMETRIC:
        if len(function_texts) != 2:
            raise MathError(
                "invalid_arguments",
                "Для параметрической кривой нужны две функции x(t) и y(t).",
            )
        parameter = validate_variable(_text(payload, "parameter", "t"))
        x_parsed = _parse_parametric_function(function_texts[0], parameter)
        y_parsed = _parse_parametric_function(function_texts[1], parameter)
        rendering = render_parametric(
            x_parsed,
            y_parsed,
            parameter,
            build_variable(parameter),
            x_min,
            x_max,
            PLOT_SAMPLES,
        )
        detail = (
            "Параметрическая кривая построена; разрывы области определения разорваны."
            if rendering.has_points
            else "Параметрическая кривая не имеет допустимых точек на выбранном диапазоне."
        )
        return _plot_payload(
            plot_type=plot_type,
            rendering=rendering,
            variable=parameter,
            x_min=x_min,
            x_max=x_max,
            y_min=None,
            y_max=None,
            detail=detail,
        )

    if len(function_texts) != 1:
        raise MathError("invalid_arguments", "Для этого типа графика нужна одна функция.")
    if plot_type in (PlotType.CONTOUR, PlotType.SURFACE):
        y_variable = validate_variable(_text(payload, "y_variable", "y"))
        if y_variable == variable:
            raise MathError("invalid_arguments")
        parsed = build_parsed_expression(function_texts[0])
        allowed_symbols = {symbol, build_variable(y_variable)}
        if parsed.free_symbols - allowed_symbols:
            raise MathError(
                "invalid_expression",
                f"Функция должна зависеть только от {variable} и {y_variable}.",
            )
        if plot_type is PlotType.CONTOUR:
            rendering = render_contour(
                parsed,
                symbol,
                build_variable(y_variable),
                x_min,
                x_max,
                y_min,
                y_max,
                levels or None,
            )
            detail = (
                "Линии уровня построены; недопустимые значения и полюсы замаскированы."
                if rendering.has_points
                else "На выбранной области нет допустимых значений для линий уровня."
            )
        else:
            rendering = render_surface(
                parsed,
                symbol,
                build_variable(y_variable),
                x_min,
                x_max,
                y_min,
                y_max,
            )
            detail = (
                "Поверхность построена; недопустимые значения и полюсы замаскированы."
                if rendering.has_points
                else "На выбранной области нет допустимых значений для поверхности."
            )
        return _plot_payload(
            plot_type=plot_type,
            rendering=rendering,
            variable=variable,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            detail=detail,
        )

    parsed = build_parsed_expression(function_texts[0])
    if parsed.free_symbols - {symbol}:
        raise MathError(
            "invalid_expression",
            f"Комплексная функция должна зависеть только от {variable}.",
        )
    if plot_type is PlotType.DOMAIN_COLORING:
        rendering = render_domain_coloring(
            parsed,
            symbol,
            x_min,
            x_max,
            y_min,
            y_max,
        )
        detail = (
            "Цветовая карта аргумента построена; полюсы и разрезы замаскированы, "
            "яркость кодирует нормированный и ограниченный |f(z)|."
            if rendering.has_points
            else "На выбранной области нет конечных значений комплексной функции."
        )
    else:
        rendering = render_complex_height(
            parsed,
            symbol,
            x_min,
            x_max,
            y_min,
            y_max,
        )
        detail = (
            "Поверхность |f(z)| построена; полюсы и разрезы замаскированы."
            if rendering.has_points
            else "На выбранной области нет конечных значений |f(z)|."
        )
    return _plot_payload(
        plot_type=plot_type,
        rendering=rendering,
        variable=variable,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        detail=detail,
    )


def _parse_curve_functions(
    function_texts: Sequence[str],
    variable: str,
    symbol: Symbol,
) -> list[tuple[str, ParsedExpression]]:
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
    return parsed_functions


def _parse_parametric_function(text: str, parameter: str) -> ParsedExpression:
    parsed = build_parsed_expression(text)
    extra_symbols = parsed.expression.free_symbols - {build_variable(parameter)}
    if extra_symbols:
        raise MathError(
            "invalid_expression",
            f"Функция должна зависеть только от {parameter}.",
        )
    return parsed


def _plot_x_range(
    payload: Mapping[str, object],
    plot_type: PlotType,
) -> tuple[float, float]:
    x_min = _optional_number(payload, "x_min")
    x_max = _optional_number(payload, "x_max")
    if (x_min is None) != (x_max is None):
        raise MathError("invalid_arguments")
    if x_min is None or x_max is None:
        if plot_type is PlotType.PARAMETRIC:
            x_min, x_max = PARAMETRIC_DEFAULT_MIN, PARAMETRIC_DEFAULT_MAX
        else:
            x_min, x_max = DEFAULT_X_MIN, DEFAULT_X_MAX
    if not math.isfinite(x_min) or not math.isfinite(x_max) or x_min >= x_max:
        raise MathError("invalid_arguments")
    return x_min, x_max


def _plot_y_range(
    payload: Mapping[str, object],
    x_min: float,
    x_max: float,
    plot_type: PlotType,
) -> tuple[float, float]:
    if plot_type in (PlotType.CURVE, PlotType.PARAMETRIC):
        return x_min, x_max
    y_min = _optional_number(payload, "y_min")
    y_max = _optional_number(payload, "y_max")
    if (y_min is None) != (y_max is None):
        raise MathError("invalid_arguments")
    if y_min is None or y_max is None:
        return x_min, x_max
    if not math.isfinite(y_min) or not math.isfinite(y_max) or y_min >= y_max:
        raise MathError("invalid_arguments")
    return y_min, y_max


def _plot_levels(payload: Mapping[str, object]) -> tuple[float, ...]:
    levels = _optional_number_list(payload, "levels", MAX_PLOT_LEVELS)
    if levels is None:
        return ()
    if len(levels) < 1:
        raise MathError("invalid_arguments", "Для contour нужен хотя бы один уровень.")
    unique = sorted(set(levels))
    if not unique:
        raise MathError("invalid_arguments", "Уровни contour должны различаться.")
    return tuple(unique)


def _plot_payload(
    *,
    plot_type: PlotType,
    rendering: PlotRendering,
    variable: str,
    x_min: float,
    x_max: float,
    y_min: float | None,
    y_max: float | None,
    detail: str,
) -> dict[str, object]:
    status = PlotStatus.OK if rendering.has_points else PlotStatus.UNDEFINED
    zero_markers = [
        MathPlotZero(re=round(real, 12), im=round(imaginary, 12))
        for real, imaginary in rendering.zero_markers
    ]
    detail_text = detail
    if zero_markers:
        marker_text = ", ".join(f"({marker.re:g}, {marker.im:g})" for marker in zero_markers)
        detail_text = f"{detail} Нули отмечены на поверхности: {marker_text}."
    result = MathPlotResult(
        status=status,
        plot_type=plot_type,
        functions=rendering.series,
        variable=variable,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        samples=rendering.samples,
        png_bytes=len(rendering.png),
        masked_points=rendering.masked_points,
        total_points=rendering.total_points,
        branch_cuts=list(rendering.branch_cuts),
        poles=list(rendering.poles),
        checkpoints=list(rendering.checkpoints),
        zero_markers=zero_markers,
        levels=list(rendering.levels),
        color_scale=rendering.color_scale,
        detail=detail_text,
    )
    return {
        "result": result.model_dump(mode="json"),
        "png_base64": _encode_png(rendering.png),
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
    *,
    convergence: ConvergenceStatus | None = None,
) -> dict[str, object]:
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=operation,
        expression=expression,
        variable=variable,
        result=result,
        numeric=numeric,
        exact=exact,
        detail=detail,
        convergence=convergence,
    )


def _undefined_calculate(
    operation: CalculateOperation,
    expression: str,
    variable: str,
    exact: bool,
    detail: str,
) -> dict[str, object]:
    return _calculate_result_payload(
        status=CalculateStatus.UNDEFINED,
        operation=operation,
        expression=expression,
        variable=variable,
        result="",
        numeric="",
        exact=exact,
        detail=detail,
    )


def _calculate_result_payload(
    *,
    status: CalculateStatus,
    operation: CalculateOperation,
    expression: str,
    variable: str,
    result: str,
    numeric: str,
    exact: bool,
    detail: str,
    variables: Sequence[str] = (),
    constraint: str = "",
    convergence: ConvergenceStatus | None = None,
    absolute_convergence: ConvergenceStatus | None = None,
    conditional: bool | None = None,
    partials: Sequence[MathPartial] = (),
    jacobian: Sequence[Sequence[str]] = (),
    jacobian_numeric: Sequence[Sequence[str]] = (),
    extrema: Sequence[MathExtremum] = (),
) -> dict[str, object]:
    return MathCalculateResult(
        status=status,
        operation=operation,
        expression=expression.strip(),
        variable=variable,
        result=result,
        numeric=numeric,
        exact=exact,
        detail=detail,
        variables=list(variables),
        constraint=constraint,
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
        partials=list(partials),
        jacobian=[list(row) for row in jacobian],
        jacobian_numeric=[list(row) for row in jacobian_numeric],
        extrema=list(extrema),
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
    if value.has(
        sympy.Integral,
        sympy.Derivative,
        sympy.Limit,
        sympy.Sum,
        sympy.Product,
        sympy.meijerg,
    ):
        return True
    return bool(value.has(sympy.AccumBounds))


def _undefined_detail(value: Expr) -> str:
    if value in (sympy.nan, sympy.zoo) or value.has(sympy.nan, sympy.zoo):
        return "Результат не определён: выражение содержит nan или zoo."
    if value.has(sympy.Integral, sympy.Derivative, sympy.Limit, sympy.meijerg):
        return "Результат остался в невычисленном виде."
    if value.has(sympy.Sum, sympy.Product):
        return "Ряд или произведение остались в невычисленном виде."
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


def _optional_number(payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
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


def _optional_text_list(
    payload: Mapping[str, object],
    key: str,
    maximum: int = MAX_VARIABLES,
) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise MathError("invalid_arguments")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MathError("invalid_arguments")
        result.append(item)
    return result


def _optional_number_list(
    payload: Mapping[str, object],
    key: str,
    maximum: int,
) -> list[float] | None:
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
    return result


__all__ = [
    "calculate",
    "handle_request",
    "plot",
    "solve",
]
