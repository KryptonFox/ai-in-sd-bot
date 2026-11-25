import math
from collections.abc import Mapping, Sequence

import sympy
from sympy import Expr, Symbol

from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    DomainRelation,
    ParsedExpression,
)
from mcp_servers.math_tools.plotting.common import (
    MAX_LISTED_POLES,
    exact_float,
    finite_number,
    format_position,
)
from mcp_servers.math_tools.schemas import CheckpointState, MathPlotCheckpoint
from mcp_servers.math_tools.symbolic import numeric_text

MAX_ZERO_MARKERS = 8


def symbolic_poles(
    expression: Expr,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[str, ...]:
    try:
        _, denominator = sympy.fraction(sympy.together(expression))
    except (TypeError, ValueError):
        return ()
    if denominator == 1 or not denominator.has(symbol):
        return ()

    roots = solve_roots(denominator, symbol)

    poles: list[str] = []
    for root in roots:
        if not isinstance(root, Expr) or root.free_symbols:
            continue
        numeric = finite_number(root)
        if numeric is None:
            continue
        if not (x_min <= numeric.real <= x_max and y_min <= numeric.imag <= y_max):
            continue
        formatted = f"{symbol.name}={sympy.sstr(sympy.simplify(root))}"
        if formatted not in poles:
            poles.append(formatted)
        if len(poles) >= MAX_LISTED_POLES:
            break
    return tuple(poles)


def solve_roots(expression: Expr, symbol: Symbol) -> list[Expr]:
    complex_symbol = sympy.Symbol(f"{symbol.name}__complex")
    try:
        roots = sympy.solve(expression.subs(symbol, complex_symbol), complex_symbol)
    except (NotImplementedError, TypeError, ValueError):
        return []
    return [root for root in roots if isinstance(root, Expr)]


def constraint_poles(constraints: Sequence[DomainConstraint]) -> tuple[str, ...]:
    poles = [
        f"{sympy.sstr(constraint.expression)} != 0"
        for constraint in constraints
        if constraint.relation is DomainRelation.NONZERO
    ]
    return tuple(dict.fromkeys(poles))


def complex_zero_markers(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[tuple[float, float, float], ...]:
    roots = solve_roots(parsed.expression, symbol)

    markers: list[tuple[float, float, float]] = []
    for root in roots:
        numeric = finite_number(root)
        if numeric is None:
            continue
        if not (x_min <= numeric.real <= x_max and y_min <= numeric.imag <= y_max):
            continue
        substitution = {symbol: root}
        try:
            original_value = parsed.original.subs(list(substitution.items()))
        except (TypeError, ValueError):
            continue
        if original_value is sympy.zoo or original_value.has(sympy.zoo, sympy.nan):
            continue
        if denominator_violated(parsed.constraints, substitution):
            continue
        if on_branch_cut(parsed.expression, symbol, root):
            continue
        markers.append((numeric.real, numeric.imag, 0.0))
        if len(markers) >= MAX_ZERO_MARKERS:
            break
    return tuple(markers)


def curve_checkpoints(
    functions: Sequence[tuple[str, ParsedExpression]],
    symbol: Symbol,
    x_min: float,
    x_max: float,
) -> tuple[MathPlotCheckpoint, ...]:
    point = 0.0 if x_min <= 0 <= x_max else (x_min + x_max) / 2
    checkpoints: list[MathPlotCheckpoint] = []
    for _, parsed in functions:
        checkpoint = real_checkpoint(
            parsed,
            {symbol: exact_float(point)},
            f"{symbol.name}={format_position(point)}",
        )
        checkpoints.append(checkpoint)
    return tuple(checkpoints)


def parametric_checkpoint(
    x_parsed: ParsedExpression,
    y_parsed: ParsedExpression,
    symbol: Symbol,
    t_min: float,
    t_max: float,
) -> MathPlotCheckpoint:
    point = 0.0 if t_min <= 0 <= t_max else (t_min + t_max) / 2
    substitution = {symbol: exact_float(point)}
    x_value = real_value(x_parsed.expression, substitution)
    y_value = real_value(y_parsed.expression, substitution)
    label = f"{symbol.name}={format_position(point)}"
    if x_value is None or y_value is None:
        return undefined_checkpoint(label)
    return MathPlotCheckpoint(
        point=label,
        value=f"({sympy.sstr(x_value)}, {sympy.sstr(y_value)})",
        numeric=f"({numeric_text(x_value)}, {numeric_text(y_value)})",
    )


def surface_checkpoints(
    parsed: ParsedExpression,
    symbols: Sequence[Symbol],
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[MathPlotCheckpoint, ...]:
    candidates = [
        (0.0, 0.0),
        (1.0, 1.0),
        (-1.0, 1.0),
        (1.0, -1.0),
        (-1.0, -1.0),
    ]
    checkpoints: list[MathPlotCheckpoint] = []
    for x_value, y_value in candidates:
        if not (x_min <= x_value <= x_max and y_min <= y_value <= y_max):
            continue
        substitutions = {
            symbols[0]: exact_float(x_value),
            symbols[1]: exact_float(y_value),
        }
        checkpoint = real_checkpoint(
            parsed,
            substitutions,
            (
                f"({symbols[0].name}, {symbols[1].name})="
                f"({format_position(x_value)}, {format_position(y_value)})"
            ),
        )
        checkpoints.append(checkpoint)
        if len(checkpoints) >= 4:
            break
    return tuple(checkpoints)


def complex_checkpoints(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    *,
    include_height: bool,
) -> tuple[MathPlotCheckpoint, ...]:
    candidates = [
        sympy.Integer(0),
        sympy.Integer(1),
        sympy.I,
        sympy.Integer(-1),
        -sympy.I,
        sympy.Integer(2),
        sympy.Integer(-2),
    ]
    checkpoints: list[MathPlotCheckpoint] = []
    for point in candidates:
        numeric_point = finite_number(point)
        if numeric_point is None:
            continue
        if not (x_min <= numeric_point.real <= x_max and y_min <= numeric_point.imag <= y_max):
            continue
        checkpoint = complex_checkpoint(parsed, symbol, point, include_height=include_height)
        checkpoints.append(checkpoint)
    return tuple(checkpoints)


def complex_checkpoint(
    parsed: ParsedExpression,
    symbol: Symbol,
    point: Expr,
    *,
    include_height: bool,
) -> MathPlotCheckpoint:
    label = f"{symbol.name}={sympy.sstr(point)}"
    substitution = {symbol: point}
    try:
        original_value = parsed.original.subs(list(substitution.items()))
    except (TypeError, ValueError):
        original_value = sympy.nan
    if original_value is sympy.zoo or original_value.has(sympy.zoo):
        if is_log_branch_point(parsed.expression, symbol, point):
            return MathPlotCheckpoint(
                point=label,
                value="branch_point",
                numeric="",
                state=CheckpointState.BRANCH_POINT,
            )
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state=CheckpointState.POLE)
    if original_value is sympy.nan or original_value.has(sympy.nan):
        return MathPlotCheckpoint(
            point=label,
            value="pole",
            numeric="",
            state=CheckpointState.REMOVABLE_POLE,
        )

    try:
        value = sympy.simplify(parsed.expression.subs(list(substitution.items())))
    except (TypeError, ValueError):
        return undefined_checkpoint(label)
    if value is sympy.zoo or value.has(sympy.zoo):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state=CheckpointState.POLE)
    if value is sympy.nan or value.has(sympy.nan, sympy.oo, -sympy.oo):
        return undefined_checkpoint(label)

    numeric = numeric_text(value)
    if not numeric:
        return undefined_checkpoint(label)

    state = (
        CheckpointState.BRANCH_CUT
        if on_branch_cut(parsed.expression, symbol, point)
        else CheckpointState.NONE
    )
    if denominator_violated(parsed.constraints, substitution):
        state = CheckpointState.REMOVABLE_POLE

    height = ""
    numeric_height = ""
    if include_height:
        height_value = sympy.simplify(sympy.Abs(value))
        height = sympy.sstr(height_value)
        numeric_height = numeric_text(height_value)
    return MathPlotCheckpoint(
        point=label,
        value=sympy.sstr(value),
        numeric=numeric,
        height=height,
        numeric_height=numeric_height,
        state=state,
    )


def real_checkpoint(
    parsed: ParsedExpression,
    substitutions: Mapping[Symbol, Expr],
    label: str,
) -> MathPlotCheckpoint:
    try:
        original_value = parsed.original.subs(list(substitutions.items()))
    except (TypeError, ValueError):
        original_value = sympy.nan
    if original_value is sympy.zoo or original_value.has(sympy.zoo):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state=CheckpointState.POLE)
    if original_value is sympy.nan or original_value.has(sympy.nan):
        return MathPlotCheckpoint(
            point=label,
            value="pole",
            numeric="",
            state=CheckpointState.REMOVABLE_POLE,
        )

    value = real_value(parsed.expression, substitutions)
    if value is None:
        return undefined_checkpoint(label)
    if denominator_violated(parsed.constraints, substitutions):
        return MathPlotCheckpoint(
            point=label,
            value="pole",
            numeric="",
            state=CheckpointState.REMOVABLE_POLE,
        )
    if constraint_violated(parsed.constraints, substitutions):
        return undefined_checkpoint(label)
    return MathPlotCheckpoint(
        point=label,
        value=sympy.sstr(value),
        numeric=numeric_text(value),
    )


