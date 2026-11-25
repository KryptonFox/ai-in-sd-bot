import math
from collections.abc import Mapping, Sequence
from typing import cast

import sympy
from sympy import Basic, Expr, Symbol

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import DomainConstraint, DomainRelation
from mcp_servers.math_tools.limits import MAX_RESULT_LENGTH

FALLBACK_DIGITS = 50


def has_undefined(value: Expr) -> bool:
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


def undefined_detail(value: Expr) -> str:
    if value in (sympy.nan, sympy.zoo) or value.has(sympy.nan, sympy.zoo):
        return "Результат не определён: выражение содержит nan или zoo."
    if value.has(sympy.Integral, sympy.Derivative, sympy.Limit, sympy.meijerg):
        return "Результат остался в невычисленном виде."
    if value.has(sympy.Sum, sympy.Product):
        return "Ряд или произведение остались в невычисленном виде."
    if value.has(sympy.AccumBounds):
        return "Предел не существует: значения колеблются."
    return "Результат не определён."


def numeric_text(value: Expr) -> str:
    if value.free_symbols or not value.is_number:
        return ""
    if has_undefined(value):
        return ""
    if value in (sympy.oo, -sympy.oo):
        return ""
    try:
        numeric = sympy.N(value, 15)
    except (TypeError, ValueError):
        return ""
    if has_undefined(numeric):
        return ""
    return sympy.sstr(numeric)


def numeric_order(value: Expr) -> float:
    try:
        numeric = float(sympy.N(value, 15))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return numeric


def checked_text(text: str) -> str:
    if len(text) > MAX_RESULT_LENGTH:
        raise MathError("resource_limit", "Результат слишком велик для выдачи.")
    return text


def constraints_hold(
    constraints: Sequence[DomainConstraint],
    substitutions: Mapping[Symbol, Expr],
    real_only: bool,
) -> bool:
    for constraint in constraints:
        if constraint.real_only and not real_only:
            continue
        value = substitute(constraint.expression, substitutions)
        if not relation_holds(value, constraint.relation):
            return False
    return True


def relation_holds(value: Expr, relation: DomainRelation) -> bool:
    if relation is DomainRelation.NONZERO:
        return is_nonzero(value)

    sign = relation_sign(value)
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


def relation_sign(value: Expr) -> int | None:
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

    numeric = bounded_numeric(simplified)
    if numeric is None:
        return None
    if numeric.is_zero is True:
        return 0
    if numeric.is_positive is True:
        return 1
    if numeric.is_negative is True:
        return -1
    return None


def is_nonzero(value: Expr) -> bool:
    if value.is_zero is True:
        return False
    if value.is_zero is False:
        return True

    simplified = sympy.simplify(value)
    if simplified.is_zero is True:
        return False
    if simplified.is_zero is False:
        return True

    numeric = bounded_numeric(simplified)
    return numeric is not None and numeric.is_zero is not True


def is_zero_expression(value: Expr) -> bool:
    if value.is_zero is True:
        return True
    if value.is_zero is False:
        return False
    if has_undefined(value):
        return False

    simplified = sympy.simplify(value)
    if simplified.is_zero is True:
        return True
    if simplified.is_zero is False:
        return False

    numeric = bounded_numeric(simplified)
    return numeric is not None and numeric.is_zero is True


def is_real_value(value: Expr) -> bool:
    if value.is_real is True:
        return True
    if value.is_real is False:
        return False

    numeric = bounded_numeric(value)
    return numeric is not None and numeric.is_real is True


def is_same_exact(left: Expr, right: Expr) -> bool:
    return is_zero_expression(sympy.simplify(left - right))


def bounded_numeric(value: Expr) -> Expr | None:
    if value.free_symbols:
        return None
    try:
        numeric = sympy.N(value, FALLBACK_DIGITS)
    except (TypeError, ValueError):
        return None
    if has_undefined(numeric):
        return None
    if numeric in (sympy.oo, -sympy.oo):
        return None
    return numeric


def substitute(
    expression: Basic,
    substitutions: Mapping[Symbol, Expr],
) -> Expr:
    return cast(Expr, expression.subs(list(substitutions.items())))
