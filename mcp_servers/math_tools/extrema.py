from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import sympy
from sympy import Expr, Symbol

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    ParsedEquation,
    ParsedExpression,
    build_equation_syntax,
    build_parsed_syntax,
    build_variable,
)
from mcp_servers.math_tools.limits import MAX_EQUATIONS, MAX_VARIABLES
from mcp_servers.math_tools.payloads import CalculateRequest
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    CalculateStatus,
    ExtremaKind,
    MathCalculateResult,
    MathExtremum,
)
from mcp_servers.math_tools.symbolic import (
    checked_text,
    constraints_hold,
    is_real_value,
    is_same_exact,
    is_zero_expression,
    numeric_order,
    numeric_text,
    substitute,
)


@dataclass(frozen=True)
class ExtremaCandidate:
    point: dict[Symbol, Expr]
    value: Expr
    multiplier: Expr


def constrained_extrema(request: CalculateRequest) -> dict[str, object]:
    expression = request.required_expression()
    variable_names = request.variables
    if not variable_names or len(variable_names) < 2:
        raise MathError(
            "invalid_arguments",
            "Для условных экстремумов укажите не менее двух переменных.",
        )
    constraints = request.constraints
    if not constraints or len(constraints) != 1:
        raise MathError(
            "invalid_arguments",
            "Для условных экстремумов нужно одно уравнение-ограничение.",
        )
    if len(variable_names) > MAX_VARIABLES or len(constraints) > MAX_EQUATIONS:
        raise MathError("invalid_arguments")

    variables = list(variable_names)
    symbols = [build_variable(name) for name in variables]
    allowed_symbols = set(symbols)
    objective = build_parsed_syntax(expression)
    constraint = build_equation_syntax(constraints[0])
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

    candidates = _lagrange_candidates(objective, constraint, symbols)
    if not candidates:
        return _extrema_result_payload(
            status=CalculateStatus.NO_SOLUTION,
            expression=expression.text,
            variables=variables,
            constraint=constraint.normalized,
            result="",
            numeric="",
            detail="Стационарные точки Лагранжа не найдены или не прошли проверку ограничения.",
        )

    values = [candidate.value for candidate in candidates]
    minimum = sympy.Min(*values)
    maximum = sympy.Max(*values)
    extrema, result_text, numeric_result = _format_extrema(
        candidates,
        variables,
        symbols,
        minimum,
        maximum,
    )
    return _extrema_result_payload(
        status=CalculateStatus.OK,
        expression=expression.text,
        variables=variables,
        constraint=constraint.normalized,
        result=result_text,
        numeric=numeric_result,
        detail=(
            "Найдены стационарные точки Лагранжа; исходное ограничение, уравнения "
            "Лагранжа и область определения целевой функции проверены подстановкой. "
            "Классификация minimum/maximum относится только к найденным кандидатам "
            "и строга для компактного ограничения при полном наборе кандидатов."
        ),
        extrema=extrema,
    )


def _lagrange_candidates(
    objective: ParsedExpression,
    constraint: ParsedEquation,
    symbols: Sequence[Symbol],
) -> list[ExtremaCandidate]:
    multiplier = sympy.Dummy("lambda", real=True)
    constraint_expression = constraint.difference
    equations = [
        sympy.diff(objective.expression, symbol)
        - multiplier * sympy.diff(constraint_expression, symbol)
        for symbol in symbols
    ]
    equations.append(constraint_expression)

    try:
        raw_solutions = sympy.solve(equations, [*symbols, multiplier], dict=True)
    except (NotImplementedError, TypeError, ValueError):
        raw_solutions = []
    if not isinstance(raw_solutions, list):
        raw_solutions = []

    original_constraint = sympy.Add(
        constraint.left.original,
        sympy.Mul(sympy.Integer(-1), constraint.right.original, evaluate=False),
        evaluate=False,
    )
    constraint_checks = constraint.left.constraints + constraint.right.constraints

    candidates: list[ExtremaCandidate] = []
    for raw_solution in raw_solutions:
        candidate = _verified_candidate(
            raw_solution,
            objective,
            constraint_expression,
            constraint_checks,
            original_constraint,
            symbols,
            multiplier,
        )
        if candidate is not None:
            candidates.append(candidate)

    candidates = _deduplicate(candidates)
    candidates.sort(
        key=lambda candidate: (
            numeric_order(candidate.value),
            sympy.sstr(candidate.value),
        )
    )
    return candidates