def undefined_checkpoint(label: str) -> MathPlotCheckpoint:
    return MathPlotCheckpoint(
        point=label,
        value="undefined",
        numeric="",
        state=CheckpointState.UNDEFINED,
    )


def real_value(
    expression: Expr,
    substitutions: Mapping[Symbol, Expr],
) -> Expr | None:
    try:
        value = sympy.simplify(expression.subs(list(substitutions.items())))
    except (TypeError, ValueError):
        return None
    if value is sympy.zoo or value.has(sympy.zoo, sympy.nan, sympy.oo, -sympy.oo):
        return None
    if value.free_symbols:
        return None
    numeric = finite_number(value)
    if numeric is None or abs(numeric.imag) > 1e-12 * max(1.0, abs(numeric.real)):
        return None
    return value


def on_branch_cut(expression: Expr, symbol: Symbol, point: Expr) -> bool:
    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        try:
            value = complex(sympy.N(argument.subs(symbol, point), 15))
        except (TypeError, ValueError, OverflowError):
            continue
        if value.real < 0 and abs(value.imag) <= 1e-12 * max(1.0, abs(value.real)):
            return True
    return False


def is_log_branch_point(expression: Expr, symbol: Symbol, point: Expr) -> bool:
    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        try:
            value = sympy.simplify(argument.subs(symbol, point))
        except (TypeError, ValueError):
            continue
        if value.is_zero is True:
            return True
    return False


