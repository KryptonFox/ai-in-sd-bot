from collections.abc import Sequence

import sympy
from sympy import Expr, Symbol

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    build_expression_syntax,
    build_variable,
)
from mcp_servers.math_tools.extrema import constrained_extrema
from mcp_servers.math_tools.limits import MAX_FUNCTIONS, MAX_VARIABLES
from mcp_servers.math_tools.payloads import CalculateRequest
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    ConvergenceStatus,
    LimitDirection,
    MathCalculateResult,
    MathExtremum,
    MathPartial,
)
from mcp_servers.math_tools.series import series
from mcp_servers.math_tools.symbolic import (
    checked_text,
    has_undefined,
    is_real_value,
    numeric_text,
    undefined_detail,
)
from mcp_servers.math_tools.syntax import ExpressionSyntax

LIMIT_DIRECTIONS: dict[LimitDirection, str] = {
    LimitDirection.BOTH: "+-",
    LimitDirection.LEFT: "-",
    LimitDirection.RIGHT: "+",
}


def calculate(request: CalculateRequest) -> dict[str, object]:
    if request.operation is CalculateOperation.JACOBIAN:
        return _calculate_jacobian(request)
    if request.operation is CalculateOperation.CONSTRAINED_EXTREMA:
        return constrained_extrema(request)

    expression = request.required_expression()
    variable = request.variable
    symbol = build_variable(variable)

    if request.operation is CalculateOperation.SERIES:
        return series(request, variable, symbol)

    expression_value = build_expression_syntax(expression)
    exact = request.operation is not CalculateOperation.NUMERIC
    value: Expr | None = None
    detail = ""
    convergence: ConvergenceStatus | None = None

    if request.operation is CalculateOperation.SIMPLIFY:
        value = sympy.simplify(expression_value)
        detail = "Выражение упрощено."
    elif request.operation is CalculateOperation.EVALUATE:
        value = sympy.simplify(expression_value.doit())
        detail = "Точное значение вычислено."
    elif request.operation is CalculateOperation.NUMERIC:
        return _calculate_numeric(expression, expression_value, variable)
    elif request.operation is CalculateOperation.DIFF:
        value = sympy.diff(expression_value, symbol, request.order)
        detail = f"Производная порядка {request.order} по переменной {variable}."
    elif request.operation is CalculateOperation.PARTIAL_DIFF:
        return _calculate_partial_diff(request, expression, expression_value, variable)
    elif request.operation is CalculateOperation.INTEGRATE:
        value, detail, convergence = _integrate(
            expression_value,
            symbol,
            variable,
            request,
        )
    elif request.operation is CalculateOperation.LIMIT:
        value, detail = _limit(expression_value, symbol, variable, request)
    else:
        raise MathError("invalid_arguments")

    if value is None:
        return _undefined_calculate(
            request.operation,
            expression.text,
            variable,
            exact,
            detail,
        )

    if has_undefined(value):
        return _undefined_calculate(
            request.operation,
            expression.text,
            variable,
            exact,
            undefined_detail(value),
        )

    value = sympy.simplify(value)
    if has_undefined(value):
        return _undefined_calculate(
            request.operation,
            expression.text,
            variable,
            exact,
            undefined_detail(value),
        )

    return _calculate_payload(
        request.operation,
        expression.text,
        variable,
        exact,
        checked_text(sympy.sstr(value)),
        checked_text(numeric_text(value)),
        detail,
        convergence=convergence,
    )


def _calculate_numeric(
    expression: ExpressionSyntax,
    value: Expr,
    variable: str,
) -> dict[str, object]:
    numeric_value = sympy.N(value, 15)
    if numeric_value.free_symbols:
        return _undefined_calculate(
            CalculateOperation.NUMERIC,
            expression.text,
            variable,
            False,
            "Численное значение требует подстановки переменных.",
        )
    if has_undefined(numeric_value):
        return _undefined_calculate(
            CalculateOperation.NUMERIC,
            expression.text,
            variable,
            False,
            "Численный результат не определён.",
        )
    numeric_result = checked_text(numeric_text(numeric_value))
    if not numeric_result:
        return _undefined_calculate(
            CalculateOperation.NUMERIC,
            expression.text,
            variable,
            False,
            "Численный результат не является конечным вещественным числом.",
        )
    return _calculate_payload(
        CalculateOperation.NUMERIC,
        expression.text,
        variable,
        False,
        numeric_result,
        numeric_result,
        "Численное значение с 15 значащими цифрами.",
    )


def _calculate_partial_diff(
    request: CalculateRequest,
    expression: ExpressionSyntax,
    expression_value: Expr,
    variable: str,
) -> dict[str, object]:
    if request.variables is None:
        variables = sorted(str(symbol) for symbol in expression_value.free_symbols)
    else:
        variables = list(request.variables)
    if not variables or len(set(variables)) != len(variables):
        raise MathError("invalid_arguments")

    partials: list[MathPartial] = []
    exact_parts: list[str] = []
    numeric_parts: list[str] = []
    for name in variables:
        symbol = build_variable(name)
        derivative = sympy.simplify(sympy.diff(expression_value, symbol, request.order))
        if has_undefined(derivative):
            raise MathError("evaluation_failed", undefined_detail(derivative))
        exact_text = checked_text(sympy.sstr(derivative))
        partial_numeric = checked_text(numeric_text(derivative))
        partials.append(
            MathPartial(
                variable=name,
                order=request.order,
                exact=exact_text,
                numeric=partial_numeric,
            )
        )
        exact_parts.append(exact_text)
        numeric_parts.append(partial_numeric)

    result_text = exact_parts[0] if len(exact_parts) == 1 else "; ".join(exact_parts)
    numeric_result = (
        numeric_parts[0]
        if len(numeric_parts) == 1
        else ("; ".join(numeric_parts) if any(numeric_parts) else "")
    )
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.PARTIAL_DIFF,
        expression=expression.text,
        variable=variable,
        result=checked_text(result_text),
        numeric=checked_text(numeric_result),
        exact=True,
        detail=(
            f"Частные производные порядка {request.order} по переменным: {', '.join(variables)}."
        ),
        variables=variables,
        partials=partials,
    )


