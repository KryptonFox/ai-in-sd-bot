import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

import numpy as np
import sympy
from sympy import Expr, Symbol, lambdify

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    DomainRelation,
    ParsedExpression,
)
from mcp_servers.math_tools.limits import POLE_MAGNITUDE
from mcp_servers.math_tools.plotting.common import BRANCH_CUT_WIDTH_CELLS, ComplexField
from mcp_servers.math_tools.plotting.features import symbolic_poles

ARRAY_FUNCTION = Callable[..., object]


@dataclass(frozen=True)
class RealGrid:
    grid_x: np.ndarray
    grid_y: np.ndarray
    values: np.ndarray
    domain_mask: np.ndarray


def real_gamma(values: object) -> np.ndarray:
    array = np.asarray(values)
    if np.iscomplexobj(array):
        return np.full(array.shape, np.nan)
    real_values = np.asarray(array, dtype=float)
    result = np.full(real_values.shape, np.nan)
    for index in np.ndindex(real_values.shape):
        try:
            result[index] = math.gamma(float(real_values[index]))
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def real_factorial(values: object) -> np.ndarray:
    array = np.asarray(values)
    if np.iscomplexobj(array):
        return np.full(array.shape, np.nan)
    return real_gamma(np.asarray(array, dtype=float) + 1.0)


SPECIAL_FUNCTIONS: dict[str, Callable[[object], np.ndarray]] = {
    "gamma": real_gamma,
    "factorial": real_factorial,
}


def validate_complex_expression(expression: Expr, symbol: Symbol) -> None:
    if expression.atoms(sympy.gamma, sympy.factorial):
        raise MathError(
            "unsupported_syntax",
            "gamma и factorial поддерживаются только для вещественных графиков.",
        )
    complex_variable = {symbol: sympy.Dummy(complex=True)}
    if any(
        call.args[0].xreplace(complex_variable).is_real is not True
        for call in expression.atoms(sympy.floor, sympy.ceiling)
    ):
        raise MathError(
            "unsupported_syntax",
            "floor и ceil требуют вещественный аргумент, например abs(z).",
        )


@lru_cache(maxsize=256)
def _compile_function(expression: Expr, symbols: tuple[Symbol, ...]) -> ARRAY_FUNCTION | None:
    try:
        function = lambdify(
            list(symbols),
            expression,
            modules=["numpy", SPECIAL_FUNCTIONS],
        )
    except (KeyError, TypeError, ValueError):
        return None
    return cast(ARRAY_FUNCTION, function)


def _invoke(function: ARRAY_FUNCTION, arrays: Sequence[np.ndarray]) -> object | None:
    try:
        with np.errstate(all="ignore"):
            return function(*arrays)
    except (ArithmeticError, NameError, TypeError, ValueError):
        return None


def evaluate_real(
    expression: Expr,
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    function = _compile_function(expression, tuple(symbols))
    if function is None:
        return np.full(arrays[0].shape, np.nan)

    raw_values = _invoke(function, arrays)
    if raw_values is None:
        return np.full(arrays[0].shape, np.nan)

    array = np.asarray(raw_values)
    if array.shape == ():
        array = np.full(arrays[0].shape, array.item())

    try:
        if np.iscomplexobj(array):
            complex_values = np.asarray(array, dtype=complex)
            real = np.real(complex_values)
            imaginary = np.imag(complex_values)
            tolerance = 1e-9 * np.maximum(1.0, np.abs(real))
            values = np.where(
                np.isfinite(real) & np.isfinite(imaginary) & (np.abs(imaginary) <= tolerance),
                real,
                np.nan,
            )
        else:
            values = np.asarray(array, dtype=float)
    except (TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan)

    values = np.asarray(values, dtype=float)
    values[~np.isfinite(values)] = np.nan
    return values


def evaluate_complex(
    expression: Expr,
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    function = _compile_function(expression, tuple(symbols))
    if function is None:
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)

    raw_values = _invoke(function, arrays)
    if raw_values is None:
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)

    try:
        values = np.asarray(raw_values, dtype=complex)
    except (TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)
    if values.shape == ():
        values = np.full(arrays[0].shape, values.item())
    values[~np.isfinite(values.real) | ~np.isfinite(values.imag)] = np.nan + 1j * np.nan
    return values


