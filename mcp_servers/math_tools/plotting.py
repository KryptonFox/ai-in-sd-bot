import io
import math
from collections.abc import Sequence

import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from sympy import Expr, Symbol, lambdify

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    DomainRelation,
    ParsedExpression,
)
from mcp_servers.math_tools.limits import MAX_PNG_BYTES
from mcp_servers.math_tools.schemas import MathPlotSeries

COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd")
ELLIPSIS_LENGTH = 48


def render_plot(
    functions: Sequence[tuple[str, ParsedExpression]],
    variable: str,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    samples: int,
) -> tuple[bytes, list[MathPlotSeries], bool]:
    x_values = np.linspace(x_min, x_max, samples)
    midpoints = (x_values[:-1] + x_values[1:]) / 2

    figure = Figure(figsize=(6.4, 4.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    rendered_series: list[MathPlotSeries] = []
    plotted_values: list[np.ndarray] = []

    for index, (text, parsed) in enumerate(functions):
        values = _evaluate(parsed.expression, symbol, x_values)
        middle_values = _evaluate(parsed.expression, symbol, midpoints)
        domain_mask = _constraint_mask(parsed.constraints, symbol, x_values)
        middle_domain_mask = _constraint_mask(parsed.constraints, symbol, midpoints)
        values = np.where(domain_mask, values, np.nan)
        middle_values = np.where(middle_domain_mask, middle_values, np.nan)
        split_values, discontinuities = _split_discontinuities(
            x_values,
            values,
            middle_values,
        )
        segments = _count_segments(split_values)
        rendered_series.append(
            MathPlotSeries(
                expression=text,
                segments=segments,
                discontinuities=[_format_position(value) for value in discontinuities],
            )
        )
        plotted_values.append(split_values)
        label = text if len(text) <= ELLIPSIS_LENGTH else text[: ELLIPSIS_LENGTH - 1] + "…"
        axes.plot(
            x_values,
            split_values,
            color=COLORS[index % len(COLORS)],
            linewidth=1.6,
            label=label,
        )

    _configure_axes(axes, variable, x_min, x_max, plotted_values)
    has_points = any(series.segments > 0 for series in rendered_series)
    return _save_png(figure), rendered_series, has_points


def _evaluate(expression: Expr, symbol: Symbol, points: np.ndarray) -> np.ndarray:
    try:
        function = lambdify(symbol, expression, modules=["numpy"])
    except (KeyError, TypeError, ValueError):
        return np.full(points.shape, np.nan)

    try:
        with np.errstate(all="ignore"):
            raw_values = function(points)
    except (ArithmeticError, TypeError, ValueError):
        return np.full(points.shape, np.nan)

    array = np.asarray(raw_values)
    if array.shape == ():
        array = np.full(points.shape, array.item())

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
        return np.full(points.shape, np.nan)

    values = np.asarray(values, dtype=float)
    values[~np.isfinite(values)] = np.nan
    return values


def _constraint_mask(
    constraints: Sequence[DomainConstraint],
    symbol: Symbol,
    points: np.ndarray,
) -> np.ndarray:
    mask = np.ones(points.shape, dtype=bool)
    for constraint in constraints:
        values = _evaluate(constraint.expression, symbol, points)
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
            valid = np.zeros(points.shape, dtype=bool)
        mask &= valid
    return mask


def _split_discontinuities(
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

    discontinuities = _discontinuity_positions(x_values, finite, break_intervals)
    return split_values, discontinuities


def _discontinuity_positions(
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

    return _deduplicate_positions(positions)


def _deduplicate_positions(positions: Sequence[float]) -> list[float]:
    result: list[float] = []
    for position in sorted(positions):
        if not result or abs(position - result[-1]) > 1e-9 * max(1.0, abs(position)):
            result.append(position)
    return result


def _count_segments(values: np.ndarray) -> int:
    count = 0
    previous_finite = False
    for value in values:
        finite = bool(np.isfinite(value))
        if finite and not previous_finite:
            count += 1
        previous_finite = finite
    return count


def _configure_axes(
    axes: Axes,
    variable: str,
    x_min: float,
    x_max: float,
    plotted_values: Sequence[np.ndarray],
) -> None:
    axes.set_xlim(x_min, x_max)
    axes.grid(True, alpha=0.3)
    axes.axhline(0, color="#444444", linewidth=0.8)
    axes.axvline(0, color="#444444", linewidth=0.8)
    axes.set_xlabel(variable)
    axes.set_ylabel("y")
    axes.set_title("Math plot")
    axes.legend(loc="best", fontsize=8)

    finite_arrays = [
        values[np.isfinite(values)] for values in plotted_values if np.isfinite(values).any()
    ]
    if not finite_arrays:
        axes.set_ylim(-1.0, 1.0)
        return

    finite_values = np.concatenate(finite_arrays)
    low = float(np.min(finite_values))
    high = float(np.max(finite_values))
    if high - low > 1e4:
        low = float(np.percentile(finite_values, 2))
        high = float(np.percentile(finite_values, 98))
    if not math.isfinite(low) or not math.isfinite(high):
        low, high = -1.0, 1.0
    if high - low <= 1e-12:
        low -= 1.0
        high += 1.0
    margin = 0.05 * (high - low)
    axes.set_ylim(low - margin, high + margin)


def _save_png(figure: Figure) -> bytes:
    for dpi in (100, 84, 68):
        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            facecolor="white",
            metadata={"Software": "math-tools"},
        )
        png = buffer.getvalue()
        if len(png) <= MAX_PNG_BYTES:
            return png
    raise MathError("resource_limit", "Не удалось уложить PNG в 256 КиБ.")


def _format_position(value: float) -> str:
    if abs(value) < 1e-12:
        value = 0.0
    return f"{value:.6g}"