def _calculate_jacobian(request: CalculateRequest) -> dict[str, object]:
    if not request.functions or not request.variables:
        raise MathError("invalid_arguments")
    if len(request.functions) > MAX_FUNCTIONS or len(request.variables) > MAX_VARIABLES:
        raise MathError("invalid_arguments")

    variables = list(request.variables)
    symbols = [build_variable(name) for name in variables]
    expressions = [build_expression_syntax(function) for function in request.functions]
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
            if has_undefined(entry):
                raise MathError("evaluation_failed", undefined_detail(entry))
            exact_row.append(checked_text(sympy.sstr(entry)))
            numeric_row.append(checked_text(numeric_text(entry)))
        exact_rows.append(exact_row)
        numeric_rows.append(numeric_row)

    result_text = checked_text(sympy.sstr(matrix))
    numeric_result = (
        checked_text(sympy.sstr(sympy.Matrix(numeric_rows)))
        if all(entry for row in numeric_rows for entry in row)
        else ""
    )
    return _calculate_result_payload(
        status=CalculateStatus.OK,
        operation=CalculateOperation.JACOBIAN,
        expression="; ".join(function.text.strip() for function in request.functions),
        variable=variables[0],
        result=result_text,
        numeric=numeric_result,
        exact=True,
        detail="Матрица Якоби; строки соответствуют функциям, столбцы — переменным.",
        variables=variables,
        jacobian=exact_rows,
        jacobian_numeric=numeric_rows,
    )


def _integrate(
    expression: Expr,
    symbol: Symbol,
    variable: str,
    request: CalculateRequest,
) -> tuple[Expr, str, ConvergenceStatus | None]:
    lower_syntax = request.lower
    upper_syntax = request.upper
    if (lower_syntax is None) != (upper_syntax is None):
        raise MathError("invalid_arguments")

    if lower_syntax is None or upper_syntax is None:
        return sympy.integrate(expression, symbol), f"Неопределённый интеграл по {variable}.", None

    lower = build_expression_syntax(lower_syntax)
    upper = build_expression_syntax(upper_syntax)
    lower_text = lower_syntax.text
    upper_text = upper_syntax.text
    if symbol in lower.free_symbols or symbol in upper.free_symbols:
        raise MathError("invalid_arguments")

    integral = sympy.Integral(expression, (symbol, lower, upper))
    value = integral.doit()
    if _needs_exact_recovery(value):
        recovered = _recover_integral(expression, symbol, lower, upper)
        if recovered is not None:
            detail = (
                f"Определённый интеграл по {variable} от {lower_text} до {upper_text}; "
                "точное значение получено дифференцированием по параметру семейства "
                f"(1+{symbol.name}**2)**(-a) и проверено символьно."
            )
            return recovered, detail, ConvergenceStatus.CONVERGENT

    if value in (sympy.oo, -sympy.oo):
        return (
            value,
            f"Интеграл по {variable} от {lower_text} до {upper_text} расходится.",
            ConvergenceStatus.DIVERGENT,
        )
    if has_undefined(value):
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
    symbol: Symbol,
    lower: Expr,
    upper: Expr,
) -> Expr | None:
    if lower != 0 or upper != sympy.oo:
        return None

    family_symbol = sympy.Dummy("a", positive=True)
    family = (1 + symbol**2) ** (-family_symbol)
    matched_integrand = -sympy.diff(family, family_symbol).subs(family_symbol, 1)
    coefficient = sympy.simplify(expression / matched_integrand)
    if coefficient.free_symbols or not is_real_value(coefficient):
        return None
    if coefficient.is_zero is True:
        return None

    family_integral = (
        sympy.sqrt(sympy.pi)
        * sympy.gamma(family_symbol - sympy.Rational(1, 2))
        / (2 * sympy.gamma(family_symbol))
    )
    value = -sympy.diff(family_integral, family_symbol).subs(family_symbol, 1)
    return sympy.simplify(coefficient * value)


def _limit(
    expression: Expr,
    symbol: Symbol,
    variable: str,
    request: CalculateRequest,
) -> tuple[Expr | None, str]:
    point_syntax = request.point
    if point_syntax is None:
        raise MathError("invalid_arguments")
    point = build_expression_syntax(point_syntax)
    try:
        value = sympy.limit(
            expression,
            symbol,
            point,
            dir=LIMIT_DIRECTIONS[request.direction],
        )
    except ValueError:
        return None, "Предел не существует: односторонние пределы различаются."
    except NotImplementedError:
        return None, "Не удалось вычислить предел в указанной точке."
    detail = (
        f"Предел по {variable} в точке {point_syntax.text}, направление {request.direction.value}."
    )
    return value, detail


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
