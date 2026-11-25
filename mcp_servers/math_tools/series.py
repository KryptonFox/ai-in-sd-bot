import sympy
from sympy import Expr, Symbol

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import build_expression, build_expression_syntax
from mcp_servers.math_tools.payloads import CalculateRequest
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    ConvergenceStatus,
    MathCalculateResult,
)
from mcp_servers.math_tools.symbolic import (
    checked_text,
    has_undefined,
    numeric_text,
    undefined_detail,
)
from mcp_servers.math_tools.syntax import ExpressionSyntax


def series(
    request: CalculateRequest,
    variable: str,
    symbol: Symbol,
) -> dict[str, object]:
    expression = request.required_expression()
    expression_value = build_expression_syntax(expression)
    lower_text = request.lower.text if request.lower is not None else "1"
    upper_text = request.upper.text if request.upper is not None else "oo"
    lower = _series_bound(request.lower, "1")
    upper = _series_bound(request.upper, "oo")
    if symbol in lower.free_symbols or symbol in upper.free_symbols:
        raise MathError("invalid_arguments")

    series_symbol = sympy.Symbol(variable, integer=True)
    series_expression = expression_value.subs(symbol, series_symbol)
    series_value = sympy.Sum(series_expression, (series_symbol, lower, upper))
    infinite = lower in (sympy.oo, -sympy.oo) or upper in (sympy.oo, -sympy.oo)
    convergence, absolute_convergence, conditional = _convergence_info(
        series_expression,
        series_symbol,
        lower,
        upper,
        infinite,
    )

    try:
        value = series_value.doit()
    except (NotImplementedError, TypeError, ValueError):
        value = series_value

    return _series_result(
        expression=expression.text,
        variable=variable,
        series=series_value,
        value=value,
        lower_text=lower_text,
        upper_text=upper_text,
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
    )


def _series_bound(value: ExpressionSyntax | None, default: str) -> Expr:
    expression = build_expression_syntax(value) if value is not None else build_expression(default)
    if expression in (sympy.oo, -sympy.oo):
        return expression
    simplified = sympy.simplify(expression)
    if simplified.is_integer is not True:
        raise MathError(
            "invalid_arguments",
            "Границы ряда должны быть целыми числами или бесконечностью.",
        )
    return simplified


def _convergence_info(
    expression: Expr,
    symbol: Symbol,
    lower: Expr,
    upper: Expr,
    infinite: bool,
) -> tuple[ConvergenceStatus | None, ConvergenceStatus | None, bool | None]:
    if not infinite:
        return None, None, None

    series_value = sympy.Sum(expression, (symbol, lower, upper))
    convergence = _series_convergence(series_value)
    absolute_series = sympy.Sum(sympy.Abs(expression), (symbol, lower, upper))
    absolute_convergence = _series_convergence(absolute_series)
    conditional: bool | None = None
    if convergence is ConvergenceStatus.CONVERGENT:
        if absolute_convergence is ConvergenceStatus.CONVERGENT:
            conditional = False
        elif absolute_convergence is ConvergenceStatus.DIVERGENT:
            conditional = True
    return convergence, absolute_convergence, conditional


def _series_result(
    *,
    expression: str,
    variable: str,
    series: sympy.Sum,
    value: Expr,
    lower_text: str,
    upper_text: str,
    convergence: ConvergenceStatus | None,
    absolute_convergence: ConvergenceStatus | None,
    conditional: bool | None,
) -> dict[str, object]:
    if value in (sympy.oo, -sympy.oo):
        if convergence is None:
            convergence = ConvergenceStatus.DIVERGENT
        return _series_payload(
            expression=expression,
            variable=variable,
            status=CalculateStatus.OK,
            result="",
            numeric="",
            exact=True,
            detail=f"Ряд расходится: сумма от {lower_text} до {upper_text} неограниченно растёт.",
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    if has_undefined(value):
        return _undefined_series_result(
            expression=expression,
            variable=variable,
            series=series,
            lower_text=lower_text,
            upper_text=upper_text,
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    value = sympy.simplify(value)
    if has_undefined(value):
        return _series_payload(
            expression=expression,
            variable=variable,
            status=CalculateStatus.UNDEFINED,
            result="",
            numeric="",
            exact=False,
            detail=undefined_detail(value),
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )

    convergence_detail = _convergence_detail(
        convergence,
        absolute_convergence,
        conditional,
    )
    return _series_payload(
        expression=expression,
        variable=variable,
        status=CalculateStatus.OK,
        result=checked_text(sympy.sstr(value)),
        numeric=checked_text(numeric_text(value)),
        exact=True,
        detail=f"Сумма ряда по {variable}: {convergence_detail}",
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
    )


def _undefined_series_result(
    *,
    expression: str,
    variable: str,
    series: sympy.Sum,
    lower_text: str,
    upper_text: str,
    convergence: ConvergenceStatus | None,
    absolute_convergence: ConvergenceStatus | None,
    conditional: bool | None,
) -> dict[str, object]:
    if convergence is ConvergenceStatus.DIVERGENT:
        return _series_payload(
            expression=expression,
            variable=variable,
            status=CalculateStatus.OK,
            result="",
            numeric="",
            exact=True,
            detail=f"Ряд от {lower_text} до {upper_text} расходится.",
            convergence=convergence,
            absolute_convergence=absolute_convergence,
            conditional=conditional,
        )
    return _series_payload(
        expression=expression,
        variable=variable,
        status=CalculateStatus.UNDEFINED,
        result="",
        numeric=_series_numeric_text(series),
        exact=False,
        detail="Точная сумма не найдена; численное значение приведено отдельно.",
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
    )


def _series_payload(
    *,
    expression: str,
    variable: str,
    status: CalculateStatus,
    result: str,
    numeric: str,
    exact: bool,
    detail: str,
    convergence: ConvergenceStatus | None,
    absolute_convergence: ConvergenceStatus | None,
    conditional: bool | None,
) -> dict[str, object]:
    return MathCalculateResult(
        status=status,
        operation=CalculateOperation.SERIES,
        expression=expression.strip(),
        variable=variable,
        result=result,
        numeric=numeric,
        exact=exact,
        detail=detail,
        variables=[variable],
        convergence=convergence,
        absolute_convergence=absolute_convergence,
        conditional=conditional,
    ).model_dump(mode="json")


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
    if has_undefined(numeric):
        return ""
    return checked_text(sympy.sstr(numeric))


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