def denominator_violated(
    constraints: Sequence[DomainConstraint],
    substitution: Mapping[Symbol, Expr],
) -> bool:
    for constraint in constraints:
        if constraint.relation is not DomainRelation.NONZERO:
            continue
        try:
            raw = complex(sympy.N(constraint.expression.subs(list(substitution.items())), 15))
        except (TypeError, ValueError, OverflowError):
            return True
        if not (math.isfinite(raw.real) and math.isfinite(raw.imag)):
            return True
        if abs(raw) == 0:
            return True
    return False


def constraint_violated(
    constraints: Sequence[DomainConstraint],
    substitution: Mapping[Symbol, Expr],
) -> bool:
    for constraint in constraints:
        try:
            raw = complex(sympy.N(constraint.expression.subs(list(substitution.items())), 15))
        except (TypeError, ValueError, OverflowError):
            return True
        if not (math.isfinite(raw.real) and math.isfinite(raw.imag)):
            return True
        if abs(raw.imag) > 1e-12 * max(1.0, abs(raw.real)):
            return True
        value = raw.real
        relation = constraint.relation
        if relation is DomainRelation.NONZERO:
            if value == 0:
                return True
        elif relation is DomainRelation.POSITIVE:
            if value <= 0:
                return True
        elif relation is DomainRelation.NONNEGATIVE:
            if value < 0:
                return True
        elif relation is DomainRelation.NEGATIVE:
            if value >= 0:
                return True
        elif relation is DomainRelation.NONPOSITIVE:
            if value > 0:
                return True
    return False