def constraint_mask(
    constraints: Sequence[DomainConstraint],
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    mask = np.ones(arrays[0].shape, dtype=bool)
    for constraint in constraints:
        values = evaluate_real(constraint.expression, symbols, arrays)
        finite = np.isfinite(values)
        relation = constraint.relation
        if relation is DomainRelation.NONZERO:
            valid = finite & (values != 0)
        elif relation is DomainRelation.POSITIVE:
            valid = finite & (values > 0)
        elif relation is DomainRelation.NONNEGATIVE:
            valid = finite & (values >= 0)
        elif relation is DomainRelation.NEGATIVE:
            valid = finite & (values < 0)
        elif relation is DomainRelation.NONPOSITIVE:
            valid = finite & (values <= 0)
        else:
            valid = np.zeros(arrays[0].shape, dtype=bool)
        mask &= valid
    return mask


def grid_axes(
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    samples: int,
) -> tuple[np.ndarray, np.ndarray]:
    x_values = np.linspace(x_min, x_max, samples)
    y_values = np.linspace(y_min, y_max, samples)
    return np.meshgrid(x_values, y_values)


def sample_real_grid(
    parsed: ParsedExpression,
    x_symbol: Symbol,
    y_symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    samples: int,
) -> RealGrid:
    grid_x, grid_y = grid_axes(x_min, x_max, y_min, y_max, samples)
    values = evaluate_real(parsed.expression, [x_symbol, y_symbol], [grid_x, grid_y])
    domain_mask = constraint_mask(parsed.constraints, [x_symbol, y_symbol], [grid_x, grid_y])
    return RealGrid(
        grid_x=grid_x,
        grid_y=grid_y,
        values=values,
        domain_mask=domain_mask,
    )


def complex_field(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    samples: int,
) -> ComplexField:
    grid_x, grid_y = grid_axes(x_min, x_max, y_min, y_max, samples)
    points = grid_x + 1j * grid_y
    values = evaluate_complex(parsed.expression, [symbol], [points])
    finite = np.isfinite(values.real) & np.isfinite(values.imag)
    pole_mask = ~finite | (np.abs(values) > POLE_MAGNITUDE)

    branch_mask, branch_cuts = branch_cut_mask(
        parsed.expression,
        symbol,
        [points],
        grid_y,
    )
    poles = symbolic_poles(parsed.expression, symbol, x_min, x_max, y_min, y_max)
    return ComplexField(
        values=values,
        mask=pole_mask | branch_mask,
        grid_x=grid_x,
        grid_y=grid_y,
        branch_cuts=branch_cuts,
        poles=poles,
    )


def branch_cut_mask(
    expression: Expr,
    symbol: Symbol,
    arrays: Sequence[np.ndarray],
    grid_y: np.ndarray,
) -> tuple[np.ndarray, tuple[str, ...]]:
    mask = np.zeros(grid_y.shape, dtype=bool)
    cuts: list[str] = []
    step = float(np.mean(np.diff(grid_y[:, 0]))) if grid_y.shape[0] > 1 else 0.01
    tolerance = max(abs(step) * BRANCH_CUT_WIDTH_CELLS, 1e-12)

    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        argument_values = evaluate_complex(argument, [symbol], arrays)
        real = argument_values.real
        imaginary = argument_values.imag
        on_cut = (real < 0) & (np.abs(imaginary) <= tolerance)
        mask |= on_cut
        cuts.append(f"log({sympy.sstr(argument)}): arg={sympy.sstr(sympy.pi)}")

    return mask, tuple(dict.fromkeys(cuts))


def magnitude_mask(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    if not finite.any():
        return finite
    magnitudes = np.abs(values[finite])
    scale = float(np.median(magnitudes))
    threshold = max(POLE_MAGNITUDE, 1e4 * max(scale, 1.0))
    return finite & (np.abs(values) <= threshold)


def split_discontinuities(
    x_values: np.ndarray,
    values: np.ndarray,
    middle_values: np.ndarray,
) -> tuple[np.ndarray, list[float]]:
    finite = np.isfinite(values)
    finite_values = np.abs(values[finite])
    scale = float(np.median(finite_values)) if finite_values.size else 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0

    break_intervals: set[int] = set()
    for index in range(len(values) - 1):
        if not finite[index] or not finite[index + 1]:
            continue
        left = float(values[index])
        right = float(values[index + 1])
        middle = float(middle_values[index])
        span = max(abs(left), abs(right))

        if not math.isfinite(middle):
            break_intervals.add(index)
            continue
        if abs(middle) > 8 * span + 1e-12:
            break_intervals.add(index)
            continue
        if (
            left * right < 0
            and min(abs(left), abs(right)) > 4 * scale
            and abs(left - right) > 20 * scale
        ):
            break_intervals.add(index)
            continue
        if span > 1e6 * scale and abs(left - right) > 1e5 * scale:
            break_intervals.add(index)

    split_values = values.copy()
    for index in break_intervals:
        split_values[index] = np.nan
        split_values[index + 1] = np.nan

    discontinuities = discontinuity_positions(x_values, finite, break_intervals)
    return split_values, discontinuities


def discontinuity_positions(
    x_values: np.ndarray,
    finite: np.ndarray,
    break_intervals: set[int],
) -> list[float]:
    positions: list[float] = []
    index = 0
    length = len(finite)

    while index < length:
        if finite[index]:
            index += 1
            continue
        start = index
        while index + 1 < length and not finite[index + 1]:
            index += 1
        end = index

        if start == end and 0 < start < length - 1:
            positions.append(float(x_values[start]))
        elif start == 0 and end + 1 < length:
            positions.append(float(x_values[end + 1]))
        elif end == length - 1 and start > 0:
            positions.append(float(x_values[start - 1]))
        index += 1

    for interval in break_intervals:
        positions.append(float((x_values[interval] + x_values[interval + 1]) / 2))

    return deduplicate_positions(positions)


def transition_positions(values: np.ndarray, valid: np.ndarray) -> list[float]:
    positions: list[float] = []
    for index in range(len(valid) - 1):
        if bool(valid[index]) != bool(valid[index + 1]):
            positions.append(float((values[index] + values[index + 1]) / 2))
    return deduplicate_positions(positions)


def deduplicate_positions(positions: Sequence[float]) -> list[float]:
    result: list[float] = []
    for position in sorted(positions):
        if not result or abs(position - result[-1]) > 1e-9 * max(1.0, abs(position)):
            result.append(position)
    return result


def count_boolean_segments(valid: np.ndarray) -> int:
    count = 0
    previous_valid = False
    for value in valid:
        current = bool(value)
        if current and not previous_valid:
            count += 1
        previous_valid = current
    return count