def _verified_candidate(
    raw_solution: object,
    objective: ParsedExpression,
    constraint_expression: Expr,
    constraint_checks: Sequence[DomainConstraint],
    original_constraint: Expr,
    symbols: Sequence[Symbol],
    multiplier: Symbol,
) -> ExtremaCandidate | None:
    if not isinstance(raw_solution, dict):
        return None
    if multiplier not in raw_solution or any(symbol not in raw_solution for symbol in symbols):
        return None

    point = {symbol: sympy.simplify(raw_solution[symbol]) for symbol in symbols}
    multiplier_value = sympy.simplify(raw_solution[multiplier])
    if any(not is_real_value(value) for value in point.values()):
        return None
    if not is_real_value(multiplier_value):
        return None
    if not constraints_hold(constraint_checks, point, True):
        return None
    if not constraints_hold(objective.constraints, point, True):
        return None
    if not is_zero_expression(substitute(original_constraint, point)):
        return None

    substitutions = {**point, multiplier: multiplier_value}
    if not _gradient_verified(objective, constraint_expression, substitutions, symbols, multiplier):
        return None

    objective_value = sympy.simplify(objective.expression.subs(list(point.items())))
    if not is_real_value(objective_value):
        return None
    return ExtremaCandidate(
        point=point,
        value=objective_value,
        multiplier=multiplier_value,
    )


def _gradient_verified(
    objective: ParsedExpression,
    constraint_expression: Expr,
    substitutions: Mapping[Symbol, Expr],
    symbols: Sequence[Symbol],
    multiplier: Symbol,
) -> bool:
    for symbol in symbols:
        residual = sympy.diff(objective.expression, symbol) - multiplier * sympy.diff(
            constraint_expression,
            symbol,
        )
        if not is_zero_expression(substitute(residual, substitutions)):
            return False
    return True


def _deduplicate(candidates: Sequence[ExtremaCandidate]) -> list[ExtremaCandidate]:
    unique: list[ExtremaCandidate] = []
    for candidate in candidates:
        if any(_same_point(candidate.point, existing.point) for existing in unique):
            continue
        unique.append(candidate)
    return unique


def _same_point(
    left: Mapping[Symbol, Expr],
    right: Mapping[Symbol, Expr],
) -> bool:
    if set(left) != set(right):
        return False
    return all(is_same_exact(left[symbol], right[symbol]) for symbol in left)


def _format_extrema(
    candidates: Sequence[ExtremaCandidate],
    variables: Sequence[str],
    symbols: Sequence[Symbol],
    minimum: Expr,
    maximum: Expr,
) -> tuple[list[MathExtremum], str, str]:
    extrema: list[MathExtremum] = []
    result_parts: list[str] = []
    numeric_parts: list[str] = []
    for candidate in candidates:
        kind = _classify(candidate.value, minimum, maximum, len(candidates))
        point_text = {
            str(symbol): checked_text(sympy.sstr(candidate.point[symbol])) for symbol in symbols
        }
        numeric_point = {
            str(symbol): checked_text(numeric_text(candidate.point[symbol])) for symbol in symbols
        }
        value_text = checked_text(sympy.sstr(candidate.value))
        numeric_value = checked_text(numeric_text(candidate.value))
        extrema.append(
            MathExtremum(
                point=point_text,
                numeric_point=numeric_point,
                value=value_text,
                numeric_value=numeric_value,
                multiplier=checked_text(sympy.sstr(candidate.multiplier)),
                numeric_multiplier=checked_text(numeric_text(candidate.multiplier)),
                kind=kind,
            )
        )
        point_display = ", ".join(f"{name}={point_text[name]}" for name in variables)
        numeric_display = ", ".join(f"{name}={numeric_point[name]}" for name in variables)
        result_parts.append(f"({point_display}) = {value_text}")
        numeric_parts.append(f"({numeric_display}) = {numeric_value}")

    return (
        extrema,
        checked_text("; ".join(result_parts)),
        checked_text("; ".join(numeric_parts)),
    )


def _classify(
    value: Expr,
    minimum: Expr,
    maximum: Expr,
    candidate_count: int,
) -> ExtremaKind:
    if candidate_count == 1 or is_same_exact(minimum, maximum):
        return ExtremaKind.STATIONARY
    if is_same_exact(value, minimum):
        return ExtremaKind.MINIMUM
    if is_same_exact(value, maximum):
        return ExtremaKind.MAXIMUM
    return ExtremaKind.STATIONARY


def _extrema_result_payload(
    *,
    status: CalculateStatus,
    expression: str,
    variables: Sequence[str],
    constraint: str,
    result: str,
    numeric: str,
    detail: str,
    extrema: Sequence[MathExtremum] = (),
) -> dict[str, object]:
    return MathCalculateResult(
        status=status,
        operation=CalculateOperation.CONSTRAINED_EXTREMA,
        expression=expression.strip(),
        variable=variables[0],
        result=result,
        numeric=numeric,
        exact=True,
        detail=detail,
        variables=list(variables),
        constraint=constraint,
        extrema=list(extrema),
    ).model_dump(mode="json")
